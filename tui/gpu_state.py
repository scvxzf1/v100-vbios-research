"""GPU 现值探测（只读）。

原则（tui/DESIGN.md §4）：
* 一律通过 ``nvidia-smi --query-gpu`` **按 BDF** 读取，不用 ``-i`` 索引（索引会随卡序漂移）。
* 定位 V100 一律扫 ``/sys/bus/pci/devices`` 的 vendor/device，**不硬编码 BDF**。
* 任何读取失败降级为 ``None``（未知）并记入 ``GpuState.notes``，不抛异常。

对外接口：

* ``list_gpus() -> list[GpuState]``
* ``read_gpu_state(bdf) -> GpuState``
* ``find_v100_bdf() -> str | None``
* ``override_present() -> bool | None``（驱动 registry 里是否已有 vbios 覆盖）
* ``boot_patch_state() -> dict``（开机持久化单元的启用/运行状态）
* ``snapshot(bdf=None) -> Snapshot``（把上面几项打包，供界面状态栏使用）
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import constants as C
from . import env_probe

# nvidia-smi 查询列（顺序即解析顺序）；单位统一用 nounits 拿裸数字
_QUERY_FIELDS = (
    "pci.bus_id",
    "name",
    "vbios_version",
    "power.limit",
    "power.max_limit",
    "power.default_limit",
    "clocks.max.sm",
    "clocks.applications.graphics",
    "clocks.current.sm",
    "temperature.gpu",
    "clocks.max.mem",
)

_BDF_RE = re.compile(r"^(?:[0-9a-fA-F]{4,8}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


def normalize_bdf(text) -> str:
    """把 ``00000000:03:00.0`` / ``03:00.0`` / ``0000:03:00.0`` 统一成 ``0000:03:00.0``。"""
    if not text:
        return ""
    raw = str(text).strip()
    if not raw:
        return ""
    parts = raw.split(":")
    if len(parts) == 3:
        domain, bus, devfn = parts
    elif len(parts) == 2:
        domain, (bus, devfn) = "0", parts
    else:
        return raw.lower()
    try:
        domain = f"{int(domain, 16):04x}"
    except ValueError:
        return raw.lower()
    return f"{domain}:{bus.lower()}.{devfn.split('.')[0].lower()}.{devfn.split('.')[-1]}"


def _to_float(text):
    value = (text or "").strip()
    if not value or value.upper().startswith("[N/A") or value.upper() == "N/A":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _to_int(text):
    value = _to_float(text)
    return None if value is None else int(round(value))


@dataclass
class GpuState:
    """单卡现值。字段缺失一律 ``None``，不用 0 冒充。"""

    bdf: str
    name: str = None
    vbios_version: str = None
    power_limit_w: float = None
    power_max_w: float = None
    power_default_w: float = None
    sm_max_mhz: int = None
    apps_graphics_mhz: int = None
    sm_current_mhz: int = None
    temp_c: int = None
    mem_max_mhz: int = None
    notes: list = field(default_factory=list)

    @property
    def is_v100(self) -> bool:
        return bool(self.vbios_version or self.name) and "V100" in (self.name or "")

    def as_dict(self) -> dict:
        return {
            "bdf": self.bdf,
            "name": self.name,
            "vbios_version": self.vbios_version,
            "power_limit_w": self.power_limit_w,
            "power_max_w": self.power_max_w,
            "power_default_w": self.power_default_w,
            "sm_max_mhz": self.sm_max_mhz,
            "apps_graphics_mhz": self.apps_graphics_mhz,
            "sm_current_mhz": self.sm_current_mhz,
            "temp_c": self.temp_c,
            "mem_max_mhz": self.mem_max_mhz,
        }


def _smi_query(bdf=None):
    """调用 nvidia-smi，返回 (成功?, 行列表, 错误说明)。"""
    argv = ["nvidia-smi", f"--query-gpu={','.join(_QUERY_FIELDS)}",
            "--format=csv,noheader,nounits"]
    if bdf:
        argv.insert(1, f"--id={normalize_bdf(bdf)}")
    res = env_probe.run(argv, timeout=C.TIMEOUT_SMI_S)
    if not res.ok:
        return False, [], (res.err or res.out or "nvidia-smi 调用失败").strip()
    rows = [ln for ln in res.out.splitlines() if ln.strip()]
    return True, rows, ""


def _parse_row(line: str) -> GpuState:
    cells = [c.strip() for c in line.split(",")]
    if len(cells) < len(_QUERY_FIELDS):
        cells += [""] * (len(_QUERY_FIELDS) - len(cells))
    state = GpuState(bdf=normalize_bdf(cells[0]))
    state.name = cells[1] or None
    state.vbios_version = cells[2] or None
    state.power_limit_w = _to_float(cells[3])
    state.power_max_w = _to_float(cells[4])
    state.power_default_w = _to_float(cells[5])
    state.sm_max_mhz = _to_int(cells[6])
    state.apps_graphics_mhz = _to_int(cells[7])
    state.sm_current_mhz = _to_int(cells[8])
    state.temp_c = _to_int(cells[9])
    state.mem_max_mhz = _to_int(cells[10])
    return state


def list_gpus():
    """枚举所有卡；失败返回空列表（不抛异常）。"""
    ok, rows, err = _smi_query()
    if not ok:
        return []
    out = []
    for line in rows:
        try:
            out.append(_parse_row(line))
        except Exception:          # 解析异常不得让界面崩掉
            continue
    _ = err
    return out


def read_gpu_state(bdf=None):
    """读取指定卡（默认第一张）的现值。失败时返回带 ``notes`` 的空状态。"""
    target = normalize_bdf(bdf) if bdf else None
    gpus = list_gpus()
    if not gpus:
        state = GpuState(bdf=target or "", notes=["nvidia-smi 不可用，现值未知。"])
        return state
    if target:
        for gpu in gpus:
            if gpu.bdf == target:
                return gpu
        state = GpuState(bdf=target, notes=[f"未找到 BDF {target} 对应的卡，现值未知。"])
        return state
    return gpus[0]


# ─────────────────────────────────────────────────────────────────────────────
# sysfs 侧定位（不依赖 nvidia-smi，也用于「无驱动」场景）
# ─────────────────────────────────────────────────────────────────────────────
def _read_hex_file(path: Path):
    try:
        return int(path.read_text().strip(), 16)
    except (OSError, ValueError):
        return None


def find_v100_bdf():
    """在 sysfs 里按 vendor:device 找 Tesla V100（10de:1db1），找不到返回 ``None``。"""
    base = Path(C.SYSFS_PCI_DEVICES)
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return None
    for name in entries:
        dev_dir = base / name
        vendor = _read_hex_file(dev_dir / "vendor")
        device = _read_hex_file(dev_dir / "device")
        if vendor == C.VENDOR_ID_NVIDIA and device == C.DEVICE_ID_V100_SXM2:
            return normalize_bdf(name)
    return None


def list_v100_bdfs():
    base = Path(C.SYSFS_PCI_DEVICES)
    out = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return out
    for name in entries:
        dev_dir = base / name
        if (_read_hex_file(dev_dir / "vendor") == C.VENDOR_ID_NVIDIA
                and _read_hex_file(dev_dir / "device") == C.DEVICE_ID_V100_SXM2):
            out.append(normalize_bdf(name))
    return out


def device_present(bdf) -> bool:
    """该 BDF 是否还在总线上（用于「半摘卡」判定）。"""
    norm = normalize_bdf(bdf)
    return bool(norm) and (Path(C.SYSFS_PCI_DEVICES) / norm).exists()


# ─────────────────────────────────────────────────────────────────────────────
# 驱动 registry 覆盖 / 开机持久化（只读）
# ─────────────────────────────────────────────────────────────────────────────
def override_present():
    """``/proc/driver/nvidia/registry`` 里是否已存在 vbios 二进制覆盖。

    返回 ``True``/``False``/``None``（读不到即未知）。
    """
    try:
        raw = Path(C.REGISTRY_PATH).read_bytes()
    except OSError:
        return None
    text = raw.decode("utf-8", errors="replace")
    if 'Binary: "vbios"' in text or "Binary: vbios" in text:
        return True
    return False


def boot_patch_state() -> dict:
    """开机持久化状态：单元启用/运行 + payload 是否就位。全部只读。"""
    state = {
        "unit": C.BOOT_UNIT,
        "enabled": env_probe.unit_is_enabled(C.BOOT_UNIT),
        "active": env_probe.unit_is_active(C.BOOT_UNIT),
        "payload_path": C.BOOT_PAYLOAD_PATH,
        "payload_present": os.path.exists(C.BOOT_PAYLOAD_PATH),
        "payload_size": None,
        "log_path": C.BOOT_LOG_PATH,
        "unknown": False,
    }
    try:
        state["payload_size"] = os.path.getsize(C.BOOT_PAYLOAD_PATH)
    except OSError:
        state["payload_size"] = None
    if state["enabled"] is None and state["active"] is None:
        state["unknown"] = True
    return state


@dataclass
class Snapshot:
    """界面状态栏所需的全部事实。"""

    env: object
    gpu: GpuState
    v100_bdf: str = None
    override: bool = None
    boot: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    @property
    def has_v100(self) -> bool:
        return bool(self.v100_bdf or (self.gpu and self.gpu.is_v100))


def snapshot(bdf=None, env=None) -> Snapshot:
    """一次性收集状态；任何子项失败都只降级，不影响其它项。"""
    env = env or env_probe.probe()
    v100_bdf = find_v100_bdf()
    target = normalize_bdf(bdf) if bdf else v100_bdf
    gpu = read_gpu_state(target) if target else GpuState(bdf="", notes=["未检测到 V100。"])
    notes = list(gpu.notes)
    if not v100_bdf:
        notes.append("未在总线上找到 10de:1db1 的 Tesla V100。")
    return Snapshot(
        env=env,
        gpu=gpu,
        v100_bdf=v100_bdf,
        override=override_present(),
        boot=boot_patch_state(),
        notes=notes,
    )
