"""``python3 -m tui`` 入口：完整参数解析与兜底都在 :mod:`tui.app`。

支持：``--rom 路径``、``--readonly``、``--no-color``、``--once``、
``--log-dir 目录``、``--version``、``--help``。无参数时进入交互界面。
"""
from __future__ import annotations

import sys

from .app import main

if __name__ == "__main__":
    sys.exit(main())
