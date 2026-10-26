"""VBIOS 覆盖注入执行层（t6）。

设计依据：``tui/DESIGN.md`` §3.2「快速注入」、§5.4「运行期状态」、§9.1「注入阶段失败矩阵」、
§9.4「半摘卡状态的识别与兜底」、§9.5「并发与幂等」。

第一原则：**绝不把系统留在半残状态**。因此本模块只做三件事：

1. 预检（只读），任一致命项不满足就**不提交**，系统零改动；
2. 生成一份自包含的 bash 脚本，交给 ``systemd-run --no-block`` 作为独立单元执行
   —— 卸载驱动会中断图形会话，TUI 进程树不能是它的父进程；
3. 读取运行期状态（``/run/v100-tui/state.json``）与最终结论（``last-job.json``），
   供界面判断「上次是否停在危险阶段」。

注入脚本的流程与 ``deploy/v100-boot-patch.sh`` **完全同构**（顺序不允许自创）::

    ① 停 lactd（它握住 /dev/nvidia* 会导致驱动无法卸载）
    ② 停显示管理器
    ③ 卸载 nvidia_drm / nvidia_modeset / nvidia_uvm / nvidia，各 3 次重试
    ④ fuser 检查 /dev/nvidia{0,1,ctl,uvm}，有进程占用则放弃并回原厂
    ⑤ echo 1 > /sys/bus/pci/devices/<bdf>/remove      摘卡
    ⑥ modprobe nvidia
    ⑦ { printf 'vbios='; cat <覆盖镜像>; } > /proc/driver/nvidia/registry
    ⑧ echo 1 > /sys/bus/pci/rescan                     卡带覆盖上线
    ⑨ 校验 power.max_limit 是否等于期望值并回报
    ⑩ 恢复：拉起 lactd、显示管理器（由 trap EXIT 兜底，任何退出路径都会执行）

``submit()`` / ``restore_factory_order()`` 之外的 ``plan_injection()`` / ``plan_restore()``
是**纯函数**：只返回脚本全文与 ``systemd-run`` 参数，不写盘、不 spawn。执行层因此可以
在完全不触碰系统的前提下被校验（``dry_run=True`` 走的就是这条路径）。

文案约定：本模块返回的错误码与界面对齐 DESIGN.md §7.2 的分区（E01xx 环境权限、
E04xx 注入、E09xx 内部错误）。界面显示文案的唯一来源仍是 ``tui/ui/text_zh.py``；
本模块 ``message`` / ``detail`` 字段是**给日志与降级展示用的**中文事实描述。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

# 复用 t4 的常量模块与统一外部命令包装（DESIGN §2.1 / §9.3）
from . import constants as C
from . import env_probe as probe

__all__ = [
    "Check",
    "InjectJob",
    "JobHandle",
    "JobState",
    "Hint",
    "RunResult",
    "run",
    "job_from_payload",
    "validate_job",
    "precheck",
    "precheck_ok",
    "submit",
    "read_job_state",
    "last_result",
    "resume_hint",
    "restore_factory_order",
    "render_inject_script",
    "plan_injection",
    "plan_restore",
    "systemd_run_argv",
    "SubmissionPlan",
    "render_restore_script",
    "running_unit",
    "systemd_run_available",
    "guess_display_manager",
    "NV_DEVICE_NODES",
    "REGISTRY_PATH",
    "RUN_DIR",
    "LOG_DIR",
    "PAYLOAD_SIZE",
]

# ---------------------------------------------------------------------------
# 常量：一律引用 tui/constants.py，本模块不重复定义任何协议数值或系统路径
# ---------------------------------------------------------------------------
REGISTRY_PATH: str = C.REGISTRY_PATH
RUN_DIR: str = C.RUN_DIR
LOG_DIR: str = C.LOG_DIR
PAYLOAD_SIZE: int = C.PAYLOAD_WINDOW_LENGTH
POWER_MIN_W: float = C.POWER_MIN_W
POWER_MAX_W: float = C.POWER_MAX_W
CLOCK_MIN_MHZ: int = C.CLOCK_MIN_MHZ
CLOCK_MAX_MHZ: int = C.CLOCK_MAX_MHZ
SYSFS_PCI_DEVICES: str = C.SYSFS_PCI_DEVICES
LACTD_UNIT_DEFAULT: str = C.LACTD_UNIT
# 摘卡前要检查占用的设备节点（与 deploy/v100-boot-patch.sh 的 fuser 检查列表一致；
# 该列表是驱动设备节点命名约定，不是字段偏移）
NV_DEVICE_NODES: tuple[str, ...] = (
    "/dev/nvidia0",
    "/dev/nvidia1",
    "/dev/nvidiactl",
    "/dev/nvidia-uvm",
)

# 注入相关超时与单元名前缀（常量模块未覆盖的部分在此集中声明）
INJECT_TIMEOUT_SEC = 180
SYSTEMCTL_TIMEOUT_S = C.TIMEOUT_SYSTEMCTL_S
INJECT_UNIT_PREFIX = "v100-tui-inject"
RESTORE_UNIT_PREFIX = "v100-tui-restore"

# 危险阶段（DESIGN §5.4 / §9.4）：这些 phase 落盘后就说明可能处于「半摘卡」
_DANGEROUS_PHASES = frozenset({"driver_unloaded", "card_removed", "payload_written", "rescanning"})
_TERMINAL_OK_PHASES = frozenset({"done", "restoring"})

_BDF_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:\\-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------------------
# 统一外部命令包装（DESIGN §2.1：执行层所有外部命令都经过 run()）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RunResult:
    """一次外部命令的结果：退出码、stdout、stderr、是否超时。

    字段是 ``env_probe.RunResult`` 的规整版本（rc/out/err → returncode/stdout/stderr），
    并补一个给中文错误原因用的 :meth:`tail`。
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, lines: int = 3) -> str:
        """取输出末尾若干行，用于中文错误原因（不输出整段）。"""
        text = (self.stderr or "").strip() or (self.stdout or "").strip()
        if not text:
            return f"退出码 {self.returncode}"
        parts = [ln for ln in text.splitlines() if ln.strip()]
        return " / ".join(parts[-lines:])


def run(argv: Sequence[str], *, timeout: float = 20.0) -> RunResult:
    """执行外部命令。

    实现复用 ``tui/env_probe.py`` 的统一包装（DESIGN §2.1：执行层所有外部命令都经过
    同一个 ``run()``），这里只做字段命名的归一与末尾行摘要，便于写中文失败原因。
    """
    args = tuple(str(a) for a in argv)
    res = probe.run(list(args), timeout=timeout)
    return RunResult(
        args,
        returncode=res.rc,
        stdout=res.out or "",
        stderr=res.err or "",
        timed_out=res.rc == 124,
    )


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _iso_now() -> str:
    """带本地时区偏移的 ISO 8601（DESIGN §5.2 时间格式）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_iso(text: str) -> float | None:
    """把 ISO 8601 解析成 epoch 秒；解析失败返回 None。"""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _boot_epoch() -> float | None:
    """由 /proc/uptime 反推本次开机时刻（epoch 秒），用于判定陈旧状态。"""
    try:
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - uptime


def _stamp() -> str:
    """单元名与日志名共用的时间戳：无冒号、无空格，可安全嵌入 systemd 单元名。"""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _num(value: float) -> str:
    """把功耗写成不带多余零的十进制串（350.0 → "350"，300.5 → "300.5"）。"""
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _read_json(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _pid_list(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^0-9]+", text) if tok]


def systemd_run_available() -> bool:
    """是否存在 systemd-run（缺失时绝不在 TUI 进程内直接卸驱动，DESIGN §6.2）。"""
    return probe.systemd_run_available()


def _unit_active(unit: str) -> bool:
    """单元是否 active；未知（无 systemctl）时按未运行处理。"""
    if not unit:
        return False
    return probe.unit_is_active(unit) is True


def guess_display_manager() -> str:
    """探测显示管理器单元名：优先取 active 的那个，取不到返回空串。"""
    unit, _active = probe.detect_display_manager()
    return unit or ""


def running_unit() -> str:
    """返回当前仍在运行的注入单元名（无则空串）。"""
    state = read_job_state()
    if state.unit and _unit_active(state.unit):
        return state.unit
    return ""


@contextmanager
def _run_lock() -> Iterator[bool]:
    """运行目录上的排他锁（DESIGN §9.5）：拿不到锁说明有并发操作在进行。"""
    path = Path(RUN_DIR) / "lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+", encoding="utf-8")
    except OSError:
        yield True  # 没有运行目录权限时不阻断，交由预检报出可读原因
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Check:
    """预检项。``blocking`` 为真时，只要不满足就禁止提交。"""

    key: str
    title: str
    ok: bool
    detail: str = ""
    blocking: bool = True
    code: str = ""

    @property
    def blocks(self) -> bool:
        return self.blocking and not self.ok


@dataclass(frozen=True)
class InjectJob:
    """一次注入任务的完整意图（只含语义值，不含任何字节偏移）。"""

    payload_path: Path
    power_w: float
    bdf: str
    payload_sha256: str = ""
    payload_size: int = PAYLOAD_SIZE
    sm_max_mhz: int | None = None
    dm_unit: str = ""
    lactd_unit: str = LACTD_UNIT_DEFAULT
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload_path", Path(self.payload_path))
        object.__setattr__(self, "power_w", float(self.power_w))
        if not self.payload_sha256:
            object.__setattr__(self, "payload_sha256", _sha256_file(self.payload_path))
        if not self.payload_size:
            try:
                object.__setattr__(self, "payload_size", self.payload_path.stat().st_size)
            except OSError:
                object.__setattr__(self, "payload_size", 0)


@dataclass(frozen=True)
class JobHandle:
    """提交结果。ok 为真时 unit / script / log 可用。"""

    ok: bool
    unit: str = ""
    script: Path | None = None
    log: Path | None = None
    submitted_at: str = ""
    code: str = ""
    message: str = ""
    advice: str = ""
    journal_hint: str = ""
    # 试算与审查用：完整的 systemd-run 参数；dry_run 为真表示没有真正提交
    argv: tuple[str, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class JobState:
    """运行期状态（state.json）+ 最终结论（last-job.json）的合并视图。"""

    phase: str = "unknown"
    unit: str = ""
    bdf: str = ""
    started_at: str = ""
    updated_at: str = ""
    payload_sha256: str = ""
    expected_power_w: float | None = None
    expected_sm_mhz: int | None = None
    log_path: str = ""
    device_present: bool = False
    stale: bool = False
    result: str = ""
    detail: str = ""
    finished_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        """阶段是否表示「正在跑」。"""
        return self.phase in _DANGEROUS_PHASES or self.phase == "verifying"


@dataclass(frozen=True)
class Hint:
    """启动时的提示/危险态判定结果（DESIGN §9.4）。"""

    danger: bool
    title: str
    detail: str
    actions: tuple[str, ...] = ()
    advice: str = ""
    code: str = ""


# ---------------------------------------------------------------------------
# 任务构造与校验
# ---------------------------------------------------------------------------
def job_from_payload(
    payload_path: str | Path,
    power_w: float,
    bdf: str,
    *,
    sm_max_mhz: int | None = None,
    dm_unit: str = "",
    lactd_unit: str = LACTD_UNIT_DEFAULT,
    note: str = "",
) -> InjectJob:
    """由覆盖镜像路径构造任务，自动补齐尺寸与 sha256。"""
    return InjectJob(
        payload_path=Path(payload_path),
        power_w=power_w,
        bdf=bdf,
        sm_max_mhz=sm_max_mhz,
        dm_unit=dm_unit,
        lactd_unit=lactd_unit,
        note=note,
    )


def validate_job(job: InjectJob) -> list[str]:
    """返回中文问题清单；空列表表示可以提交。所有值都会写进 shell 脚本，必须严格校验。"""
    problems: list[str] = []
    if not _BDF_RE.match(job.bdf or ""):
        problems.append(f"设备地址不合法：{job.bdf!r}（应形如 0000:04:00.0）")
    if not (POWER_MIN_W <= job.power_w <= POWER_MAX_W):
        problems.append(
            f"目标功耗 {job.power_w:g} W 超出允许范围 {POWER_MIN_W:g}–{POWER_MAX_W:g} W"
        )
    if job.sm_max_mhz is not None and not (CLOCK_MIN_MHZ <= job.sm_max_mhz <= CLOCK_MAX_MHZ):
        problems.append(
            f"目标核心上报上限 {job.sm_max_mhz} MHz 超出允许范围 "
            f"{CLOCK_MIN_MHZ}–{CLOCK_MAX_MHZ} MHz"
        )
    if job.dm_unit and not _UNIT_RE.match(job.dm_unit):
        problems.append(f"显示管理器单元名不合法：{job.dm_unit!r}")
    if not _UNIT_RE.match(job.lactd_unit or ""):
        problems.append(f"lactd 单元名不合法：{job.lactd_unit!r}")
    path = Path(job.payload_path)
    if not path.is_file():
        problems.append(f"覆盖镜像不存在：{path}")
    else:
        if not os.access(path, os.R_OK):
            problems.append(f"覆盖镜像不可读：{path}")
        size = path.stat().st_size
        if size != job.payload_size:
            problems.append(f"覆盖镜像尺寸不符：{size} 字节（期望 {job.payload_size} 字节）")
        if job.payload_sha256 and _SHA_RE.match(job.payload_sha256):
            actual = _sha256_file(path)
            if actual != job.payload_sha256:
                problems.append(
                    f"覆盖镜像 sha256 不符：{actual[:8]}…（期望 {job.payload_sha256[:8]}…）"
                )
    return problems


# ---------------------------------------------------------------------------
# 预检（只读，系统零改动）
# ---------------------------------------------------------------------------
def _fuser_holders(device: str) -> list[str]:
    res = run(["fuser", device], timeout=10)
    return _pid_list(res.stdout) + _pid_list(res.stderr)


def precheck(job: InjectJob) -> list[Check]:
    """§3.2 预检页：全部只读。``blocks`` 为真的项不满足时禁止提交。"""
    checks: list[Check] = []
    is_root = os.geteuid() == 0

    checks.append(
        Check(
            key="root",
            title="运行身份",
            ok=is_root,
            detail="以 root 运行" if is_root else "未以 root 运行，注入与持久化都需要 root",
            code="" if is_root else C.ERR_INJECT_DISABLED,
        )
    )

    dev_dir = Path(SYSFS_PCI_DEVICES) / job.bdf
    checks.append(
        Check(
            key="bdf_present",
            title="目标设备存在",
            ok=dev_dir.is_dir(),
            detail=f"{dev_dir} {'存在' if dev_dir.is_dir() else '不存在'}",
            code="E0401",
        )
    )

    registry = Path(REGISTRY_PATH)
    registry_ok = registry.is_file() and os.access(registry, os.W_OK)
    checks.append(
        Check(
            key="registry",
            title="驱动 registry 接口可写",
            ok=registry_ok,
            detail=(
                f"{REGISTRY_PATH} 可写"
                if registry_ok
                else f"{REGISTRY_PATH} {'不存在' if not registry.is_file() else '不可写'}"
            ),
            code=C.ERR_REGISTRY_MISSING,
        )
    )

    fuser_ok = shutil.which("fuser") is not None
    if not fuser_ok:
        checks.append(
            Check(
                key="devices_free",
                title="/dev/nvidia* 无进程占用",
                ok=False,
                blocking=False,
                detail="未找到 fuser，无法确认设备占用；摘卡时可能阻塞在内核态数分钟",
            )
        )
    else:
        busy: list[str] = []
        for dev in NV_DEVICE_NODES:
            if not Path(dev).exists():
                continue
            holders = _fuser_holders(dev)
            if holders:
                busy.append(f"{dev}（pid {','.join(holders)}）")
        checks.append(
            Check(
                key="devices_free",
                title="/dev/nvidia* 无进程占用",
                ok=not busy,
                detail=(
                    "无进程占用"
                    if not busy
                    else "被占用：" + "；".join(busy) + "（持有设备的进程会让内核摘卡阻塞数分钟）"
                ),
                code="E0403",
            )
        )

    payload = Path(job.payload_path)
    payload_problems: list[str] = []
    if not payload.is_file():
        payload_problems.append("文件不存在")
    else:
        size = payload.stat().st_size
        if size != job.payload_size:
            payload_problems.append(f"尺寸 {size} 字节，期望 {job.payload_size} 字节")
        if job.payload_sha256:
            actual = _sha256_file(payload)
            if actual != job.payload_sha256:
                payload_problems.append(f"sha256 {actual[:8]}…，期望 {job.payload_sha256[:8]}…")
    checks.append(
        Check(
            key="payload",
            title="覆盖镜像可校验",
            ok=not payload_problems,
            detail="尺寸与 sha256 一致" if not payload_problems else "；".join(payload_problems),
            code="E0405",
        )
    )

    # 运行目录与日志目录：root 下就地创建，非 root 只判定「能否创建」，不凭空判定失败
    dirs_ok = True
    dir_details: list[str] = []
    for raw in (RUN_DIR, LOG_DIR):
        target = Path(raw)
        writable = target.is_dir() and os.access(target, os.W_OK)
        if not writable and is_root:
            try:
                target.mkdir(parents=True, exist_ok=True)
                writable = os.access(target, os.W_OK)
            except OSError:
                writable = False
        dir_details.append(
            f"{raw} {'可写' if writable else '当前不可写（注入时以 root 自动创建）'}"
        )
        dirs_ok = dirs_ok and writable
    checks.append(
        Check(
            key="dirs",
            title="运行与日志目录可写",
            ok=dirs_ok,
            # 非 root 时不具备判定条件，只做提示，不阻断（真正的注入一定以 root 运行）
            blocking=is_root,
            detail="；".join(dir_details),
            code="E0406",
        )
    )

    sd_ok = systemd_run_available()
    checks.append(
        Check(
            key="systemd_run",
            title="systemd-run 可用",
            ok=sd_ok,
            detail=(
                "可用，注入会作为独立单元提交"
                if sd_ok
                else "缺少 systemd-run：绝不在本进程内直接卸载驱动"
            ),
            code="E0407",
        )
    )

    busy_unit = running_unit()
    checks.append(
        Check(
            key="no_running_job",
            title="无并行注入任务",
            ok=not busy_unit,
            detail=(
                f"已有注入任务在运行（单元 {busy_unit}）" if busy_unit else "当前没有运行中的注入任务"
            ),
            code="E0404",
        )
    )

    # 以下两项只做展示，不阻断：显示管理器与 lactd 的状态
    dm = job.dm_unit or guess_display_manager()
    checks.append(
        Check(
            key="dm",
            title="显示管理器",
            ok=True,
            blocking=False,
            detail=(f"{dm} 运行中，注入前会先停止它" if dm else "未探测到运行中的显示管理器"),
        )
    )
    checks.append(
        Check(
            key="lactd",
            title="lactd",
            ok=True,
            blocking=False,
            detail=(
                f"{job.lactd_unit} 运行中，注入前会先停止它"
                if _unit_active(job.lactd_unit)
                else f"{job.lactd_unit} 未运行"
            ),
        )
    )
    return checks


def precheck_ok(checks: Sequence[Check]) -> bool:
    """全部阻断项都通过才允许提交。"""
    return not any(check.blocks for check in checks)


# ---------------------------------------------------------------------------
# 单元脚本生成
# ---------------------------------------------------------------------------
_HEADER = r"""#!/bin/bash
# =============================================================================
# v100-tui 自动生成脚本（请勿手工编辑）
#   单元名   : @@UNIT@@
#   生成时间 : @@GENERATED_AT@@
#@@EXTRA_HEADER@@
#
# 任何失败都走 fail() → on_exit 恢复序列，绝不把系统留在半摘卡状态。
# =============================================================================
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH

PAYLOAD='@@PAYLOAD@@'
PAYLOAD_SHA='@@PAYLOAD_SHA@@'
PAYLOAD_SIZE='@@PAYLOAD_SIZE@@'
BDF='@@BDF@@'
DEV='@@DEV@@'
REGISTRY='@@REGISTRY@@'
RUN_DIR='@@RUN_DIR@@'
LOG_DIR='@@LOG_DIR@@'
LOG='@@LOG@@'
UNIT='@@UNIT@@'
STARTED_AT='@@GENERATED_AT@@'
DM_UNIT='@@DM_UNIT@@'
LACTD_UNIT='@@LACTD_UNIT@@'
NV_DEVS='@@NV_DEVS@@'
EXPECT_W='@@EXPECT_W@@'
EXPECT_SM='@@EXPECT_SM@@'
FACTORY_W='@@FACTORY_W@@'
STATE="$RUN_DIR/state.json"
LAST="$RUN_DIR/last-job.json"

STOPPED_LACT=0
STOPPED_DM=0
TOUCHED=0
RESULT='failed'
DETAIL='脚本未执行到结论阶段'

mkdir -p "$RUN_DIR" "$LOG_DIR" 2>/dev/null
: > "$LOG" 2>/dev/null
# 输出同时进 journal（由 systemd 捕获）与日志文件（TUI 下次启动可读）
exec > >(exec tee -a "$LOG") 2>&1

log(){ printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }
now_iso(){ date '+%Y-%m-%dT%H:%M:%S%:z'; }

# 期望值归一成 JSON 数字或 null
case "$EXPECT_W" in ''|*[!0-9.]*) EXPECT_W_JSON=null ;; *) EXPECT_W_JSON="$EXPECT_W" ;; esac
case "$EXPECT_SM" in ''|*[!0-9]*) EXPECT_SM_JSON=null ;; *) EXPECT_SM_JSON="$EXPECT_SM" ;; esac
EXPECT_W_2DP=''
if [ -n "$EXPECT_W" ]; then
  EXPECT_W_2DP=$(LC_ALL=C printf '%.2f' "$EXPECT_W" 2>/dev/null)
fi

# write-ahead：先落盘当前阶段，再做动作（崩溃时状态只会超前，不会落后）
write_state(){
  local phase="$1" tmp="$STATE.tmp"
  printf '{"unit":"%s","bdf":"%s","phase":"%s","started_at":"%s","updated_at":"%s","payload_sha256":"%s","expected":{"power_max_w":%s,"sm_max_mhz":%s},"stop_lactd":%s,"dm_unit":"%s","log_path":"%s"}\n' \
    "$UNIT" "$BDF" "$phase" "$STARTED_AT" "$(now_iso)" "$PAYLOAD_SHA" \
    "$EXPECT_W_JSON" "$EXPECT_SM_JSON" \
    "$([ "$STOPPED_LACT" = 1 ] && echo true || echo false)" "$DM_UNIT" "$LOG" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$STATE" 2>/dev/null
}

write_last(){
  local tmp="$LAST.tmp" detail="$DETAIL"
  detail=${detail//\"/}
  detail=${detail//\\/}
  printf '{"unit":"%s","result":"%s","detail":"%s","log_path":"%s","finished_at":"%s","bdf":"%s","payload_sha256":"%s"}\n' \
    "$UNIT" "$RESULT" "$detail" "$LOG" "$(now_iso)" "$BDF" "$PAYLOAD_SHA" \
    > "$tmp" 2>/dev/null && mv -f "$tmp" "$LAST" 2>/dev/null
}

# ---- 单一失败出口：所有失败分支都必须经过它（DESIGN §9.4 原则一）----
fail(){
  RESULT='failed'
  DETAIL="$*"
  log "失败: $DETAIL"
  write_last
  exit 1   # 交给 on_exit 的恢复序列兜底
}

# ---- 恢复动作（幂等，可重复执行）----
rescue_devices(){
  local i
  if [ ! -e "$DEV" ]; then
    log "设备不在总线上，执行重新扫描总线"
    for i in 1 2 3; do
      echo 1 > /sys/bus/pci/rescan 2>/dev/null
      sleep 1
      [ -e "$DEV" ] && break
    done
  fi
  if [ -e "$DEV" ]; then
    log "设备 $BDF 在总线上"
  else
    RESULT='failed'
    DETAIL='重新扫描总线后显卡仍未出现，建议重启本机'
    log "★ 警告: $DETAIL"
  fi
  modprobe nvidia 2>/dev/null
  local m
  for m in nvidia_uvm nvidia_modeset nvidia_drm; do modprobe "$m" 2>/dev/null; done
  log "驱动模块已补齐"
}

rescue_services(){
  if [ "$STOPPED_LACT" = "1" ]; then
    if systemctl start "$LACTD_UNIT" 2>/dev/null; then
      log "已恢复 $LACTD_UNIT"
    else
      log "失败: $LACTD_UNIT 未恢复，请手动 systemctl start $LACTD_UNIT"
    fi
  fi
  if [ "$STOPPED_DM" = "1" ] && [ -n "$DM_UNIT" ]; then
    if systemctl start "$DM_UNIT" 2>/dev/null; then
      log "已恢复显示管理器（$DM_UNIT）"
    else
      log "失败: 显示管理器 $DM_UNIT 未恢复，请手动 systemctl start $DM_UNIT"
    fi
  fi
}

finalize(){
  if [ "$RESULT" = "ok" ]; then write_state done; else write_state failed; fi
  write_last
  log "结论: result=$RESULT  detail=$DETAIL"
  log '=== 结束 ==='
  sync 2>/dev/null
  sleep 0.2 2>/dev/null || true
  exit 0
}

on_exit(){
  trap - EXIT INT TERM
  if [ "$TOUCHED" = "0" ]; then
    log '未改动系统状态，跳过恢复序列'
    finalize
  fi
  log '=== 恢复序列（保证系统回到可用状态）==='
  write_state restoring
  rescue_devices
  rescue_services
  finalize
}
trap on_exit EXIT
trap 'log "收到终止信号"; exit 1' INT TERM
"""

_INJECT_BODY = r"""
# ============================ 注入主流程 ============================
log '=== v100-tui 注入开始 ==='
log "目标设备 $BDF，期望功耗上限 ${EXPECT_W} W$( [ -n "$EXPECT_SM" ] && printf '，期望核心上报上限 %s MHz' "$EXPECT_SM" )"

if [ "$(id -u)" -ne 0 ]; then
  fail '未以 root 运行，拒绝执行注入'
fi

# 提交后可能被改动：落盘前复核覆盖镜像
[ -r "$PAYLOAD" ] || fail "覆盖镜像不可读：$PAYLOAD"
FSZ=$(stat -c%s "$PAYLOAD" 2>/dev/null || echo 0)
[ "$FSZ" = "$PAYLOAD_SIZE" ] || fail "覆盖镜像尺寸不符：$FSZ 字节（期望 $PAYLOAD_SIZE 字节）"
if command -v sha256sum >/dev/null 2>&1; then
  ASHA=$(sha256sum "$PAYLOAD" 2>/dev/null | cut -d' ' -f1)
  [ "$ASHA" = "$PAYLOAD_SHA" ] || fail "覆盖镜像 sha256 不符：${ASHA:0:8}…（期望 ${PAYLOAD_SHA:0:8}…）"
fi
[ -e "$DEV" ] || fail "目标设备不存在：$DEV"
[ -e "$REGISTRY" ] || fail "驱动 registry 接口不存在：$REGISTRY"

write_state preparing
TOUCHED=1

# ① 停 lactd —— 它握住 /dev/nvidia* 会让驱动卸不掉（实测会让摘卡阻塞数分钟）
if systemctl is-active --quiet "$LACTD_UNIT" 2>/dev/null; then
  if systemctl stop "$LACTD_UNIT" 2>/dev/null; then
    STOPPED_LACT=1
    log "已停止 $LACTD_UNIT"
  else
    log "警告: $LACTD_UNIT 未能停止"
  fi
fi

# ② 停显示管理器（会中断图形会话）
if [ -n "$DM_UNIT" ] && systemctl is-active --quiet "$DM_UNIT" 2>/dev/null; then
  if systemctl stop "$DM_UNIT" 2>/dev/null; then
    STOPPED_DM=1
    log "已停止显示管理器（$DM_UNIT）"
  else
    log "警告: 显示管理器（$DM_UNIT）未能停止"
  fi
fi
sleep 2

# ③ 卸驱动：每个模块 3 次重试，卸载后必须验证 lsmod 干净
if command -v lsmod >/dev/null 2>&1 && lsmod | grep -qE '^nvidia '; then
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do
      lsmod | grep -qE "^$m " || break
      rmmod "$m" 2>/dev/null
      sleep 1
    done
  done
  if lsmod | grep -qE '^nvidia '; then
    fail '驱动未能完全卸载（可能有进程仍在使用显卡），已回原厂'
  fi
  log '驱动已卸载'
fi
write_state driver_unloaded

# ④ fuser 检查：进程持有设备时内核 nv_pci_remove 会阻塞在内核态数分钟，宁可放弃
if command -v fuser >/dev/null 2>&1; then
  for dv in $NV_DEVS; do
    [ -e "$dv" ] || continue
    HOLDERS=$(fuser "$dv" 2>/dev/null | tr -s ' ')
    if [ -n "${HOLDERS// /}" ]; then
      fail "设备 $dv 被进程占用 (pid:$HOLDERS)，放弃注入并回原厂"
    fi
  done
  log '设备无进程占用，准备摘卡'
else
  log '提示: 未找到 fuser，无法确认设备占用，摘卡可能阻塞数分钟'
fi

# ⑤ 摘卡
write_state card_removed
echo 1 > "$DEV/remove" 2>/dev/null || fail '无法把显卡摘下 PCI 总线（不做重试性摘卡，避免半摘卡）'
log "已摘卡：$BDF"
sleep 1

# ⑥ 加载驱动（此时只 probe 显示卡，覆盖还没写）
modprobe nvidia 2>/dev/null || fail 'modprobe nvidia 失败（摘卡后驱动未加载）'
sleep 1
[ -e "$REGISTRY" ] || fail '驱动 registry 接口不存在（驱动版本不支持覆盖）'

# ⑦ 写覆盖镜像
{ printf 'vbios='; cat "$PAYLOAD"; } > "$REGISTRY" 2>/dev/null || fail '写入驱动 registry 失败'
write_state payload_written
log "覆盖已写入（$FSZ 字节，sha256 ${PAYLOAD_SHA:0:8}…）"

# ⑧ 重新扫描总线：卡带着覆盖上线
write_state rescanning
RESCANNED=0
for i in 1 2 3; do
  echo 1 > /sys/bus/pci/rescan 2>/dev/null
  sleep 2
  [ -e "$DEV" ] && { RESCANNED=1; break; }
done
if [ "$RESCANNED" = "0" ]; then
  RESULT='failed'
  DETAIL='重新扫描总线后显卡未回到总线，请重启；本工具下次启动会提示恢复'
  write_state rescan_failed
  log "★ 危险: $DETAIL"
  exit 1
fi
sleep 3

# ⑨ 校验
write_state verifying
MX=''
for i in 1 2 3 4 5 6; do
  MX=$(nvidia-smi -i "$BDF" --query-gpu=power.max_limit --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
  [ -n "$MX" ] && break
  sleep 2
done
SM=$(nvidia-smi -i "$BDF" --query-gpu=clocks.max.sm --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
VB=$(nvidia-smi -i "$BDF" --query-gpu=vbios_version --format=csv,noheader 2>/dev/null | tr -d ' ')

if [ -z "$MX" ]; then
  RESULT='verify_failed'
  DETAIL='nvidia-smi 未能读回功耗上限，覆盖可能未生效；系统仍然可用'
  log "失败: $DETAIL"
  exit 1
fi

MX_2DP=$(LC_ALL=C printf '%.2f' "$MX" 2>/dev/null)
if [ "$MX_2DP" = "$EXPECT_W_2DP" ]; then
  if [ -n "$EXPECT_W" ]; then
    nvidia-smi -i "$BDF" -pl "$EXPECT_W" >/dev/null 2>&1 && log "已按目标值设置功耗上限 ${EXPECT_W} W"
  fi
  RESULT='ok'
  DETAIL="注入成功：power.max_limit=${MX} W，clocks.max.sm=${SM:-未知} MHz，vbios=${VB:-未知}"
  log "成功: vbios=$VB max_limit=${MX}W max_sm=${SM}MHz"
  if [ -n "$EXPECT_SM" ] && [ "$SM" != "$EXPECT_SM" ]; then
    log "提示: 核心上报上限 ${SM} MHz 与期望 ${EXPECT_SM} MHz 不同，覆盖可能只部分生效"
  fi
else
  RESULT='verify_failed'
  DETAIL="验证不符：power.max_limit=${MX} W（期望 ${EXPECT_W_2DP} W），覆盖未生效但系统可用"
  log "失败: $DETAIL"
fi
exit 0
"""

_RESTORE_BODY = r"""
# ============================ 恢复原厂主流程 ============================
log '=== v100-tui 恢复原厂开始 ==='

if [ "$(id -u)" -ne 0 ]; then
  fail '未以 root 运行，拒绝执行恢复原厂'
fi

if [ ! -e "$DEV" ]; then
  fail "目标设备不存在：$DEV（可先执行 echo 1 > /sys/bus/pci/rescan）"
fi

TOUCHED=1
write_state restoring

# ① 停 lactd 与显示管理器：覆盖只活在模块里，必须先卸载模块
if systemctl is-active --quiet "$LACTD_UNIT" 2>/dev/null; then
  systemctl stop "$LACTD_UNIT" 2>/dev/null && { STOPPED_LACT=1; log "已停止 $LACTD_UNIT"; }
fi
if [ -n "$DM_UNIT" ] && systemctl is-active --quiet "$DM_UNIT" 2>/dev/null; then
  systemctl stop "$DM_UNIT" 2>/dev/null && { STOPPED_DM=1; log "已停止显示管理器（$DM_UNIT）"; }
fi
sleep 2

# ② 卸载模块（各 3 次重试）
if command -v lsmod >/dev/null 2>&1 && lsmod | grep -qE '^nvidia '; then
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do
      lsmod | grep -qE "^$m " || break
      rmmod "$m" 2>/dev/null
      sleep 1
    done
  done
  lsmod | grep -qE '^nvidia ' && fail '驱动未能完全卸载（可能有进程仍在使用显卡）'
  log '驱动已卸载，覆盖随之失效'
fi

# ③ 重新加载驱动（不带覆盖）
modprobe nvidia 2>/dev/null || fail 'modprobe nvidia 失败'
sleep 2

# ④ 可选：把功耗上限写回原厂值（取不到原厂值就跳过）
if [ -n "$FACTORY_W" ]; then
  if nvidia-smi -i "$BDF" -pl "$FACTORY_W" >/dev/null 2>&1; then
    log "已把功耗上限写回原厂值 ${FACTORY_W} W"
  else
    log "提示: 未能把功耗上限写回 ${FACTORY_W} W（可手动 nvidia-smi -i $BDF -pl $FACTORY_W）"
  fi
fi

# ⑤ 校验：不指定原厂值时报当前值，指定时与期望比对
MX=$(nvidia-smi -i "$BDF" --query-gpu=power.max_limit --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
SM=$(nvidia-smi -i "$BDF" --query-gpu=clocks.max.sm --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
if [ -n "$MX" ]; then
  if [ -n "$FACTORY_W" ]; then
    MX_2DP=$(LC_ALL=C printf '%.2f' "$MX" 2>/dev/null)
    FAC_2DP=$(LC_ALL=C printf '%.2f' "$FACTORY_W" 2>/dev/null)
    if [ "$MX_2DP" = "$FAC_2DP" ]; then
      RESULT='ok'
      DETAIL="已回到原厂：power.max_limit=${MX} W，clocks.max.sm=${SM:-未知} MHz"
      log "成功: $DETAIL"
    else
      RESULT='verify_failed'
      DETAIL="恢复后功耗上限仍为 ${MX} W（期望 ${FAC_2DP} W），覆盖可能仍在某个进程里生效，可重启彻底回原厂"
      log "失败: $DETAIL"
    fi
  else
    RESULT='ok'
    DETAIL="覆盖已失效：当前 power.max_limit=${MX} W，clocks.max.sm=${SM:-未知} MHz（未提供原厂值，只做记录）"
    log "成功: $DETAIL"
  fi
else
  RESULT='verify_failed'
  DETAIL='nvidia-smi 未能读回功耗上限，请重启本机确认'
  log "失败: $DETAIL"
fi
exit 0
"""

_COMMON_EPILOGUE = "\n# 主流程结束，on_exit 负责恢复序列与结论落盘。\n"


def _render(
    *,
    kind: str,
    unit: str,
    stamp: str,
    bdf: str,
    payload: str = "",
    payload_sha: str = "",
    payload_size: int = PAYLOAD_SIZE,
    dm_unit: str = "",
    lactd_unit: str = LACTD_UNIT_DEFAULT,
    expect_w: str = "",
    expect_sm: str = "",
    factory_w: str = "",
    paths: Mapping[str, str] | None = None,
) -> str:
    """把模板渲染成最终脚本。所有值都已由调用方校验，可直接内联到单引号变量。"""
    paths = dict(paths or {})
    dev = paths.get("dev") or f"/sys/bus/pci/devices/{bdf}"
    nv_devs = paths.get("nv_devs") or " ".join(NV_DEVICE_NODES)
    registry = paths.get("registry") or REGISTRY_PATH
    run_dir = paths.get("run_dir") or RUN_DIR
    log_dir = paths.get("log_dir") or LOG_DIR
    log_path = f"{log_dir}/{kind}-{stamp}.log"

    for value in (bdf, dm_unit, lactd_unit):
        if value and not _UNIT_RE.match(value):
            raise ValueError(f"拒绝渲染脚本：{value!r} 含非法字符")
    if not _BDF_RE.match(bdf):
        raise ValueError(f"拒绝渲染脚本：设备地址不合法 {bdf!r}")
    for value in (payload, dev, registry, run_dir, log_dir, nv_devs):
        if "'" in value or "\n" in value:
            raise ValueError("拒绝渲染脚本：路径含单引号或换行")

    if kind == "inject":
        body = _INJECT_BODY
        extra = f"#   目标设备 : {bdf}\n#   覆盖镜像 : {payload}\n"
    else:
        body = _RESTORE_BODY
        extra = f"#   目标设备 : {bdf}\n#   用途     : 恢复原厂（不带覆盖重新加载驱动）\n"

    text = _HEADER + body + _COMMON_EPILOGUE
    replacements = {
        "@@UNIT@@": unit,
        "@@GENERATED_AT@@": _iso_now(),
        "@@EXTRA_HEADER@@": extra.rstrip("\n"),
        "@@NV_DEVS@@": nv_devs,
        "@@PAYLOAD@@": payload,
        "@@PAYLOAD_SHA@@": payload_sha,
        "@@PAYLOAD_SIZE@@": str(payload_size),
        "@@BDF@@": bdf,
        "@@DEV@@": dev,
        "@@REGISTRY@@": registry,
        "@@RUN_DIR@@": run_dir,
        "@@LOG_DIR@@": log_dir,
        "@@LOG@@": log_path,
        "@@DM_UNIT@@": dm_unit,
        "@@LACTD_UNIT@@": lactd_unit,
        "@@EXPECT_W@@": expect_w,
        "@@EXPECT_SM@@": expect_sm,
        "@@FACTORY_W@@": factory_w,
    }
    for marker, value in replacements.items():
        text = text.replace(marker, value)
    left = re.findall(r"@@[A-Z_]+@@", text)
    if left:
        raise ValueError(f"脚本模板存在未替换的占位符：{sorted(set(left))}")
    return text


def render_inject_script(
    job: InjectJob,
    *,
    stamp: str,
    unit: str | None = None,
    paths: Mapping[str, str] | None = None,
) -> str:
    """生成注入脚本全文。``paths`` 仅用于测试与诊断时替换系统路径。"""
    return _render(
        kind="inject",
        unit=unit or f"{INJECT_UNIT_PREFIX}-{stamp}",
        stamp=stamp,
        bdf=job.bdf,
        payload=str(job.payload_path),
        payload_sha=job.payload_sha256,
        payload_size=job.payload_size,
        dm_unit=job.dm_unit or guess_display_manager(),
        lactd_unit=job.lactd_unit,
        expect_w=_num(job.power_w),
        expect_sm="" if job.sm_max_mhz is None else str(int(job.sm_max_mhz)),
        paths=paths,
    )


def render_restore_script(
    *,
    bdf: str,
    stamp: str,
    unit: str | None = None,
    factory_power_w: float | None = None,
    dm_unit: str = "",
    lactd_unit: str = LACTD_UNIT_DEFAULT,
    paths: Mapping[str, str] | None = None,
) -> str:
    """生成「恢复原厂」脚本全文（DESIGN §9.2 的确定性步骤）。"""
    return _render(
        kind="restore",
        unit=unit or f"{RESTORE_UNIT_PREFIX}-{stamp}",
        stamp=stamp,
        bdf=bdf,
        dm_unit=dm_unit or guess_display_manager(),
        lactd_unit=lactd_unit,
        factory_w="" if factory_power_w is None else _num(factory_power_w),
        paths=paths,
    )


# ---------------------------------------------------------------------------
# 提交（先拼命令、再提交；dry_run 只拼不提交，供试算与离线测试）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SubmissionPlan:
    """一次提交所需的全部内容：脚本全文 + systemd-run 参数。

    **纯计算结果**：构造它不写盘、不 spawn 任何进程。界面可以据此做试算，
    测试可以据此断言流程（DESIGN §3.2 的单元提交契约），t8 也不必真跑一次注入。
    """

    unit: str
    script_path: Path
    log_path: Path
    script_text: str
    argv: tuple[str, ...]

    @property
    def command_line(self) -> str:
        """可复制到中文提示里的一行命令（仅用于展示）。"""
        return " ".join(self.argv)


def _write_script(path: Path, text: str) -> None:
    """原子写入脚本文件，权限 0700（root 专用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)


def _journal_hint(unit: str, log_path: Path) -> str:
    return (
        f"任务已提交为独立单元 {unit}。图形会话将在数秒后中断，本界面会随之关闭。"
        f"请在 TTY 或 SSH 会话查看进度：journalctl -u {unit} -f，或查看 {log_path}。"
    )


def systemd_run_argv(unit: str, script: Path, *, description: str) -> tuple[str, ...]:
    """systemd-run 的完整参数：``--no-block --collect`` + oneshot + 超时上限。

    ``--no-block`` 保证 TUI 提交后立刻返回；``--collect`` 让单元结束后自动清理，
    避免残留一堆失败单元；``TimeoutStartSec`` 是「摘卡卡死」情况下的最后一道保险。
    """
    return (
        "systemd-run",
        "--no-block",
        "--collect",
        f"--unit={unit}",
        "--property=Type=oneshot",
        f"--property=TimeoutStartSec={INJECT_TIMEOUT_SEC}",
        "--property=TimeoutStopSec=30",
        f"--description={description}",
        "/bin/bash",
        str(script),
    )


def plan_injection(job: InjectJob, *, stamp: str | None = None) -> SubmissionPlan:
    """纯计算：生成注入脚本全文与 systemd-run 参数，不写盘、不执行任何命令。"""
    stamp = stamp or _stamp()
    unit = f"{INJECT_UNIT_PREFIX}-{stamp}"
    script_path = Path(RUN_DIR) / f"inject-{stamp}.sh"
    log_path = Path(LOG_DIR) / f"inject-{stamp}.log"
    text = render_inject_script(job, stamp=stamp, unit=unit)
    argv = systemd_run_argv(
        unit, script_path, description=f"v100-tui 注入 {job.bdf} 功耗 {_num(job.power_w)} W"
    )
    return SubmissionPlan(unit=unit, script_path=script_path, log_path=log_path,
                          script_text=text, argv=argv)


def plan_restore(
    *,
    bdf: str,
    factory_power_w: float | None = None,
    dm_unit: str = "",
    lactd_unit: str = LACTD_UNIT_DEFAULT,
    stamp: str | None = None,
) -> SubmissionPlan:
    """纯计算：生成「恢复原厂」脚本全文与 systemd-run 参数。"""
    stamp = stamp or _stamp()
    unit = f"{RESTORE_UNIT_PREFIX}-{stamp}"
    script_path = Path(RUN_DIR) / f"restore-{stamp}.sh"
    log_path = Path(LOG_DIR) / f"restore-{stamp}.log"
    text = render_restore_script(
        bdf=bdf,
        stamp=stamp,
        unit=unit,
        factory_power_w=factory_power_w,
        dm_unit=dm_unit,
        lactd_unit=lactd_unit,
    )
    argv = systemd_run_argv(unit, script_path, description=f"v100-tui 恢复原厂 {bdf}")
    return SubmissionPlan(unit=unit, script_path=script_path, log_path=log_path,
                          script_text=text, argv=argv)


def submit(job: InjectJob, *, runner: Any = None, dry_run: bool = False) -> JobHandle:
    """把注入提交为独立 systemd 单元；TUI 提交成功即可退出（DESIGN §3.2）。

    ``dry_run=True`` 时只生成脚本与命令并返回（``JobHandle.argv``），
    **不写盘、不提交、不 spawn**：供界面做试算、供测试断言命令内容。
    """
    exec_cmd = runner or run

    problems = validate_job(job)
    if problems:
        return JobHandle(
            ok=False,
            code="E0405",
            message=problems[0],
            advice="请回到「快速修改」重新生成覆盖镜像后再注入。",
        )

    if not dry_run:
        if os.geteuid() != 0:
            return JobHandle(
                ok=False,
                code=C.ERR_INJECT_DISABLED,
                message="注入需要 root 权限。",
                advice="退出本工具后执行 `sudo python3 -m tui`（在仓库根目录）。",
            )
        if not systemd_run_available():
            return JobHandle(
                ok=False,
                code="E0407",
                message="缺少 systemd-run，无法把注入提交为独立单元。",
                advice="本工具不会在界面进程内直接卸载驱动，请安装 systemd 后重试。",
            )

    try:
        plan = plan_injection(job)
    except (OSError, ValueError) as exc:
        return JobHandle(
            ok=False,
            code="E0901",
            message=f"生成注入脚本失败：{exc}",
            advice=f"请换一份覆盖镜像，并确认 {RUN_DIR} 可写且磁盘未满。",
        )

    if dry_run:
        return JobHandle(
            ok=True,
            unit=plan.unit,
            script=plan.script_path,
            log=plan.log_path,
            argv=plan.argv,
            dry_run=True,
            message=f"试算：已生成注入脚本与命令，未提交（单元名将是 {plan.unit}）。",
        )

    with _run_lock() as locked:
        if not locked:
            return JobHandle(
                ok=False,
                code="E0404",
                message="另一个注入任务正在提交或运行。",
                advice="等待其结束后重试，或查看 /run/v100-tui/state.json 确认状态。",
            )
        busy = running_unit()
        if busy:
            return JobHandle(
                ok=False,
                code="E0404",
                message=f"已有注入任务在运行（单元 {busy}）。",
                advice=f"等待其结束：journalctl -u {busy} -f。",
            )

        try:
            _write_script(plan.script_path, plan.script_text)
        except OSError as exc:
            return JobHandle(
                ok=False,
                code="E0901",
                message=f"写入注入脚本失败：{exc}",
                advice=f"请确认 {RUN_DIR} 可写且磁盘未满。",
            )

        # write-ahead：提交前先落一个「准备中」状态，崩溃后能据此判断未完成任务
        placeholder = Path(RUN_DIR) / "state.json"
        try:
            placeholder.write_text(
                json.dumps(
                    {
                        "unit": plan.unit,
                        "bdf": job.bdf,
                        "phase": "preparing",
                        "started_at": _iso_now(),
                        "updated_at": _iso_now(),
                        "payload_sha256": job.payload_sha256,
                        "expected": {
                            "power_max_w": job.power_w,
                            "sm_max_mhz": job.sm_max_mhz,
                        },
                        "stop_lactd": False,
                        "dm_unit": job.dm_unit or guess_display_manager(),
                        "log_path": str(plan.log_path),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # 状态文件写不了不阻断，脚本自己会写

        res = exec_cmd(list(plan.argv), timeout=30)
        if not getattr(res, "ok", False):
            # 提交失败要清掉占位状态，避免下次启动误判「未完成任务」
            current = _read_json(placeholder)
            if current.get("unit") == plan.unit:
                try:
                    placeholder.unlink()
                except OSError:
                    pass
            return JobHandle(
                ok=False,
                code="E0408",
                message=f"提交独立单元失败：{res.tail()}",
                advice="请确认以 root 运行且 systemd 正常（`systemctl is-system-running`）。",
            )

    return JobHandle(
        ok=True,
        unit=plan.unit,
        script=plan.script_path,
        log=plan.log_path,
        submitted_at=_iso_now(),
        argv=plan.argv,
        message=f"注入任务已提交为独立单元 {plan.unit}。",
        journal_hint=_journal_hint(plan.unit, plan.log_path),
    )


def restore_factory_order(
    *,
    bdf: str,
    factory_power_w: float | None = None,
    dm_unit: str = "",
    lactd_unit: str = LACTD_UNIT_DEFAULT,
    runner: Any = None,
    dry_run: bool = False,
) -> JobHandle:
    """按 DESIGN §9.2 提交「恢复原厂」独立单元：卸载模块 → 重新加载 → 恢复服务。"""
    exec_cmd = runner or run
    if not _BDF_RE.match(bdf or ""):
        return JobHandle(
            ok=False, code="E0401", message=f"设备地址不合法：{bdf!r}", advice="请重新选择显卡。"
        )

    if not dry_run:
        if os.geteuid() != 0:
            return JobHandle(
                ok=False,
                code=C.ERR_NOT_ROOT,
                message="恢复原厂需要 root 权限。",
                advice="退出本工具后执行 `sudo python3 -m tui`（在仓库根目录）。",
            )
        if not systemd_run_available():
            return JobHandle(
                ok=False,
                code="E0407",
                message="缺少 systemd-run，无法把恢复动作提交为独立单元。",
                advice="请安装 systemd 后重试，或在 TTY 中手动执行 §9.2 的步骤。",
            )

    try:
        plan = plan_restore(
            bdf=bdf,
            factory_power_w=factory_power_w,
            dm_unit=dm_unit,
            lactd_unit=lactd_unit,
        )
    except (OSError, ValueError) as exc:
        return JobHandle(
            ok=False,
            code="E0901",
            message=f"生成恢复脚本失败：{exc}",
            advice=f"请确认 {RUN_DIR} 可写。",
        )

    if dry_run:
        return JobHandle(
            ok=True,
            unit=plan.unit,
            script=plan.script_path,
            log=plan.log_path,
            argv=plan.argv,
            dry_run=True,
            message=f"试算：已生成恢复原厂脚本与命令，未提交（单元名将是 {plan.unit}）。",
        )

    try:
        _write_script(plan.script_path, plan.script_text)
    except OSError as exc:
        return JobHandle(
            ok=False,
            code="E0901",
            message=f"写入恢复脚本失败：{exc}",
            advice=f"请确认 {RUN_DIR} 可写。",
        )

    res = exec_cmd(list(plan.argv), timeout=30)
    if not getattr(res, "ok", False):
        return JobHandle(
            ok=False,
            code="E0409",
            message=f"提交恢复单元失败：{res.tail()}",
            advice="请确认以 root 运行且 systemd 正常。",
        )
    return JobHandle(
        ok=True,
        unit=plan.unit,
        script=plan.script_path,
        log=plan.log_path,
        submitted_at=_iso_now(),
        argv=plan.argv,
        message=f"恢复原厂任务已提交为独立单元 {plan.unit}。",
        journal_hint=_journal_hint(plan.unit, plan.log_path),
    )


# ---------------------------------------------------------------------------
# 状态读取与危险态判定
# ---------------------------------------------------------------------------
def read_job_state(*, bdf: str | None = None) -> JobState:
    """合并 state.json 与 last-job.json，并按 §9.4 判定是否陈旧。"""
    run_dir = Path(RUN_DIR)
    state_raw = _read_json(run_dir / "state.json")
    last_raw = _read_json(run_dir / "last-job.json")

    target_bdf = bdf or str(state_raw.get("bdf") or last_raw.get("bdf") or "")
    device_present = False
    if target_bdf and _BDF_RE.match(target_bdf):
        device_present = (Path(SYSFS_PCI_DEVICES) / target_bdf).is_dir()

    expected = state_raw.get("expected")
    expected = expected if isinstance(expected, dict) else {}
    power = expected.get("power_max_w")
    sm = expected.get("sm_max_mhz")

    started_raw = str(state_raw.get("started_at") or "")
    stale = False
    started_epoch = _parse_iso(started_raw)
    boot = _boot_epoch()
    if started_epoch is not None and boot is not None:
        # 早于本次开机 60 秒以上视为上一次开机的残留
        stale = started_epoch < (boot - 60)

    return JobState(
        phase=str(state_raw.get("phase") or "unknown"),
        unit=str(state_raw.get("unit") or last_raw.get("unit") or ""),
        bdf=target_bdf,
        started_at=started_raw,
        updated_at=str(state_raw.get("updated_at") or ""),
        payload_sha256=str(state_raw.get("payload_sha256") or last_raw.get("payload_sha256") or ""),
        expected_power_w=float(power) if isinstance(power, (int, float)) else None,
        expected_sm_mhz=int(sm) if isinstance(sm, int) else None,
        log_path=str(state_raw.get("log_path") or last_raw.get("log_path") or ""),
        device_present=device_present,
        stale=stale,
        result=str(last_raw.get("result") or ""),
        detail=str(last_raw.get("detail") or ""),
        finished_at=str(last_raw.get("finished_at") or ""),
        raw={**state_raw, "last": last_raw},
    )


def last_result() -> dict[str, Any]:
    """上次任务的最终结论（供启动页展示）；无记录返回空字典。"""
    return _read_json(Path(RUN_DIR) / "last-job.json")


def resume_hint(state: JobState | None = None) -> Hint | None:
    """启动时的危险态判定（DESIGN §9.4 表格）。None 表示无残留，可正常进入。"""
    st = state if state is not None else read_job_state()
    phase = st.phase

    if phase in _TERMINAL_OK_PHASES or phase in ("", "unknown"):
        return None

    if st.stale:
        return Hint(
            danger=False,
            title="上次注入状态已过期",
            detail=(
                f"运行期状态记录于本次开机之前（阶段 {phase}），"
                "可以视为已随重启清除。"
            ),
            actions=("continue", "view_log"),
            advice="直接继续操作即可；如需彻底确认，请查看当时的日志。",
        )

    if phase == "failed":
        if st.device_present:
            return None  # 卡在总线上 → 无残留，界面只需展示上次失败摘要
        return Hint(
            danger=True,
            title="显卡可能不在总线上",
            detail=(
                f"上次注入以失败结束，且 {st.bdf or '目标设备'} 当前不在 /sys/bus/pci/devices/ 下。"
            ),
            actions=("restore_factory", "view_log"),
            advice="执行「恢复原厂」，它会重新扫描总线把显卡放回；若仍不出现请重启本机。",
            code="E0410",
        )

    if phase == "rescan_failed":
        return Hint(
            danger=True,
            title="重新扫描总线失败",
            detail="上次注入在重新扫描总线（rescan）阶段失败，显卡可能未被放回总线。",
            actions=("restore_factory", "view_log"),
            advice=(
                "执行「恢复原厂」，它会执行 `echo 1 > /sys/bus/pci/rescan` 并重新加载驱动；"
                "若仍失败请重启本机。"
            ),
            code="E0410",
        )

    if phase in _DANGEROUS_PHASES:
        if st.device_present:
            return Hint(
                danger=False,
                title="上次注入未完成，但显卡已回到总线",
                detail=f"上次任务停在阶段 {phase}，{st.bdf} 目前存在于总线。",
                actions=("continue", "view_log"),
                advice="可以继续操作；若显卡行为异常，先执行「恢复原厂」。",
            )
        return Hint(
            danger=True,
            title="检测到上次注入未完成，显卡可能不在总线上",
            detail=(
                f"上次任务停在阶段 {phase}，且 {st.bdf or '目标设备'} 当前不在 /sys/bus/pci/devices/ 下。"
                "这就是「半摘卡」状态：驱动已卸载、卡还没放回总线。"
            ),
            actions=("restore_factory", "view_log"),
            advice=(
                "执行「恢复原厂」（等价于 `echo 1 > /sys/bus/pci/rescan` 后重新加载驱动并拉起服务）；"
                "若显卡仍不出现，请重启本机。"
            ),
            code="E0410",
        )

    return Hint(
        danger=False,
        title="上次注入状态未知",
        detail=f"运行期状态文件中的阶段为 {phase}。",
        actions=("continue", "view_log"),
        advice="如显卡工作异常，先执行「恢复原厂」。",
    )
