"""动态字段推导封装：一切偏移来自 ``tools/derive_tables.py``。

铁律（tui/DESIGN.md §9.3(1)）：``tui/`` 内**不得复制任何 BIT 解析代码**，
也不得重算偏移公式。本模块只做三件事——载入、调用、规整成界面友好的结构。

对外接口：

* ``derive_layout(rom) -> Layout``
* ``Layout.fields()`` / ``Layout.current_values()`` / ``Layout.checksum_at``
* ``ratio_gate(layout, clocks) -> list[Issue]``（写入前的比率一致性闸门）
* ``DeriveError(code, reason, advice)``
"""
from __future__ import annotations

import functools
import hashlib
import importlib.util
from dataclasses import dataclass, field as dc_field

from . import constants as C


class DeriveError(Exception):
    """字段推导失败。出现即中止，绝不猜测偏移。"""

    def __init__(self, code, reason, advice=""):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.advice = advice

    def __str__(self) -> str:
        return f"错误[{self.code}] {self.reason}"


@functools.lru_cache(maxsize=4)
def _load_derive_module(path_str: str):
    """以文件路径载入 derive_tables.py（不用 import 语句，避免污染 sys.path）。"""
    spec = importlib.util.spec_from_file_location("v100_derive_tables", path_str)
    if spec is None or spec.loader is None:
        raise DeriveError(C.ERR_DERIVE, f"无法载入字段推导脚本：{path_str}",
                          "请确认仓库 tools/derive_tables.py 存在且完整。")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                       # 语法/导入错误
        raise DeriveError(C.ERR_DERIVE, f"字段推导脚本载入失败：{exc}",
                          "请检查 tools/derive_tables.py 是否被改动过。")
    return module


def derive_module():
    """返回已载入的 derive_tables 模块（供 payload 层复用，避免重复解析）。"""
    return _load_derive_module(str(C.TOOLS_DIR / "derive_tables.py"))


@dataclass
class Field:
    """一个可编辑字段的现值与写入位置（偏移来自 derive()）。"""

    name: str
    label: str
    at: int
    mhz: int
    ratio: object = None            # 原固件的比率字段值（u16）
    flagged: bool = False           # 是否带 PSTATE_FLAG 标志位

    @property
    def expected_ratio(self) -> int:
        return self.mhz // C.CLOCK_RATIO_DIVISOR

    @property
    def ratio_consistent(self) -> bool:
        """原固件的比率字段是否等于 MHz//4（实测三例一致，见 DESIGN §10）。"""
        return self.ratio is None or int(self.ratio) == self.expected_ratio

    @property
    def writable(self) -> bool:
        return C.CLOCK_MIN_MHZ <= self.mhz <= C.CLOCK_MAX_MHZ


@dataclass
class Layout:
    """一次推导得到的全部字段位置与现值。"""

    rom_sha256: str
    rom_length: int
    power_row_at: int
    power_row_index: int
    power_low_w: float
    power_rated_w: float
    power_max_w: float
    xbar: Field
    sys: Field
    gpc_hdr: Field
    gpc_r1: Field
    performance_entry0_at: int
    performance_entry0_mhz: int
    data_image_start: int
    data_image_end: int
    data_images: tuple = ()
    images: tuple = ()
    warnings: list = dc_field(default_factory=list)

    # ── 校验和落点：被改动数据镜像的末字节 ─────────────────────────────
    @property
    def checksum_at(self) -> int:
        return self.data_image_end - 1

    def fields(self) -> dict:
        """语义键 → Field（不含功耗行：功耗是同一行的两个 u32）。"""
        return {
            C.FIELD_XBAR: self.xbar,
            C.FIELD_SYS: self.sys,
            C.FIELD_GPC_HDR: self.gpc_hdr,
            C.FIELD_GPC_R1: self.gpc_r1,
            C.FIELD_PERF0: Field(
                name=C.FIELD_PERF0,
                label="Performance ENTRY0",
                at=self.performance_entry0_at,
                mhz=self.performance_entry0_mhz,
            ),
        }

    def current_values(self) -> dict:
        """现值（语义值），可直接作为编辑界面的初始值。"""
        values = {C.FIELD_POWER: self.power_rated_w}
        for name, fld in self.fields().items():
            values[name] = fld.mhz
        return values

    def labels(self) -> dict:
        labels = {C.FIELD_POWER: "功耗上限"}
        for name, fld in self.fields().items():
            labels[name] = fld.label
        return labels

    def describe(self) -> list:
        """界面用的中文现值摘要（每行一条）。"""
        lines = [f"功耗上限：{self.power_rated_w:.0f} W"
                 f"（下限 {self.power_low_w:.0f} W，功耗行 row{self.power_row_index}）",
                 f"核心上报上限 XBAR：{self.xbar.mhz} MHz",
                 f"SYS：{self.sys.mhz} MHz",
                 f"Applications GPC_HDR：{self.gpc_hdr.mhz} MHz",
                 f"Applications GPC_R1：{self.gpc_r1.mhz} MHz",
                 f"Performance ENTRY0：{self.performance_entry0_mhz} MHz",
                 f"数据镜像：[{self.data_image_start:#x}..{self.data_image_end:#x}]"
                 f"，校验和末字节 {self.checksum_at:#x}"]
        return lines


def derive_layout(rom) -> Layout:
    """调用 ``tools/derive_tables.derive()`` 并规整结果。

    失败一律抛 :class:`DeriveError`（带错误码），调用方应中止写入。
    """
    data = bytes(rom)
    if len(data) < C.PAYLOAD_WINDOW_START + C.PAYLOAD_WINDOW_LENGTH:
        raise DeriveError(
            C.ERR_ROM_TOO_SHORT,
            f"ROM 只有 {len(data)} 字节，不足覆盖镜像窗口所需的 "
            f"{C.PAYLOAD_WINDOW_START + C.PAYLOAD_WINDOW_LENGTH} 字节。",
            "请提供完整 VBIOS dump。")

    module = derive_module()
    try:
        r = module.derive(bytearray(data))
    except SystemExit as exc:                    # derive_tables 用 SystemExit 报错
        raise DeriveError(C.ERR_DERIVE, f"字段推导失败：{exc}",
                          "该固件可能不是 GV100 版本，或镜像链结构不受支持。")
    except Exception as exc:
        raise DeriveError(C.ERR_DERIVE, f"字段推导异常：{exc}",
                          "请用 tools/derive_tables.py 单独验证该 ROM。")

    try:
        row = r["power"]["row"]
        ps = r["pstate"]
        pf = r["performance"]
        images = tuple((im["start"], im["end"], im["code"]) for im in r["images"])
        data_imgs = tuple((im["start"], im["end"]) for im in r["images"]
                          if im["code"] == module.IMG_TYPE_DATA)
    except (KeyError, TypeError) as exc:
        raise DeriveError(C.ERR_DERIVE, f"推导结果缺少必需字段：{exc}",
                          "请确认 tools/derive_tables.py 未被裁剪。")

    if "entry0_3rd_at" not in pf or "entry_size" not in pf:
        raise DeriveError(
            C.ERR_DERIVE,
            "derive_tables.derive() 未导出 performance.entry0_3rd_at / entry_size，"
            "无法在不重算公式的前提下写回 ENTRY0 字段。",
            "请使用仓库当前版本的 tools/derive_tables.py（含该两字段的扩展）。")

    host = next(((s, e) for s, e in data_imgs if s <= row["at"] < e), None)
    if host is None:
        raise DeriveError(C.ERR_DERIVE, "功耗行不在任何数据镜像内。",
                          "该固件结构与已知版本不同，拒绝写入。")

    def field_of(key, label, sem, flagged):
        node = ps[key]
        return Field(name=sem, label=label, at=node["at"], mhz=node["mhz"],
                     ratio=node.get("ratio"), flagged=flagged)

    layout = Layout(
        rom_sha256=hashlib.sha256(data).hexdigest(),
        rom_length=len(data),
        power_row_at=row["at"],
        power_row_index=row["i"],
        power_low_w=row["low"] / 1000,
        power_rated_w=row["rated"] / 1000,
        power_max_w=row["max"] / 1000,
        xbar=field_of("xbar", "核心上报上限 XBAR", C.FIELD_XBAR, True),
        sys=field_of("sys", "SYS", C.FIELD_SYS, True),
        gpc_hdr=field_of("gpc_hdr", "Applications GPC_HDR", C.FIELD_GPC_HDR, False),
        gpc_r1=field_of("gpc_r1", "Applications GPC_R1", C.FIELD_GPC_R1, False),
        performance_entry0_at=pf["entry0_3rd_at"],
        performance_entry0_mhz=pf["entry0_3rd_u16"] * C.CLOCK_RATIO_DIVISOR,
        data_image_start=host[0],
        data_image_end=host[1],
        data_images=data_imgs,
        images=images,
    )

    # 不阻断、但必须如实告知的异常事实
    for fld in layout.fields().values():
        if not fld.writable:
            layout.warnings.append(
                f"{fld.label} 现值 {fld.mhz} MHz 超出 "
                f"{C.CLOCK_MIN_MHZ}–{C.CLOCK_MAX_MHZ} MHz 的约定范围。")
        if not fld.ratio_consistent:
            layout.warnings.append(
                f"{fld.label} 的比率字段 {fld.ratio} 与 MHz//4={fld.expected_ratio} 不一致。")
    if layout.power_rated_w <= 0:
        layout.warnings.append("功耗行 rated 为 0，可能不是生效行（tgp_index）。")
    return layout


def ratio_gate(layout: Layout, clocks: dict):
    """写入前的比率一致性闸门（DESIGN §10）。

    返回问题列表 ``[(字段名, 说明)]``；为空表示可以安全写入。
    判据：原固件该字段满足 ``ratio == mhz//4``，但目标值 ``mhz//4`` 与之不同构时，
    说明本固件的比率编码规则与已知版本不同，默认拒绝写入。
    """
    issues = []
    current = layout.fields()
    for name, mhz in (clocks or {}).items():
        fld = current.get(name)
        if fld is None:
            continue
        if not fld.ratio_consistent:
            issues.append((
                name,
                f"{fld.label} 原固件比率字段 {fld.ratio} 与 MHz//4={fld.expected_ratio} "
                f"不一致，拒绝按该编码写入。"))
        try:
            target_ratio = int(mhz) // C.CLOCK_RATIO_DIVISOR
        except (TypeError, ValueError):
            issues.append((name, f"{fld.label} 目标值 {mhz!r} 不是有效整数 MHz。"))
            continue
        if target_ratio <= 0:
            issues.append((name, f"{fld.label} 目标值 {mhz} MHz 过小，比率字段会为 0。"))
    return issues
