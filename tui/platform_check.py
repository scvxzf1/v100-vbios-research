"""平台闸门：本工具仅支持 Linux。

本文件是任务 t4 描述里列出的文件名；实现复用 :mod:`tui.env_probe`，
不重复写一份平台判断逻辑。新代码请直接使用 ``env_probe.platform_gate()``。

对外接口：

* ``ensure_linux() -> (bool, str)``：可用时返回 ``(True, "")``，否则返回中文原因
* ``is_linux() -> bool``
* ``platform_name() -> str``
* ``EXIT_UNSUPPORTED = 2``：非 Linux 时的进程退出码（DESIGN §6.1）
"""
from __future__ import annotations

from .env_probe import (LINUX_ONLY_MESSAGE, is_linux, platform_gate,
                        platform_name)

EXIT_UNSUPPORTED = 2

__all__ = ["ensure_linux", "is_linux", "platform_name", "LINUX_ONLY_MESSAGE",
           "EXIT_UNSUPPORTED"]


def ensure_linux():
    """返回 ``(是否可用, 中文说明)``；非 Linux 时说明可直接打印给用户。"""
    return platform_gate()
