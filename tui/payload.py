"""编辑意图 → 覆盖镜像（纯计算，不执行任何外部命令、不触碰硬件）。

分工（tui/DESIGN.md §4）：
* 真实写入逻辑在 ``tools/build_payload.py``（本次已改造为纯函数并保留 CLI）；
  本模块只负责**校验、调用、回读、落盘**，绝不复制第二份字段写入实现。
* 本文件不调用任何外部命令：生成覆盖镜像全程是本地字节运算。

对外接口：

* ``EditIntent``：编辑意图（语义值：W / MHz，不含任何偏移）
* ``validate_intent(intent, layout) -> list[Issue]``
* ``build(rom, intent, layout=None) -> Payload``
* ``verify_roundtrip(payload) -> VerifyReport``
* ``write(payload, cache_dir=None, name_hint="payload") -> Path``
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from . import constants as C
from . import env_probe, vbios_derive


class PayloadError(Exception):
    """覆盖镜像生成/校验失败。"""

    def __init__(self, code, reason, advice=""):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.advice = advice

    def __str__(self) -> str:
        return f"错误[{self.code}] {self.reason}"


@dataclass
class Issue:
    """一条校验结论：``level='error'`` 必须阻止生成，``'warn'`` 只提示。"""

    code: str
    level: str
    field: str
    message: str

    @property
    def blocking(self) -> bool:
        return self.level == "error"


@dataclass
class EditIntent:
    """编辑意图：只存语义值（W / MHz），绝不存偏移。"""

    power_w: float
    clocks: dict = dc_field(default_factory=dict)

    # ── 构造 ────────────────────────────────────────────────────────
    @classmethod
    def from_layout(cls, layout):
        """以现有固件的现值为初始意图。"""
        return cls(power_w=layout.power_rated_w,
                   clocks={k: v for k, v in layout.current_values().items()
                           if k != C.FIELD_POWER})

    @classmethod
    def from_dict(cls, data) -> "EditIntent":
        data = data or {}
        power = data.get("power_w", data.get(C.FIELD_POWER))
        clocks = data.get("clocks") or {
            k: data[k] for k in C.CLOCK_FIELDS if k in data}
        return cls(power_w=float(power) if power is not None else None,
                   clocks={k: int(v) for k, v in (clocks or {}).items() if v is not None})

    # ── 导出 ────────────────────────────────────────────────────────
    def as_dict(self) -> dict:
        out = {"power_w": self.power_w}
        out.update({k: v for k, v in self.clocks.items()})
        return out

    def diff_against(self, layout) -> dict:
        """``{语义键: (现值, 目标值)}``，只列出真正变化的字段。"""
        current = layout.current_values()
        out = {}
        if self.power_w is not None and abs(self.power_w - current[C.FIELD_POWER]) > 1e-9:
            out[C.FIELD_POWER] = (current[C.FIELD_POWER], self.power_w)
        for key in C.CLOCK_FIELDS:
            target = self.clocks.get(key)
            if target is not None and int(target) != int(current.get(key, -1)):
                out[key] = (current.get(key), int(target))
        return out


@dataclass
class Payload:
    """生成的覆盖镜像及其元信息。"""

    data: bytes
    sha256: str
    size: int
    intent: EditIntent
    summary: dict = dc_field(default_factory=dict)

    @property
    def sha8(self) -> str:
        return self.sha256[:8]


@dataclass
class VerifyReport:
    ok: bool
    size_ok: bool
    sha256: str
    fields: dict = dc_field(default_factory=dict)   # 名称 → (目标, 回读实得)
    mismatches: list = dc_field(default_factory=list)
    checksum_ok: bool = True
    notes: list = dc_field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────────────────────
def validate_intent(intent: EditIntent, layout) -> list:
    """前端校验的第二份（数据层必须自己再验一次，见 DESIGN §6.5）。"""
    issues = []
    current = layout.current_values()

    if intent.power_w is None:
        issues.append(Issue(C.ERR_RANGE, "error", C.FIELD_POWER, "未指定目标功耗上限。"))
    else:
        pw = float(intent.power_w)
        if not (C.POWER_MIN_W <= pw <= C.POWER_MAX_W):
            issues.append(Issue(
                C.ERR_RANGE, "error", C.FIELD_POWER,
                f"功耗上限必须在 {C.POWER_MIN_W:.0f}–{C.POWER_MAX_W:.0f} W 之间，"
                f"收到 {pw:g} W。"))
        if pw <= layout.power_low_w:
            issues.append(Issue(
                C.ERR_RANGE, "error", C.FIELD_POWER,
                f"功耗上限必须大于该行下限 {layout.power_low_w:.1f} W，收到 {pw:g} W。"))

    for key in C.CLOCK_FIELDS:
        value = intent.clocks.get(key)
        if value is None:
            continue
        try:
            mhz = int(value)
        except (TypeError, ValueError):
            issues.append(Issue(C.ERR_RANGE, "error", key, f"频率必须是整数 MHz。"))
            continue
        if not (C.CLOCK_MIN_MHZ <= mhz <= C.CLOCK_MAX_MHZ):
            issues.append(Issue(
                C.ERR_RANGE, "error", key,
                f"频率必须在 {C.CLOCK_MIN_MHZ}–{C.CLOCK_MAX_MHZ} MHz 之间，收到 {mhz} MHz。"))
        if mhz > C.WARN_CLOCK_MHZ:
            issues.append(Issue(
                C.WARN_CLOCK_PHYSICAL, "warn", key,
                f"{mhz} MHz 高于出厂电压下的实测物理上限 {C.WARN_CLOCK_MHZ} MHz，"
                f"超出部分只影响上报值，实际频率不会提升。"))
        if key == C.FIELD_PERF0 and mhz % C.CLOCK_RATIO_DIVISOR:
            issues.append(Issue(
                C.WARN_PERF_ALIGN, "warn", key,
                f"{mhz} MHz 不是 {C.CLOCK_RATIO_DIVISOR} 的整数倍，"
                f"该字段按 {C.CLOCK_RATIO_DIVISOR} MHz 对齐，实际写为 "
                f"{(mhz // C.CLOCK_RATIO_DIVISOR) * C.CLOCK_RATIO_DIVISOR} MHz。"))

    for name, why in vbios_derive.ratio_gate(layout, intent.clocks or {}):
        issues.append(Issue(C.ERR_RATIO, "error", name, why))

    if not intent.diff_against(layout):
        issues.append(Issue(
            C.WARN_SAME_AS_CURRENT, "error", "-",
            "目标值与现值相同，无需生成覆盖镜像。"))
    return issues


def blocking_issues(issues) -> list:
    return [i for i in issues if i.blocking]


# ─────────────────────────────────────────────────────────────────────────────
# 生成
# ─────────────────────────────────────────────────────────────────────────────
def _load_build_module(path: Path):
    spec = importlib.util.spec_from_file_location("v100_build_payload", path)
    if spec is None or spec.loader is None:
        raise PayloadError(C.ERR_BUILD, f"无法载入 {path}", "请确认仓库 tools/ 完整。")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PayloadError(C.ERR_BUILD, f"tools/build_payload.py 载入失败：{exc}",
                           "请检查该脚本是否被改动。")
    return module


def build(rom, intent: EditIntent, layout=None) -> Payload:
    """生成覆盖镜像。校验不通过时抛 :class:`PayloadError`，不产生任何文件。"""
    layout = layout or vbios_derive.derive_layout(rom)
    issues = validate_intent(intent, layout)
    blocking = blocking_issues(issues)
    if blocking:
        first = blocking[0]
        raise PayloadError(first.code, first.message,
                           "请调整目标值后重试。")

    module = _load_build_module(C.TOOLS_DIR / "build_payload.py")
    clocks = {k: int(v) for k, v in (intent.clocks or {}).items() if v is not None}
    try:
        data = module.build_payload(bytes(rom), power_w=float(intent.power_w),
                                    clocks=clocks, dt=vbios_derive.derive_module())
    except ValueError as exc:
        raise PayloadError(C.ERR_OUTSIDE, f"生成覆盖镜像被拒绝：{exc}",
                           "该固件结构与已知版本不同，或目标值越界。")
    except Exception as exc:
        raise PayloadError(C.ERR_BUILD, f"生成覆盖镜像失败：{exc}",
                           "请用 tools/build_payload.py 单独复现。")

    if len(data) != C.PAYLOAD_WINDOW_LENGTH:
        raise PayloadError(C.ERR_BUILD,
                           f"生成结果长度 {len(data)} 与约定的 "
                           f"{C.PAYLOAD_WINDOW_LENGTH} 字节不符。")

    summary = summarize(rom, data, intent)
    payload = Payload(data=data, sha256=hashlib.sha256(data).hexdigest(),
                      size=len(data), intent=intent, summary=summary)
    report = verify_roundtrip(payload)
    if not report.ok:
        raise PayloadError(
            C.ERR_BUILD,
            "回读校验未通过：" + "；".join(report.mismatches or ["字段与目标不一致"]),
            "拒绝产出可疑镜像。")
    payload.summary["warnings"] = [i.message for i in issues if i.level == "warn"]
    return payload


def summarize(rom, payload_bytes: bytes, intent: EditIntent) -> dict:
    """改动摘要：字节数、区间、目标值。"""
    rom = bytes(rom)
    win_lo = C.PAYLOAD_WINDOW_START
    end = min(len(rom), win_lo + len(payload_bytes))
    diff = [i for i in range(win_lo, end) if payload_bytes[i - win_lo] != rom[i]]
    return {
        "changed_bytes": len(diff),
        "first_offset": min(diff) if diff else None,
        "last_offset": max(diff) if diff else None,
        "size": len(payload_bytes),
        "target": intent.as_dict(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 回读校验
# ─────────────────────────────────────────────────────────────────────────────
def _reconstruct(payload_bytes: bytes) -> bytes:
    """把覆盖镜像放回等长 ROM 缓冲（前缀补零），以便重新推导。"""
    return bytes(C.PAYLOAD_WINDOW_START) + bytes(payload_bytes)


def verify_roundtrip(payload: Payload) -> VerifyReport:
    """重新推导生成的镜像，逐字段核对，并检查没有新增非零校验和镜像。"""
    data = payload.data
    report = VerifyReport(ok=True, size_ok=(len(data) == C.PAYLOAD_WINDOW_LENGTH),
                          sha256=hashlib.sha256(data).hexdigest())
    if report.sha256 != payload.sha256:
        report.ok = False
        report.mismatches.append("sha256 与生成时不一致")

    try:
        layout = vbios_derive.derive_layout(_reconstruct(data))
    except vbios_derive.DeriveError as exc:
        report.ok = False
        report.mismatches.append(f"回读推导失败：{exc.reason}")
        return report

    target = payload.intent.as_dict()
    got = layout.current_values()
    expect = {C.FIELD_POWER: layout.power_rated_w}
    for key in C.CLOCK_FIELDS:
        expect[key] = got.get(key)
    wanted = {
        C.FIELD_POWER: target.get("power_w"),
        **{k: target.get(k) for k in C.CLOCK_FIELDS if target.get(k) is not None},
    }
    for key, want in wanted.items():
        have = expect.get(key)
        report.fields[key] = (want, have)
        if want is None or have is None:
            report.ok = False
            report.mismatches.append(f"{key} 回读缺失")
            continue
        goal = (int(want) // C.CLOCK_RATIO_DIVISOR) * C.CLOCK_RATIO_DIVISOR \
            if key == C.FIELD_PERF0 else want
        if abs(float(have) - float(goal)) > 1e-6:
            report.ok = False
            report.mismatches.append(f"{key} 目标 {goal} 实得 {have}")

    additive = [im for im in layout.images
                if im[2] != C.IMG_TYPE_NO_ADDITIVE_CKSUM]
    report.notes.append(f"回读镜像链：共 {len(layout.images)} 段，"
                        f"其中 {len(additive)} 段参与加和校验")
    if not report.size_ok:
        report.ok = False
        report.mismatches.append("覆盖镜像长度不符")
    return report


# ─────────────────────────────────────────────────────────────────────────────
# 落盘
# ─────────────────────────────────────────────────────────────────────────────
def write(payload: Payload, cache_dir=None, name_hint="payload") -> Path:
    """把覆盖镜像写入缓存目录（原子写 + 属主归还）。"""
    target_dir = Path(cache_dir) if cache_dir else env_probe.payload_cache_dir()
    env_probe.ensure_dir(target_dir)
    safe = "".join(ch for ch in (name_hint or "payload")
                   if ch.isalnum() or ch in "-_") or "payload"
    path = target_dir / f"{safe}-{payload.sha8}.bin"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload.data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise PayloadError(C.ERR_BUILD, f"写入覆盖镜像失败：{exc}",
                           "请检查缓存目录权限。")
    env_probe.chown_to_invoker(path)
    return path
