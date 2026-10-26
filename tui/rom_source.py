"""基准 ROM 获取（tui/DESIGN.md §2.2 的四路来源）。

查找顺序（命中即用，并在返回值里标明来源与 sha256）：

1. ``/sys/bus/pci/devices/<bdf>/rom``——需 root；仅在 ``allow_sys_enable=True`` 时
   短暂把使能位写 1 再写回 0（这是本模块唯一的写操作，且不改变显卡工作状态）。
2. ``nvidia-smi --gpu-rom-dump`` 到临时文件——需 root。
3. 调用方显式给出的路径（``--rom``）。
4. 仓库自带样本 ``firmware/v100_original_nvflash.rom``——只读，界面必须明示来源。

对外接口：

* ``load_rom(bdf=None, explicit_path=None, allow_sys_enable=False) -> RomCandidate``
* ``repo_sample_rom() -> Path | None``
* ``RomError(code, reason, advice)``
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import constants as C
from . import env_probe

# 能容纳 payload 窗口的最小 ROM 长度：330,240 字节（窗口起点 + 窗口长度）
MIN_ROM_LENGTH = C.PAYLOAD_WINDOW_START + C.PAYLOAD_WINDOW_LENGTH

SOURCE_SYSFS = "sysfs"
SOURCE_SMI = "nvidia-smi"
SOURCE_FILE = "file"
SOURCE_REPO = "repo"

SOURCE_LABELS = {
    SOURCE_SYSFS: "驱动 sysfs ROM",
    SOURCE_SMI: "nvidia-smi 转储",
    SOURCE_FILE: "指定文件",
    SOURCE_REPO: "仓库自带样本",
}


class RomError(Exception):
    """ROM 获取/校验失败；带错误码与可执行建议，便于界面直接展示。"""

    def __init__(self, code, reason, advice=""):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.advice = advice

    def __str__(self) -> str:
        return f"错误[{self.code}] {self.reason}"


@dataclass
class RomCandidate:
    data: bytes
    source: str
    sha256: str
    kind: str = SOURCE_FILE
    path: str = None

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.kind, self.kind)

    @property
    def sha8(self) -> str:
        return self.sha256[:8]

    def describe(self) -> str:
        """一行中文来源说明（界面与历史都用这一句）。"""
        where = self.path or self.source
        return f"{self.source_label}：{where}（{self.size} 字节，sha256 {self.sha8}…）"


def _make_candidate(data: bytes, source: str, kind: str, path: str = None) -> RomCandidate:
    return RomCandidate(
        data=data,
        source=source,
        sha256=hashlib.sha256(data).hexdigest(),
        kind=kind,
        path=path,
    )


def _check_payload_window(data: bytes, origin: str) -> None:
    """ROM 必须不短于 payload 窗口，否则覆盖镜像无处可取。"""
    if len(data) < MIN_ROM_LENGTH:
        raise RomError(
            C.ERR_ROM_TOO_SHORT,
            f"{origin} 只有 {len(data)} 字节，少于覆盖镜像所需的 {MIN_ROM_LENGTH} 字节。",
            "请提供完整 VBIOS dump（≥ 330,240 字节），例如 "
            "`nvidia-smi --gpu-rom-dump` 或 `nvflash --save` 的输出。")


def repo_sample_rom():
    """仓库自带原厂样本；不存在返回 ``None``。"""
    for name in ("v100_original_nvflash.rom", "v100_original.rom"):
        path = C.FIRMWARE_DIR / name
        if path.is_file():
            return path
    return None


def _from_sysfs(bdf, allow_sys_enable):
    """① sysfs；不顺路返回 ``(None, 原因)``，不抛异常。"""
    if not bdf:
        return None, "未指定 BDF。"
    if not env_probe.is_root():
        return None, "读取 sysfs ROM 需要 root 权限。"
    rom_path = Path(C.SYSFS_PCI_DEVICES) / bdf / "rom"
    if not rom_path.exists():
        return None, f"{rom_path} 不存在。"
    try:
        if allow_sys_enable:
            rom_path.write_text("1")
        try:
            data = rom_path.read_bytes()
        finally:
            if allow_sys_enable:
                try:
                    rom_path.write_text("0")
                except OSError:
                    pass
    except OSError as exc:
        return None, f"读取 sysfs ROM 失败：{exc}"
    if not data:
        return None, "sysfs ROM 读出 0 字节（可能被驱动独占）。"
    return data, ""


def _from_smi_dump(bdf):
    """② nvidia-smi 转储到临时文件；失败返回 ``(None, 原因)``。"""
    if not env_probe.is_root():
        return None, "nvidia-smi ROM 转储需要 root 权限。"
    if shutil.which("nvidia-smi") is None:
        return None, "未找到 nvidia-smi。"
    tmpdir = tempfile.mkdtemp(prefix="v100-tui-rom-")
    out = Path(tmpdir) / "dump.rom"
    argv = ["nvidia-smi"]
    if bdf:
        argv.append(f"--id={bdf}")
    argv += ["--gpu-rom-dump", "-f", str(out)]
    try:
        res = env_probe.run(argv, timeout=C.TIMEOUT_ROM_DUMP_S)
        if not res.ok or not out.is_file():
            return None, (res.err or res.out or "nvidia-smi ROM 转储失败").strip()
        data = out.read_bytes()
        if not data:
            return None, "nvidia-smi 转储文件为空。"
        return data, ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def load_rom(bdf=None, explicit_path=None, allow_sys_enable=False, prefer_repo=False):
    """按设计顺序取基准 ROM，返回 ``RomCandidate``。

    参数:
        bdf:              目标卡 BDF（用于 ①②）
        explicit_path:    用户显式指定的 ROM 路径（第 ③ 路）
        allow_sys_enable: 是否允许写 sysfs 使能位（仅 root 下有效）
        prefer_repo:      跳过 ①②，直接用仓库样本（离线模式用）

    异常:
        RomError: 四路全部失败，或取到的文件不满足最小长度要求
    """
    attempts = []
    norm = bdf or ""

    if not prefer_repo:
        data, why = _from_sysfs(norm, allow_sys_enable)
        if data:
            _check_payload_window(data, "sysfs ROM")
            return _make_candidate(data, f"{C.SYSFS_PCI_DEVICES}/{norm}/rom",
                                   SOURCE_SYSFS,
                                   f"{C.SYSFS_PCI_DEVICES}/{norm}/rom")
        attempts.append(f"① {why}")

        data, why = _from_smi_dump(norm)
        if data:
            _check_payload_window(data, "nvidia-smi 转储")
            return _make_candidate(data, "nvidia-smi --gpu-rom-dump", SOURCE_SMI)
        attempts.append(f"② {why}")

    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.is_file():
            try:
                data = path.read_bytes()
            except OSError as exc:
                attempts.append(f"③ 读取 {explicit_path} 失败：{exc}")
            else:
                _check_payload_window(data, f"文件 {explicit_path}")
                return _make_candidate(data, str(path), SOURCE_FILE, str(path))
        else:
            attempts.append(f"③ 文件不存在或不可读：{explicit_path}")
    elif not prefer_repo:
        attempts.append("③ 未指定 --rom 路径。")

    sample = repo_sample_rom()
    if sample is not None:
        data = sample.read_bytes()
        _check_payload_window(data, f"仓库样本 {sample.name}")
        return _make_candidate(
            data, f"firmware/{sample.name}", SOURCE_REPO, f"firmware/{sample.name}")

    raise RomError(
        C.ERR_ROM_READ,
        "无法获取基准 ROM，四条来源全部失败。",
        "请以 root 运行以读取显卡 ROM，或使用 `--rom <文件>` 指定一份完整 dump。"
        "尝试记录：" + "；".join(attempts) if attempts else
        "请使用 `--rom <文件>` 指定一份完整 dump。")


def load_from_bytes(data: bytes, source: str = "内存数据", kind: str = SOURCE_FILE):
    """把已有字节包装成候选（测试与导入场景用）。"""
    _check_payload_window(data, source)
    return _make_candidate(data, source, kind)
