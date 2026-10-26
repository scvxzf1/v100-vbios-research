"""配置档案的简短别名模块。

本文件是任务 t4 描述里列出的文件名；实现见 :mod:`tui.profile`（DESIGN §4 的正式模块名）。
新代码请优先 ``from . import profile``。

对外接口与 ``profile`` 完全同名同义：

* ``Profile`` / ``ProfileMeta`` / ``ProfileError``
* ``list_profiles()`` / ``load(slug)`` / ``save(profile)`` / ``delete(slug)``
* ``validate(data)`` / ``slugify(display_name, taken)`` / ``migrate_legacy_file()``
"""
from __future__ import annotations

from .profile import (Profile, ProfileError, ProfileMeta, delete,
                      existing_slugs, list_profiles, load, migrate_legacy_file,
                      profile_dir, profile_path, rename, save, slugify, validate)

__all__ = ["Profile", "ProfileMeta", "ProfileError", "list_profiles", "load",
           "save", "delete", "rename", "validate", "slugify", "existing_slugs",
           "profile_dir", "profile_path", "migrate_legacy_file"]
