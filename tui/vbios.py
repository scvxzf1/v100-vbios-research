"""VBIOS 字段读取与覆盖镜像生成的简短别名模块。

本文件是任务 t4 描述里列出的文件名；实现见 :mod:`tui.vbios_derive`（动态推导）与
:mod:`tui.payload`（覆盖镜像）。新代码请优先引用这两个模块。

额外提供两个一步到位的便捷函数，便于脚本与小工具调用：

* ``read_fields(rom) -> dict``：现值语义值（W / MHz）
* ``build_from_values(rom, power_w, clocks) -> Payload``：按语义值直接生成
"""
from __future__ import annotations

from . import payload as _payload
from . import vbios_derive as _derive
from .payload import (EditIntent, Issue, Payload, PayloadError, VerifyReport,
                      build, validate_intent, verify_roundtrip, write)
from .vbios_derive import DeriveError, Field, Layout, derive_layout, ratio_gate

__all__ = [
    "Layout", "Field", "DeriveError", "derive_layout", "ratio_gate",
    "EditIntent", "Issue", "Payload", "PayloadError", "VerifyReport",
    "build", "write", "verify_roundtrip", "validate_intent",
    "read_fields", "build_from_values",
]


def read_fields(rom):
    """读取字段现值（语义值）：``{'power_limit_w': 300.0, 'pstate_xbar_mhz': 1530, ...}``。"""
    return _derive.derive_layout(rom).current_values()


def build_from_values(rom, power_w, clocks=None):
    """按语义值直接生成覆盖镜像（内部仍走完校验与回读）。"""
    intent = _payload.EditIntent(power_w=power_w, clocks=dict(clocks or {}))
    return _payload.build(rom, intent)
