"""配置档案（profile）增删改查、校验、版本迁移与原子写。

存储布局（tui/DESIGN.md §5.1、§5.2）：
``$XDG_CONFIG_HOME/v100-tui/profiles/<slug>.json``，与界面展示用的中文名分离。
旧版单文件 ``profiles.json`` 仅通过 :func:`migrate_legacy_file` 导入，不再写入。

铁律：
* 档案里**只存语义值（W / MHz）**，绝不存字节偏移——偏移每次重新推导。
* 不存任何凭据、不存 SUDO_USER、不存主机名、不存个人绝对路径。
* 更高版本创建的档案视为只读，禁止「载入并注入」。

对外接口：

* ``list_profiles() -> list[ProfileMeta]``
* ``load(slug) -> Profile`` ／ ``save(profile) -> Path`` ／ ``delete(slug)``
* ``validate(data) -> list[str]``
* ``slugify(display_name, taken=()) -> str``
* ``migrate_legacy_file() -> list[str]``
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from . import constants as C
from . import env_probe

_SLUG_RE = re.compile(r"^[a-zA-Z0-9_-]{1,%d}$" % C.SLUG_MAX_LEN)


class ProfileError(Exception):
    """档案读写失败（含错误码与建议）。"""

    def __init__(self, code, reason, advice="", path=None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.advice = advice
        self.path = path

    def __str__(self) -> str:
        return f"错误[{self.code}] {self.reason}"


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_time(text):
    if not text:
        return None
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Profile:
    """一份配置档案（version = 1）。字段含义见 DESIGN §5.2。"""

    slug: str
    display_name: str = ""
    note: str = ""
    power_w: float = None
    clocks: dict = dc_field(default_factory=dict)
    target: dict = dc_field(default_factory=dict)
    payload: dict = dc_field(default_factory=dict)
    extra: dict = dc_field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    version: int = C.PROFILE_VERSION
    readonly: bool = False
    filled: list = dc_field(default_factory=list)
    source: str = "本机生成"

    # ── 序列化 ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        doc = {
            "kind": C.PROFILE_KIND,
            "version": int(self.version),
            "slug": self.slug,
            "display_name": self.display_name,
            "note": self.note,
            "created_at": self.created_at or _now(),
            "updated_at": self.updated_at or _now(),
            "target": dict(self.target),
            "power": {"limit_w": None if self.power_w is None else round(float(self.power_w), 1)},
            "clocks": {k: (None if v is None else int(v)) for k, v in self.clocks.items()},
            "payload": dict(self.payload),
        }
        # 前向兼容：未知顶层字段原样带回
        for key, value in (self.extra or {}).items():
            doc.setdefault(key, value)
        return doc

    @classmethod
    def from_dict(cls, data, path=None) -> "Profile":
        if not isinstance(data, dict):
            raise ProfileError(C.ERR_PROFILE_CORRUPT, "档案内容不是 JSON 对象。",
                               "该文件可能被外部程序改坏。", path)
        kind = data.get("kind", C.PROFILE_KIND)
        if kind != C.PROFILE_KIND:
            raise ProfileError(
                C.ERR_PROFILE_INVALID,
                f"这不是 v100-tui 的配置档案（kind={kind!r}）。",
                "请确认文件来源。", path)

        version = data.get("version", C.PROFILE_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = C.PROFILE_VERSION
        readonly = version > C.PROFILE_VERSION

        filled = []
        power = (data.get("power") or {}).get("limit_w")
        if power is None and C.FIELD_POWER in data:
            power = data.get(C.FIELD_POWER)
        clocks = {}
        for key in C.CLOCK_FIELDS:
            value = (data.get("clocks") or {}).get(key, data.get(key))
            if value is None:
                # 缺字段用默认值补齐（界面标「默认」），不阻断读取
                filled.append(key)
            else:
                try:
                    clocks[key] = int(value)
                except (TypeError, ValueError):
                    filled.append(key)
        known = {"kind", "version", "slug", "display_name", "note", "created_at",
                 "updated_at", "target", "power", "clocks", "payload"}
        extra = {k: v for k, v in data.items() if k not in known}
        return cls(
            slug=str(data.get("slug") or (Path(path).stem if path else "")),
            display_name=str(data.get("display_name") or ""),
            note=str(data.get("note") or ""),
            power_w=None if power is None else round(float(power), 1),
            clocks=clocks,
            target=dict(data.get("target") or {}),
            payload=dict(data.get("payload") or {}),
            extra=extra,
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            version=version,
            readonly=readonly,
            filled=filled,
            source=str(data.get("source") or "本机生成"),
        )

    # ── 与编辑意图互转 ─────────────────────────────────────────────
    def to_intent(self):
        from .payload import EditIntent        # 延迟导入，避免循环依赖
        return EditIntent(power_w=self.power_w, clocks=dict(self.clocks))

    @classmethod
    def from_intent(cls, intent, slug, display_name="", note="",
                    layout=None, rom_candidate=None) -> "Profile":
        target = {}
        if layout is not None:
            target = {
                "vbios_version": (rom_candidate.target_version
                                  if getattr(rom_candidate, "target_version", None) else None),
                "rom_sha256": layout.rom_sha256,
                "rom_length": layout.rom_length,
            }
        if rom_candidate is not None:
            target.setdefault("rom_sha256", rom_candidate.sha256)
            target.setdefault("rom_length", rom_candidate.size)
        return cls(
            slug=slug,
            display_name=display_name or slug,
            note=note,
            power_w=None if intent.power_w is None else round(float(intent.power_w), 1),
            clocks={k: int(v) for k, v in (intent.clocks or {}).items() if v is not None},
            target={k: v for k, v in target.items() if v is not None},
            created_at=_now(),
            updated_at=_now(),
        )

    def has_payload(self) -> bool:
        return bool(self.payload.get("sha256"))


@dataclass
class ProfileMeta:
    """列表页需要的元信息（不载入完整档案）。"""

    slug: str
    display_name: str
    power_w: float = None
    clocks: dict = dc_field(default_factory=dict)
    updated_at: str = ""
    version: int = C.PROFILE_VERSION
    readonly: bool = False
    has_payload: bool = False
    error: str = ""
    source: str = "本机生成"

    @property
    def updated_display(self) -> str:
        moment = _parse_time(self.updated_at)
        return moment.strftime("%Y-%m-%d %H:%M") if moment else "未知"

    @property
    def clocks_display(self) -> str:
        xbar = self.clocks.get(C.FIELD_XBAR)
        gpc = self.clocks.get(C.FIELD_GPC_HDR)
        parts = []
        if xbar:
            parts.append(f"核心 {xbar}")
        if gpc:
            parts.append(f"Applications {gpc}")
        return " · ".join(parts) if parts else "未记录"


# ─────────────────────────────────────────────────────────────────────────────
# 校验
# ─────────────────────────────────────────────────────────────────────────────
def validate(data) -> list:
    """返回中文问题列表；空列表表示可写入。"""
    problems = []
    if not isinstance(data, dict):
        return ["档案内容必须是 JSON 对象。"]
    if data.get("kind", C.PROFILE_KIND) != C.PROFILE_KIND:
        problems.append(f"kind 必须是 {C.PROFILE_KIND}。")
    slug = str(data.get("slug") or "")
    if not _SLUG_RE.match(slug):
        problems.append("slug 只能包含字母、数字、下划线与连字符，长度 1–32。")
    power = (data.get("power") or {}).get("limit_w")
    if power is None:
        problems.append("缺少功耗上限。")
    else:
        try:
            power = float(power)
        except (TypeError, ValueError):
            problems.append("功耗上限必须是数字。")
        else:
            if not (C.POWER_MIN_W <= power <= C.POWER_MAX_W):
                problems.append(
                    f"功耗上限必须在 {C.POWER_MIN_W:.0f}–{C.POWER_MAX_W:.0f} W 之间。")
    for key, value in (data.get("clocks") or {}).items():
        if key not in C.CLOCK_FIELDS:
            problems.append(f"未知的频率字段：{key}。")
            continue
        if value is None:
            continue
        try:
            mhz = int(value)
        except (TypeError, ValueError):
            problems.append(f"{key} 必须是整数 MHz。")
            continue
        if not (C.CLOCK_MIN_MHZ <= mhz <= C.CLOCK_MAX_MHZ):
            problems.append(
                f"{key} 必须在 {C.CLOCK_MIN_MHZ}–{C.CLOCK_MAX_MHZ} MHz 之间。")
    target = data.get("target") or {}
    sha = target.get("rom_sha256")
    if sha and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
        problems.append("target.rom_sha256 必须是 64 位小写十六进制。")
    for field_name in ("created_at", "updated_at"):
        if data.get(field_name) and _parse_time(data[field_name]) is None:
            problems.append(f"{field_name} 不是合法的 ISO 8601 时间。")
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# slug 生成
# ─────────────────────────────────────────────────────────────────────────────
def slugify(display_name, taken=()) -> str:
    """把展示名压成安全 slug；中文名不会进入文件名（见 DESIGN §5.1）。"""
    taken = {str(t).lower() for t in (taken or ())}
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(display_name or "")).strip("-_").lower()
    base = base[:C.SLUG_MAX_LEN] or C.SLUG_DEFAULT_PREFIX
    if base not in taken:
        return base
    for index in range(2, 100):
        candidate = f"{base[:C.SLUG_MAX_LEN - 3]}-{index}"
        if candidate not in taken:
            return candidate
    return f"{C.SLUG_DEFAULT_PREFIX}-{int(_dt.datetime.now().timestamp())}"


# ─────────────────────────────────────────────────────────────────────────────
# 读写
# ─────────────────────────────────────────────────────────────────────────────
def profile_dir() -> Path:
    return env_probe.profiles_dir()


def profile_path(slug) -> Path:
    return profile_dir() / f"{slug}.json"


def existing_slugs() -> list:
    try:
        return sorted(p.stem for p in profile_dir().glob("*.json"))
    except OSError:
        return []


def list_profiles() -> list:
    """列出全部档案元信息；单个文件损坏不影响其它档案。"""
    metas = []
    for path in sorted(profile_dir().glob("*.json")) if profile_dir().exists() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            metas.append(ProfileMeta(slug=path.stem, display_name=path.stem,
                                     error=f"文件无法解析：{exc}"))
            continue
        try:
            profile = Profile.from_dict(data, path)
        except ProfileError as exc:
            metas.append(ProfileMeta(slug=path.stem, display_name=path.stem,
                                     error=str(exc)))
            continue
        metas.append(ProfileMeta(
            slug=path.stem,              # 文件名即权威 slug（load 按文件名定位）
            display_name=profile.display_name or profile.slug or path.stem,
            power_w=profile.power_w,
            clocks=dict(profile.clocks),
            updated_at=profile.updated_at,
            version=profile.version,
            readonly=profile.readonly,
            has_payload=profile.has_payload(),
            source=profile.source,
        ))
    def _sort_key(meta):
        moment = _parse_time(meta.updated_at)
        return (moment.timestamp() if moment else float("-inf"), meta.slug)

    metas.sort(key=_sort_key, reverse=True)
    return metas


def load(slug) -> Profile:
    """按 slug 载入；损坏或类型不符抛 :class:`ProfileError`。"""
    name = str(slug)
    if name.endswith(".json"):
        name = name[:-5]
    path = profile_path(name)
    if not path.is_file():
        raise ProfileError(C.ERR_PROFILE_IO, f"档案不存在：{name}。",
                           "请用档案列表确认名称。", path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(
            C.ERR_PROFILE_CORRUPT, f"档案 {name} 的 JSON 解析失败：第 {exc.lineno} 行。",
            "可把该文件改名备份为 *.corrupt-<时间戳>.json 后重建。", path)
    except OSError as exc:
        raise ProfileError(C.ERR_PROFILE_IO, f"读取档案 {name} 失败：{exc}", "", path)
    profile = Profile.from_dict(data, path)
    profile.slug = name            # 文件名是权威 slug，避免内容与文件名不一致时错位
    return profile


def save(profile: Profile) -> Path:
    """原子写入（tmp + os.replace），属主归还给调用者。"""
    if profile.readonly or profile.version > C.PROFILE_VERSION:
        raise ProfileError(
            C.ERR_PROFILE_VERSION,
            f"档案 {profile.slug} 由更高版本（version={profile.version}）创建，只读。",
            "请升级本工具后再修改，或另存为新档案。")
    doc = profile.to_dict()
    problems = validate(doc)
    if problems:
        raise ProfileError(C.ERR_PROFILE_INVALID, "；".join(problems), "请修正后重试。")
    directory = env_probe.ensure_dir(profile_dir())
    if not profile.created_at:
        profile.created_at = _now()
    profile.updated_at = _now()
    doc["created_at"] = profile.created_at
    doc["updated_at"] = profile.updated_at
    path = directory / f"{profile.slug}.json"
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ProfileError(C.ERR_PROFILE_IO, f"写入档案失败：{exc}",
                           "请检查配置目录权限。", path)
    env_probe.chown_to_invoker(path)
    return path


def delete(slug) -> bool:
    """删除档案；不存在返回 ``False``（幂等）。"""
    path = profile_path(str(slug).removesuffix(".json"))
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ProfileError(C.ERR_PROFILE_IO, f"删除档案失败：{exc}", "", path)


def rename(profile: Profile, new_display_name) -> Profile:
    """只改展示名与备注（slug 不变，避免破坏引用）。"""
    profile.display_name = str(new_display_name or profile.display_name)
    profile.updated_at = _now()
    save(profile)
    return profile


# ─────────────────────────────────────────────────────────────────────────────
# 旧版单文件导入
# ─────────────────────────────────────────────────────────────────────────────
def migrate_legacy_file() -> list:
    """把旧版 ``profiles.json``（单文件字典）导入为按 slug 拆分的新布局。

    返回导入成功的 slug 列表；旧文件保持原样不动（只读迁移，可重复执行）。
    """
    legacy = env_probe.legacy_profiles_path()
    if not legacy.is_file():
        return []
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = []
    if isinstance(data, dict):
        if "profiles" in data and isinstance(data["profiles"], list):
            entries = data["profiles"]
        elif "kind" in data or "power" in data:
            entries = [data]
        else:
            entries = [dict(v, slug=k) if isinstance(v, dict) else {"slug": k}
                       for k, v in data.items()]
    elif isinstance(data, list):
        entries = data

    imported = []
    taken = set(existing_slugs())
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry = dict(entry)
        slug = entry.get("slug") or slugify(entry.get("display_name", ""), taken)
        entry["slug"] = slug
        if "power" not in entry and entry.get("power_w") is not None:
            entry["power"] = {"limit_w": entry["power_w"]}
        try:
            profile = Profile.from_dict(entry)
            if profile.slug in taken:
                continue
            save(profile)
        except ProfileError:
            continue
        taken.add(slug)
        imported.append(slug)
    return imported
