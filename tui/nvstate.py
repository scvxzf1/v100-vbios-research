"""GPU 状态探测的简短别名模块。

本文件是任务 t4 描述里列出的文件名；实现见 :mod:`tui.gpu_state`（DESIGN §4 的正式
模块名）。新代码请优先 ``from . import gpu_state``。

对外接口与 ``gpu_state`` 完全同名同义：

* ``GpuState`` / ``Snapshot``
* ``list_gpus()`` / ``read_gpu_state(bdf)`` / ``find_v100_bdf()``
* ``override_present()``（驱动 registry 是否已有 vbios 覆盖）
* ``boot_patch_state()``（开机持久化单元状态）
* ``snapshot(bdf=None, env=None)``
"""
from __future__ import annotations

from .gpu_state import (GpuState, Snapshot, boot_patch_state, device_present,
                        find_v100_bdf, list_gpus, list_v100_bdfs, normalize_bdf,
                        override_present, read_gpu_state, snapshot)

__all__ = ["GpuState", "Snapshot", "list_gpus", "list_v100_bdfs", "read_gpu_state",
           "find_v100_bdf", "device_present", "normalize_bdf", "override_present",
           "boot_patch_state", "snapshot"]
