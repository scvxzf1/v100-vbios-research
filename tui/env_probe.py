"""平台与能力探测（只读）。

本模块只做**查询**：不装卸模块、不写 registry、不启停服务。
唯一允许的写入由 `rom_source.load_rom(allow_sys_enable=True)` 负责（sysfs 使能位）。

对外接口（tui/DESIGN.md §4）：

* ``probe(readonly=False) -> EnvInfo``
* ``run(argv, timeout) -> RunResult``：全项目统一的外部命令包装
* ``platform_gate() -> (bool, str)``：仅 Linux 校验
* 路径解析：``user_home()`` / ``config_dir()`` / ``cache_dir()`` / ``profiles_dir()`` /
  ``payload_cache_dir()`` / ``history_path()``
* ``chown_to_invoker(path)``：以 sudo 运行后把文件属主归还给调用者

所有读取失败一律降级为「未知」并记入 ``EnvInfo.reasons``，**绝不抛异常**。
"""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import constants as C

LINUX_ONLY_MESSAGE = (
    "本工具仅支持 Linux（当前系统为 {platform}）。"
    "VBIOS 内存覆盖依赖 Linux 驱动提供的 /proc/driver/nvidia/registry 接口，"
    "其他系统无法使用。"
)


# ─────────────────────────────────────────────────────────────────────────────
# 外部命令统一包装
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    """一次外部命令的结果；失败也是数据，不是异常。"""

    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(argv, timeout=None):
    """执行外部命令并捕获一切异常。

    参数 ``argv`` 为 list（不经过 shell，避免注入）；超时返回 ``rc=124``，
    可执行文件缺失返回 ``rc=127``。调用方据此降级，不需要 try/except。
    """
    if isinstance(argv, (str, os.PathLike)):
        argv = [str(argv)]
    argv = [str(a) for a in argv]
    to = C.TIMEOUT_SMI_S if timeout is None else timeout
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=to,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return RunResult(124, "", f"命令超时（{to} 秒）：{' '.join(argv)}")
    except FileNotFoundError:
        return RunResult(127, "", f"命令不存在：{argv[0]}")
    except OSError as exc:                       # 权限不足等
        return RunResult(126, "", f"命令无法执行（{argv[0]}）：{exc}")
    return RunResult(p.returncode, p.stdout or "", p.stderr or "")


# ─────────────────────────────────────────────────────────────────────────────
# 平台闸门
# ─────────────────────────────────────────────────────────────────────────────
def platform_name() -> str:
    return sys.platform or "未知"


def platform_gate():
    """返回 ``(是否可用, 中文说明)``；非 Linux 一律不可用。"""
    if sys.platform.startswith("linux"):
        return True, ""
    return False, LINUX_ONLY_MESSAGE.format(platform=platform_name())


def is_linux() -> bool:
    return platform_gate()[0]


# ─────────────────────────────────────────────────────────────────────────────
# XDG / 用户路径解析
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class UserAccount:
    uid: int
    gid: int
    home: Path
    name: str


def invoker_account():
    """以 sudo 运行时返回真实调用者账号，否则返回 ``None``。

    用途：避免把档案、历史、覆盖镜像写进提权用户的 home（tui/DESIGN.md §5.1）。
    """
    name = os.environ.get("SUDO_USER") or ""
    if not name or name == "root":
        return None
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None
    home = Path(pw.pw_dir) if pw.pw_dir else Path(os.path.expanduser("~"))
    return UserAccount(pw.pw_uid, pw.pw_gid, home, name)


def is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:          # 非 POSIX 平台
        return False


def current_user() -> str:
    acct = invoker_account()
    if acct is not None:
        return acct.name
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, AttributeError):
        return os.environ.get("USER", "未知")


def user_home() -> Path:
    """调用者的 home；以 sudo 运行时取 SUDO_USER 的 home。"""
    acct = invoker_account()
    if acct is not None:
        return acct.home
    return Path(os.path.expanduser("~"))


def config_home() -> Path:
    value = os.environ.get(C.XDG_CONFIG_HOME)
    return Path(value) if value else user_home() / ".config"


def cache_home() -> Path:
    value = os.environ.get(C.XDG_CACHE_HOME)
    return Path(value) if value else user_home() / ".cache"


def config_dir() -> Path:
    return config_home() / C.APP_DIR_NAME


def cache_dir() -> Path:
    return cache_home() / C.APP_DIR_NAME


def profiles_dir() -> Path:
    return config_dir() / C.PROFILES_SUBDIR


def payload_cache_dir() -> Path:
    return cache_dir() / C.PAYLOADS_SUBDIR


def history_path() -> Path:
    return config_dir() / C.HISTORY_FILENAME


def legacy_profiles_path() -> Path:
    return config_dir() / C.LEGACY_PROFILES_FILENAME


def ensure_dir(path: Path, mode: int = 0o755) -> Path:
    """创建目录（含父目录）并把属主归还给调用者；失败只尝试、不抛。"""
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
    except OSError:
        pass
    chown_to_invoker(path)
    return path


def chown_to_invoker(path: Path) -> None:
    """仅当以 root 且存在 SUDO_USER 时才 chown；其余情况静默跳过。"""
    acct = invoker_account()
    if acct is None or not is_root():
        return
    try:
        os.chown(Path(path), acct.uid, acct.gid)
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# systemd 查询（只读）
# ─────────────────────────────────────────────────────────────────────────────
def systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def unit_is_active(unit: str):
    """返回 ``True``/``False``/``None``（未知）。"""
    if not systemctl_available():
        return None
    res = run(["systemctl", "is-active", unit], timeout=C.TIMEOUT_SYSTEMCTL_S)
    text = (res.out or res.err).strip()
    if res.rc == 0 or text == "active":
        return True
    if text in ("inactive", "failed", "unknown", "dead"):
        return False
    return None


def unit_is_enabled(unit: str):
    """返回 ``True``/``False``/``None``（未知）。"""
    if not systemctl_available():
        return None
    res = run(["systemctl", "is-enabled", unit], timeout=C.TIMEOUT_SYSTEMCTL_S)
    text = (res.out or "").strip()
    if text in ("enabled", "enabled-runtime", "static", "indirect"):
        return True
    if text in ("disabled", "masked", "not-found", ""):
        return False
    return res.rc == 0


def detect_display_manager():
    """返回 ``(单元名或 None, 是否 active)``。"""
    for unit in C.DISPLAY_MANAGER_UNITS:
        active = unit_is_active(unit)
        if active:
            return unit, True
    if not systemctl_available():
        return None, False
    # 都没 active 时，尽量给出「装了哪个」，便于界面提示
    for unit in C.DISPLAY_MANAGER_UNITS:
        if unit_is_enabled(unit):
            return unit, False
    return None, False


def registry_available() -> bool:
    """驱动 registry 接口是否存在且可读。"""
    return os.path.exists(C.REGISTRY_PATH) and os.access(C.REGISTRY_PATH, os.R_OK)


def systemd_run_available() -> bool:
    return shutil.which("systemd-run") is not None


# ─────────────────────────────────────────────────────────────────────────────
# 汇总探测
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class EnvInfo:
    """一次启动所需的全部环境事实。字段缺失为 ``None``/``False``，不含猜测。"""

    platform_ok: bool
    platform: str
    is_root: bool
    readonly: bool
    registry_ok: bool
    systemd_run_ok: bool
    dm_unit: str = None
    dm_active: bool = False
    lactd_active: bool = None
    reasons: list = field(default_factory=list)
    term_cols: int = 0
    term_rows: int = 0
    user: str = ""

    # ── 能力判定：禁用时必须能回答「为什么」与「怎么恢复」──────────────
    def disable_reason(self, capability: str):
        """返回禁用原因（中文）；可用时返回 ``None``。"""
        if not self.platform_ok:
            return f"本工具仅支持 Linux（当前系统为 {self.platform}）。"
        if self.readonly:
            return "当前以 --readonly 启动，所有需要改动系统的入口已禁用。"
        if capability in ("inject", "persist", "restore"):
            if not self.is_root:
                return "需要 root 权限。请退出后执行 `sudo python3 -m tui`。"
        if capability in ("inject", "restore"):
            if not self.registry_ok:
                return ("驱动 registry 接口不可用（/proc/driver/nvidia/registry 不存在）。"
                        "请确认 NVIDIA 驱动已加载且版本支持该接口。")
            if not self.systemd_run_ok:
                return ("缺少 systemd-run，无法把注入提交为独立任务。"
                        "本工具绝不在界面进程内直接卸载驱动。")
        return None

    # ── 常用组合判定 ────────────────────────────────────────────────
    @property
    def can_edit(self) -> bool:
        """生成覆盖镜像是纯本地运算，任何权限下都可用。"""
        return self.platform_ok

    @property
    def can_inject(self) -> bool:
        return self.disable_reason("inject") is None

    @property
    def can_persist(self) -> bool:
        return self.disable_reason("persist") is None

    @property
    def can_restore(self) -> bool:
        return self.disable_reason("restore") is None

    @property
    def banner(self) -> str:
        if not self.platform_ok:
            return f"不支持的平台（{self.platform}）"
        if self.readonly:
            return "只读模式（--readonly）"
        if not self.is_root:
            return "只读模式（未使用 sudo）"
        return "完整功能（root）"


def probe(readonly: bool = False) -> EnvInfo:
    """探测当前环境。**任何失败都不抛异常**，只在 ``reasons`` 里留下事实。"""
    ok, why = platform_gate()
    reasons = []
    if why:
        reasons.append(why)

    info = EnvInfo(
        platform_ok=ok,
        platform=platform_name(),
        is_root=is_root(),
        readonly=bool(readonly),
        registry_ok=False,
        systemd_run_ok=False,
    )
    info.user = current_user()

    if ok:
        info.registry_ok = registry_available()
        if not info.registry_ok:
            reasons.append("未发现 /proc/driver/nvidia/registry，注入与恢复原厂不可用。")
        info.systemd_run_ok = systemd_run_available()
        if not info.systemd_run_ok:
            reasons.append("未发现 systemd-run，注入任务无法安全提交。")
        info.dm_unit, info.dm_active = detect_display_manager()
        info.lactd_active = unit_is_active(C.LACTD_UNIT)
        if not info.is_root:
            reasons.append("未以 root 运行：注入、持久化、恢复原厂入口已禁用。")

    try:
        size = os.get_terminal_size()
        info.term_cols, info.term_rows = size.columns, size.lines
    except OSError:
        info.term_cols, info.term_rows = 80, 24

    if invoker_account() is None and is_root():
        reasons.append("以 root 直接运行（无 SUDO_USER），配置与历史将写入提权用户目录。")

    info.reasons = reasons
    return info


def sudo_owner_note():
    """sudo 场景下无法解析真实调用者时的中文提示（供界面展示）。"""
    if is_root() and invoker_account() is None and os.environ.get("SUDO_USER"):
        return "提示[W0102] 无法解析 SUDO_USER 的账号信息，配置将写入当前提权用户目录。"
    return None
