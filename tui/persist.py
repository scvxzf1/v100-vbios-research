"""开机持久化执行层（t6）。

设计依据：``tui/DESIGN.md`` §3.3-C「开机持久化」、§9.3(3)「deploy/ 的接口契约」。

原则：**复用，不复制**。

* 安装只调用 ``deploy/install.sh``，卸载只调用 ``deploy/uninstall.sh``；
  本模块不重新实现单元内容，也不自己写一套注入流程（否则必然与既有脚本漂移）。
* 安装顺序是硬性要求：先让 ``install.sh`` 完成 ``MANIFEST.sha256`` 完整性校验并安装，
  **再**用档案的覆盖镜像替换 ``/etc/v100/v100_final_payload.bin``。
* 任意档案（非 350 W）的持久化依赖 ``deploy/v100-boot-patch.sh`` 的
  ``V100_EXPECT_W`` 参数化：脚本支持时用 systemd drop-in
  ``/etc/systemd/system/v100-boot-patch.service.d/10-v100-tui.conf`` 覆盖期望值；
  不支持时必须降级为「仅标准 350 W」并返回 ``错误[E0504]``。
* 全程不读密码、不写密码、不自行提权，只要求用户以 ``sudo`` 运行。

文案约定同 ``tui/inject.py``：错误码对齐 DESIGN §7.2 分区（E05xx 开机持久化），
``message`` / ``detail`` 是给日志与降级展示用的中文事实描述。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import constants as C
from . import env_probe as probe
from .inject import JobState, RunResult, read_job_state, run

__all__ = [
    "PersistState",
    "PersistStep",
    "PersistResult",
    "PersistTarget",
    "PersistPlan",
    "STATE_UNIT",
    "DROPIN_PATH",
    "INSTALLED_PAYLOAD",
    "SOURCE_RECORD",
    "BOOT_LOG",
    "STANDARD_POWER_W",
    "deploy_dir",
    "supports_expect_param",
    "standard_payload_path",
    "state",
    "install_plan",
    "preflight_install",
    "install",
    "uninstall",
    "make_target",
    "target_from_profile_dict",
    "lact_power_caps",
    "conflict_notes",
]

# 系统路径一律引用 tui/constants.py（DESIGN §5.1 / §9.3），本模块不重复定义
STATE_UNIT = C.BOOT_UNIT
UNIT_FILE = Path("/etc/systemd/system") / STATE_UNIT
DROPIN_DIR = Path(C.BOOT_DROPIN_DIR)
DROPIN_PATH = DROPIN_DIR / "10-v100-tui.conf"
INSTALLED_PAYLOAD = Path(C.BOOT_PAYLOAD_PATH)
SOURCE_RECORD = Path(C.ETC_V100_DIR) / "v100-tui-source.json"
BOOT_LOG = Path(C.BOOT_LOG_PATH)
LACT_CONFIG = Path("/etc/lact/config.yaml")
SCRIPT_INSTALLED = Path("/usr/local/sbin/v100-boot-patch.sh")

# 标准档案：参数化未落地时唯一允许持久化的功耗
STANDARD_POWER_W = C.POWER_MAX_W

_PAYLOAD_SIZE = C.PAYLOAD_WINDOW_LENGTH
_ENV_LINE_RE = re.compile(
    r"^\s*Environment\s*=\s*\"?" + re.escape(C.BOOT_EXPECT_ENV_KEY) + r"=([0-9]+(?:\.[0-9]+)?)\"?\s*$"
)
_POWER_CAP_RE = re.compile(r"power_cap\s*:\s*([0-9]+(?:\.[0-9]+)?)")


def deploy_dir(repo_root: Path | None = None) -> Path:
    """``deploy/`` 目录：默认按本文件位置推仓库根，绝不硬编码个人路径。"""
    if repo_root is not None:
        return Path(repo_root) / "deploy"
    return Path(__file__).resolve().parent.parent / "deploy"


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 目标与结果
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PersistTarget:
    """要持久化的档案语义值：功耗 + 覆盖镜像（不含任何偏移）。"""

    payload_path: Path
    power_w: float
    payload_sha256: str = ""
    slug: str = ""
    display_name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload_path", Path(self.payload_path))
        object.__setattr__(self, "power_w", float(self.power_w))
        if not self.payload_sha256:
            object.__setattr__(self, "payload_sha256", _sha256_file(self.payload_path))


def make_target(
    payload_path: str | Path,
    power_w: float,
    *,
    payload_sha256: str = "",
    slug: str = "",
    display_name: str = "",
) -> PersistTarget:
    return PersistTarget(
        payload_path=Path(payload_path),
        power_w=power_w,
        payload_sha256=payload_sha256,
        slug=slug,
        display_name=display_name,
    )


def target_from_profile_dict(
    profile: Mapping[str, Any], *, payload_dir: str | Path | None = None
) -> PersistTarget:
    """按 DESIGN §5.2 的档案 JSON 构造目标（``payload.cache_name`` 是相对文件名）。"""
    payload_meta = profile.get("payload") or {}
    power_meta = profile.get("power") or {}
    cache_name = str(payload_meta.get("cache_name") or "")
    if not cache_name:
        raise ValueError("档案缺少 payload.cache_name，无法定位覆盖镜像")
    if payload_dir is None:
        raise ValueError("需要给出覆盖镜像缓存目录才能解析 payload.cache_name")
    path = Path(payload_dir) / cache_name
    return PersistTarget(
        payload_path=path,
        power_w=float(power_meta.get("limit_w") or 0.0),
        payload_sha256=str(payload_meta.get("sha256") or ""),
        slug=str(profile.get("slug") or ""),
        display_name=str(profile.get("display_name") or ""),
    )


@dataclass(frozen=True)
class PersistStep:
    """安装/卸载过程中的单步结果。"""

    title: str
    ok: bool
    detail: str = ""
    code: str = ""


@dataclass(frozen=True)
class PersistState:
    """开机持久化现状（只读探测结果）。"""

    unit_present: bool = False
    enabled: str = ""
    active: str = ""
    payload_present: bool = False
    payload_sha256: str = ""
    payload_size: int | None = None
    dropin_present: bool = False
    expected_power_w: float | None = None  # None 表示未覆盖，等同 350 W
    param_supported: bool = False
    source_slug: str = ""
    source_sha256: str = ""
    boot_log_last: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def installed(self) -> bool:
        return self.unit_present

    @property
    def effective_power_w(self) -> float:
        return self.expected_power_w if self.expected_power_w is not None else STANDARD_POWER_W


@dataclass(frozen=True)
class PersistPlan:
    """二次确认页需要展示的路径清单（DESIGN §6.3）。"""

    files_to_write: tuple[str, ...] = ()
    files_to_remove: tuple[str, ...] = ()
    expected_power_w: float = STANDARD_POWER_W
    overwrite_payload: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PersistResult:
    """安装/卸载结果：ok + 分步明细 + 可读中文结论。"""

    ok: bool
    action: str = ""
    steps: tuple[PersistStep, ...] = field(default_factory=tuple)
    code: str = ""
    message: str = ""
    advice: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)
    state: PersistState | None = None


# ---------------------------------------------------------------------------
# 只读探测
# ---------------------------------------------------------------------------
def _systemctl(prop: str, unit: str = STATE_UNIT, *, timeout: float = 10.0) -> str:
    """读 systemctl 的结果串。is-enabled / is-active 对「未启用 / 未运行」会返回非零退出码，
    但真实状态仍写在 stdout（disabled / inactive），所以不能按退出码丢弃 stdout。"""
    res = run(["systemctl", prop, unit], timeout=timeout)
    lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    if lines:
        return lines[0]
    return "not-found" if res.returncode != 0 else ""


def _dropin_power(path: Path = DROPIN_PATH) -> float | None:
    """读取 drop-in 里的 Environment=V100_EXPECT_W=…；没有则 None。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        match = _ENV_LINE_RE.match(line)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return None
    return None


def _last_log_line(path: Path = BOOT_LOG) -> str:
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return ""
    for line in reversed(lines):
        if line:
            return line
    return ""


def supports_expect_param(repo_root: Path | None = None) -> bool:
    """deploy 脚本是否已支持 V100_EXPECT_W（决定能否持久化非 350 W 档案）。"""
    script = deploy_dir(repo_root) / "v100-boot-patch.sh"
    try:
        text = script.read_text(encoding="utf-8")
    except OSError:
        return False
    return C.BOOT_EXPECT_ENV_KEY in text


def standard_payload_path(repo_root: Path | None = None) -> Path:
    """仓库自带的 350 W 标准覆盖镜像（deploy/install.sh 安装的那一份）。"""
    return deploy_dir(repo_root).parent / "firmware" / "v100_payload_350w.bin"


def lact_power_caps(path: Path = LACT_CONFIG) -> list[float]:
    """粗略扫描 LACT 配置里的 power_cap（只读，不解析完整 YAML，不代改）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    caps: list[float] = []
    for line in text.splitlines():
        match = _POWER_CAP_RE.search(line)
        if match:
            try:
                caps.append(float(match.group(1)))
            except ValueError:
                continue
    return caps


def conflict_notes(
    expected_power_w: float,
    *,
    observed_max_limit_w: float | None = None,
    caps: Sequence[float] | None = None,
) -> list[str]:
    """互斥/冲突提示（只提示，不代改用户配置）。"""
    notes: list[str] = []
    if observed_max_limit_w is not None and abs(observed_max_limit_w - 300.0) > 0.01:
        notes.append(
            f"检测到 flash 可能已被修改（当前驱动上报上限 {observed_max_limit_w:g} W，原厂应为 300 W），"
            "建议不要叠加开机持久化。"
        )
    for cap in caps if caps is not None else lact_power_caps():
        if abs(cap - expected_power_w) > 0.01:
            notes.append(
                f"LACT 配置的 power_cap={cap:g} W 会把功耗改回，"
                f"建议在 LACT 中把 V100 条目的 power_cap 固定为 {expected_power_w:g} W。"
            )
    return notes


def state(*, repo_root: Path | None = None) -> PersistState:
    """探测持久化现状：单元状态、已装 payload、drop-in 期望值、日志末行。"""
    unit_present = UNIT_FILE.is_file()
    payload_present = INSTALLED_PAYLOAD.is_file()
    payload_size: int | None = None
    payload_sha = ""
    if payload_present:
        try:
            payload_size = INSTALLED_PAYLOAD.stat().st_size
        except OSError:
            payload_size = None
        payload_sha = _sha256_file(INSTALLED_PAYLOAD)

    source_slug = ""
    source_sha = ""
    try:
        record = json.loads(SOURCE_RECORD.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            source_slug = str(record.get("slug") or "")
            source_sha = str(record.get("payload_sha256") or "")
    except (OSError, ValueError, json.JSONDecodeError):
        pass

    notes: list[str] = []
    param_ok = supports_expect_param(repo_root)
    if not param_ok:
        notes.append(
            "deploy/v100-boot-patch.sh 尚未支持 V100_EXPECT_W 参数化，"
            "开机持久化只能安装标准 350 W 档案。"
        )

    dropin_present = DROPIN_PATH.is_file()
    expected_power = _dropin_power(DROPIN_PATH) if dropin_present else None
    if not param_ok and expected_power is not None and abs(expected_power - STANDARD_POWER_W) > 0.01:
        notes.append(
            f"存在 drop-in 期望值 {expected_power:g} W，但脚本不支持参数化，"
            "该值不会生效。"
        )

    return PersistState(
        unit_present=unit_present,
        enabled=_systemctl("is-enabled") if unit_present else "not-found",
        active=_systemctl("is-active") if unit_present else "inactive",
        payload_present=payload_present,
        payload_sha256=payload_sha,
        payload_size=payload_size,
        dropin_present=dropin_present,
        expected_power_w=expected_power,
        param_supported=param_ok,
        source_slug=source_slug,
        source_sha256=source_sha,
        boot_log_last=_last_log_line(),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# 安装/卸载计划（供二次确认页展示）
# ---------------------------------------------------------------------------
def install_plan(
    target: PersistTarget, *, repo_root: Path | None = None, observed_max_limit_w: float | None = None
) -> PersistPlan:
    """列出将写入的具体路径，供「键入 安装」确认页展示。"""
    files = [str(UNIT_FILE), str(SCRIPT_INSTALLED), str(INSTALLED_PAYLOAD)]
    overwrite = _sha256_file(target.payload_path) != _sha256_file(standard_payload_path(repo_root))
    if overwrite:
        files.append(f"{INSTALLED_PAYLOAD}（用档案的覆盖镜像替换）")
    if abs(target.power_w - STANDARD_POWER_W) > 0.01:
        files.append(f"{DROPIN_PATH}（Environment={C.BOOT_EXPECT_ENV_KEY}={target.power_w:g}）")
    files.append(str(SOURCE_RECORD))
    return PersistPlan(
        files_to_write=tuple(files),
        files_to_remove=(),
        expected_power_w=target.power_w,
        overwrite_payload=overwrite,
        notes=tuple(
            ["会先运行 deploy/install.sh 的 MANIFEST.sha256 完整性校验，再替换覆盖镜像。"]
            + conflict_notes(target.power_w, observed_max_limit_w=observed_max_limit_w)
        ),
    )


# ---------------------------------------------------------------------------
# 互斥：等待正在运行的注入单元结束（DESIGN §9.5，最多 30 秒）
# ---------------------------------------------------------------------------
def _wait_for_idle_inject(timeout: float = 30.0) -> tuple[bool, str]:
    """等待正在运行的注入单元结束（DESIGN §9.5：最多 30 秒）。"""
    deadline = time.monotonic() + timeout
    state_now: JobState = read_job_state()
    while state_now.unit and time.monotonic() < deadline:
        res = run(["systemctl", "is-active", "--quiet", state_now.unit], timeout=10)
        if res.returncode != 0:
            return True, ""
        time.sleep(1.0)
        state_now = read_job_state()
    if state_now.unit:
        res = run(["systemctl", "is-active", "--quiet", state_now.unit], timeout=10)
        if res.returncode == 0:
            return False, state_now.unit
    return True, ""


def _replace_file(src: Path, dst: Path, mode: int = 0o644) -> None:
    """原子替换目标文件（tmp + os.replace），避免出现半截 payload。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".v100-tui.tmp")
    shutil.copyfile(src, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)


def _deploy_script(name: str, repo_root: Path | None = None) -> Path:
    return deploy_dir(repo_root) / name


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
def preflight_install(
    target: PersistTarget, *, repo_root: Path | None = None
) -> tuple[tuple[PersistStep, ...], PersistResult | None]:
    """安装前的纯校验：不写盘、不需要 root。返回（已通过步骤，失败结果或 None）。

    单独暴露是为了让界面在二次确认页之前就能给出可读原因，也便于离线测试
    「参数化未落地 → 仅标准 350 W」这条降级路径（DESIGN §9.3(3)）。
    """
    steps: list[PersistStep] = []
    payload_path = Path(target.payload_path)
    if not payload_path.is_file():
        return (), PersistResult(
            ok=False,
            action="install",
            code="E0505",
            message=f"覆盖镜像不存在：{payload_path}",
            advice="请回到「快速修改」重新生成覆盖镜像。",
        )
    try:
        size = payload_path.stat().st_size
    except OSError as exc:
        return (), PersistResult(
            ok=False, action="install", code="E0505", message=f"无法读取覆盖镜像：{exc}"
        )
    if size != _PAYLOAD_SIZE:
        return (), PersistResult(
            ok=False,
            action="install",
            code="E0505",
            message=f"覆盖镜像尺寸不符：{size} 字节（期望 {_PAYLOAD_SIZE} 字节）",
            advice="请重新生成覆盖镜像后再安装。",
        )
    actual_sha = _sha256_file(payload_path)
    if target.payload_sha256 and actual_sha != target.payload_sha256:
        return (), PersistResult(
            ok=False,
            action="install",
            code="E0505",
            message=(
                f"覆盖镜像 sha256 不符：{actual_sha[:8]}…（档案记录 {target.payload_sha256[:8]}…）"
            ),
            advice="档案记录的镜像已被改动，请重新生成后再安装。",
        )
    steps.append(PersistStep("校验覆盖镜像", True, f"{size} 字节，sha256 {actual_sha[:8]}…"))

    param_ok = supports_expect_param(repo_root)
    if abs(target.power_w - STANDARD_POWER_W) > 0.01 and not param_ok:
        return tuple(steps), PersistResult(
            ok=False,
            action="install",
            code="E0504",
            message=(
                f"deploy/v100-boot-patch.sh 尚未参数化，只能安装标准 {STANDARD_POWER_W:g} W 档案；"
                f"本次档案为 {target.power_w:g} W。"
            ),
            advice=(
                "改用功耗 350 W 的档案，或等 deploy 脚本支持 V100_EXPECT_W 后再安装；"
                "本工具不会另写一套注入流程。"
            ),
            steps=tuple(steps),
        )
    steps.append(
        PersistStep(
            "参数化支持检查",
            True,
            f"脚本支持 V100_EXPECT_W，可写入期望值 {target.power_w:g} W"
            if param_ok
            else "脚本未参数化，按标准 350 W 安装",
        )
    )
    return tuple(steps), None


def install(target: PersistTarget, *, repo_root: Path | None = None) -> PersistResult:
    """安装开机持久化：``deploy/install.sh`` → 替换覆盖镜像 → 写 drop-in → 记录来源。"""
    steps: list[PersistStep] = []

    if not probe.is_root():
        return PersistResult(
            ok=False,
            action="install",
            code=C.ERR_NOT_ROOT,
            message="安装开机持久化需要 root 权限。",
            advice="退出本工具后执行 `sudo python3 -m tui`（在仓库根目录）。",
        )

    idle, busy_unit = _wait_for_idle_inject(30.0)
    if not idle:
        return PersistResult(
            ok=False,
            action="install",
            code="E0506",
            message=f"注入任务 {busy_unit} 仍在运行，暂不安装开机持久化。",
            advice="等待注入结束后重试，避免边注入边改单元与覆盖镜像。",
        )
    steps.append(PersistStep("等待注入任务结束", True, "当前没有运行中的注入任务"))

    checked_steps, failure = preflight_install(target, repo_root=repo_root)
    steps.extend(checked_steps)
    if failure is not None:
        return replace(failure, steps=tuple(steps))
    payload_path = Path(target.payload_path)
    actual_sha = _sha256_file(payload_path)
    installer = _deploy_script("install.sh", repo_root)
    if not installer.is_file():
        return PersistResult(
            ok=False,
            action="install",
            code="E0502",
            message=f"找不到安装脚本：{installer}",
            advice="请在 v100-vbios-research 仓库根目录运行本工具。",
            steps=tuple(steps),
        )
    res = run(["bash", str(installer)], timeout=180)
    summary = _summary(res)
    if not res.ok:
        return PersistResult(
            ok=False,
            action="install",
            code="E0502",
            message=f"deploy/install.sh 执行失败：{summary}",
            advice="检查 MANIFEST.sha256 是否与仓库文件一致（`cd deploy && sha256sum -c MANIFEST.sha256`）。",
            steps=tuple(steps + [PersistStep("执行 deploy/install.sh", False, summary)]),
        )
    steps.append(PersistStep("执行 deploy/install.sh", True, "MANIFEST 校验通过，单元已启用"))

    # 顺序硬性要求：install.sh 通过之后再替换覆盖镜像
    replace_note = "与标准 payload 一致，无需替换"
    if actual_sha != _sha256_file(standard_payload_path(repo_root)):
        try:
            _replace_file(payload_path, INSTALLED_PAYLOAD)
        except OSError as exc:
            return PersistResult(
                ok=False,
                action="install",
                code="E0502",
                message=f"替换已安装覆盖镜像失败：{exc}",
                advice=f"请确认 {INSTALLED_PAYLOAD} 可写（需要 root）。",
                steps=tuple(steps),
            )
        installed_sha = _sha256_file(INSTALLED_PAYLOAD)
        if installed_sha != actual_sha:
            return PersistResult(
                ok=False,
                action="install",
                code="E0502",
                message=f"替换后回读 sha256 不符：{installed_sha[:8]}…（期望 {actual_sha[:8]}…）",
                steps=tuple(steps),
            )
        replace_note = f"已替换为档案镜像（sha256 {actual_sha[:8]}…）"
    steps.append(PersistStep("安装档案覆盖镜像", True, replace_note))

    dropin_note = "期望值为标准 350 W，未覆盖"
    if abs(target.power_w - STANDARD_POWER_W) > 0.01:
        try:
            DROPIN_DIR.mkdir(parents=True, exist_ok=True)
            DROPIN_PATH.write_text(
                "# 由 v100-tui 生成：开机持久化的期望功耗上限\n"
                "# 删除本文件即恢复单元内建的 350 W 缺省值\n"
                "[Service]\n"
                f"Environment={C.BOOT_EXPECT_ENV_KEY}={target.power_w:g}\n",
                encoding="utf-8",
            )
            os.chmod(DROPIN_PATH, 0o644)
        except OSError as exc:
            return PersistResult(
                ok=False,
                action="install",
                code="E0502",
                message=f"写入 drop-in 失败：{exc}",
                steps=tuple(steps),
            )
        dropin_note = f"已写入 {DROPIN_PATH}（V100_EXPECT_W={target.power_w:g}）"
    elif DROPIN_PATH.exists():
        try:
            DROPIN_PATH.unlink()
            dropin_note = "已移除旧的 drop-in，回到单元内建的 350 W"
        except OSError:
            dropin_note = "旧的 drop-in 未能移除"
    if dropin_note:
        reload_res = run(["systemctl", "daemon-reload"], timeout=60)
        steps.append(
            PersistStep(
                "写入期望功耗 drop-in",
                reload_res.ok,
                dropin_note if reload_res.ok else f"{dropin_note}；daemon-reload 失败：{_summary(reload_res)}",
            )
        )

    try:
        SOURCE_RECORD.write_text(
            json.dumps(
                {
                    "kind": "v100-tui.persist-source",
                    "version": 1,
                    "slug": target.slug,
                    "display_name": target.display_name,
                    "power_w": target.power_w,
                    "payload_sha256": actual_sha,
                    "installed_at": _iso_now(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(SOURCE_RECORD, 0o644)
        steps.append(PersistStep("记录来源档案", True, f"{SOURCE_RECORD}（slug：{target.slug or '未命名'}）"))
    except OSError as exc:
        steps.append(PersistStep("记录来源档案", False, f"写入失败：{exc}"))

    notes = conflict_notes(target.power_w)
    notes.append("已启用开机持久化：重启后会在显示管理器启动前自动注入。")
    return PersistResult(
        ok=all(step.ok for step in steps),
        action="install",
        steps=tuple(steps),
        message=f"开机持久化已安装（期望功耗 {target.power_w:g} W）。",
        notes=tuple(notes),
        state=state(repo_root=repo_root),
    )


# ---------------------------------------------------------------------------
# 卸载
# ---------------------------------------------------------------------------
def uninstall(*, repo_root: Path | None = None) -> PersistResult:
    """卸载开机持久化：``deploy/uninstall.sh`` → 清 drop-in → 提示内存覆盖仍在。"""
    steps: list[PersistStep] = []
    if not probe.is_root():
        return PersistResult(
            ok=False,
            action="uninstall",
            code=C.ERR_NOT_ROOT,
            message="卸载开机持久化需要 root 权限。",
            advice="退出本工具后执行 `sudo python3 -m tui`（在仓库根目录）。",
        )

    idle, busy_unit = _wait_for_idle_inject(30.0)
    if not idle:
        return PersistResult(
            ok=False,
            action="uninstall",
            code="E0506",
            message=f"注入任务 {busy_unit} 仍在运行，暂不卸载开机持久化。",
            advice="等待注入结束后重试，避免边注入边删文件。",
        )
    steps.append(PersistStep("等待注入任务结束", True, "当前没有运行中的注入任务"))

    script = _deploy_script("uninstall.sh", repo_root)
    if not script.is_file():
        return PersistResult(
            ok=False,
            action="uninstall",
            code="E0503",
            message=f"找不到卸载脚本：{script}",
            advice="请在 v100-vbios-research 仓库根目录运行本工具。",
            steps=tuple(steps),
        )
    res = run(["bash", str(script)], timeout=120)
    summary = _summary(res)
    if not res.ok:
        return PersistResult(
            ok=False,
            action="uninstall",
            code="E0503",
            message=f"deploy/uninstall.sh 执行失败：{summary}",
            advice="可在 TTY 中手动执行 `sudo bash deploy/uninstall.sh` 查看完整输出。",
            steps=tuple(steps + [PersistStep("执行 deploy/uninstall.sh", False, summary)]),
        )
    steps.append(PersistStep("执行 deploy/uninstall.sh", True, "单元已禁用，相关文件已移除"))

    if DROPIN_DIR.is_dir():
        try:
            shutil.rmtree(DROPIN_DIR)
            steps.append(PersistStep("清理 drop-in", True, f"已删除 {DROPIN_DIR}"))
        except OSError as exc:
            steps.append(PersistStep("清理 drop-in", False, f"删除失败：{exc}"))
        run(["systemctl", "daemon-reload"], timeout=60)

    notes = [
        "当前内存中的覆盖仍在生效：需要恢复原厂请使用主菜单「恢复原厂」或重启本机。",
    ]
    return PersistResult(
        ok=all(step.ok for step in steps),
        action="uninstall",
        steps=tuple(steps),
        message="开机持久化已卸载。",
        notes=tuple(notes),
        state=state(repo_root=repo_root),
    )


def _summary(res: RunResult, lines: int = 3) -> str:
    """把脚本输出压成一行中文摘要，供步骤明细展示。"""
    return res.tail(lines)
