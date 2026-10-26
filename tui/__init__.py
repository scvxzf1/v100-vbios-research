"""V100 快捷操作台 —— TUI 数据层与集成（团队任务 t4 / t7）。

本包只依赖 Python 3 标准库；数据层不做任何破坏性操作。
模块地图见 ``tui/DESIGN.md`` §4：

* ``constants``      协议常量与数值范围（唯一允许出现十六进制字面量的模块）
* ``env_probe``      平台/权限/服务探测、XDG 路径、外部命令统一包装
* ``gpu_state``      nvidia-smi 现值、sysfs 定位、覆盖与持久化状态
* ``rom_source``     四路基准 ROM 获取
* ``vbios_derive``   动态字段推导（复用 ``tools/derive_tables.py``）
* ``payload``        编辑意图 → 覆盖镜像（复用 ``tools/build_payload.py``）
* ``profile``        配置档案 CRUD
* ``history``        操作历史 JSONL
"""
__version__ = "1.0"

__all__ = ["__version__"]
