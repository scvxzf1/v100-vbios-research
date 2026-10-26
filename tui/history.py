"""操作历史：追加式 JSONL。

存储位置：``$XDG_CONFIG_HOME/v100-tui/history.jsonl``（tui/DESIGN.md §5.1）。
原则：

* **只追加**，界面只读派生视图，不提供编辑入口。
* 每条记录自带时间、动作、结果与关键数值；写入失败不影响主流程（返回 ``None``）。
* 不记录任何凭据、不记录个人绝对路径（``--rom`` 路径只记文件名）。

对外接口：

* ``append(event) -> dict | None``
* ``record(action, result, **fields) -> dict | None``（便捷写法）
* ``tail(n=50) -> list[dict]``（最新在前）
* ``export_text(path=None) -> Path``
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
from pathlib import Path

from . import constants as C
from . import env_probe

# 动作用词（与界面菜单保持一致，避免同一件事两种叫法）
ACTION_EDIT = "生成覆盖镜像"
ACTION_INJECT = "注入"
ACTION_SAVE_PROFILE = "保存档案"
ACTION_DELETE_PROFILE = "删除档案"
ACTION_PERSIST_INSTALL = "安装开机持久化"
ACTION_PERSIST_UNINSTALL = "卸载开机持久化"
ACTION_RESTORE = "恢复原厂"
ACTION_EXPORT = "导出"

RESULT_OK = "成功"
RESULT_FAILED = "失败"
RESULT_VERIFY_FAILED = "已执行但校验不符"


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def history_file() -> Path:
    return env_probe.history_path()


def _sanitize(event: dict) -> dict:
    """去掉绝对路径（只保留文件名）与任何疑似凭据的键。"""
    out = {}
    for key, value in (event or {}).items():
        lowered = key.lower()
        # 疑似凭据的键直接丢弃，历史里绝不落任何密钥类字段
        if re.search(r"pass|secret|token|credential", lowered):
            continue
        if isinstance(value, str) and value.startswith("/"):
            out[key] = os.path.basename(value)
        else:
            out[key] = value
    return out


def append(event: dict):
    """追加一条记录。返回实际写入的字典；写入失败返回 ``None``（不抛出）。"""
    if not isinstance(event, dict):
        return None
    record = _sanitize(event)
    record.setdefault("ts", _now())
    directory = env_probe.ensure_dir(env_probe.config_dir())
    path = directory / C.HISTORY_FILENAME
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        return None
    env_probe.chown_to_invoker(path)
    return record


def record(action, result, detail="", **fields):
    """便捷写法：``record("注入", "成功", power_w=350)``。"""
    event = {"action": action, "result": result, "detail": detail}
    event.update({k: v for k, v in fields.items() if v is not None})
    return append(event)


def tail(n: int = 50) -> list:
    """读取最近 ``n`` 条，**最新在前**。文件不存在或解析失败都返回空/跳过。"""
    path = history_file()
    if not path.is_file() or n <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if len(out) >= n:
            break
    return out


def export_text(path=None) -> Path:
    """把全部历史导出为可读文本（时间倒序）。失败抛 ``OSError``。"""
    target = Path(path) if path else env_probe.config_dir() / "history-export.txt"
    env_probe.ensure_dir(target.parent)
    entries = tail(100000)
    lines = ["# V100 快捷操作台 操作历史导出", f"# 导出时间：{_now()}",
             f"# 记录条数：{len(entries)}", ""]
    for item in entries:
        ts = item.get("ts", "未知时间")
        action = item.get("action", "未知动作")
        result = item.get("result", "")
        detail = item.get("detail", "")
        extras = " ".join(
            f"{k}={v}" for k, v in item.items()
            if k not in ("ts", "action", "result", "detail"))
        lines.append(f"[{ts}] {action} {result} {detail} {extras}".rstrip())
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_probe.chown_to_invoker(target)
    return target


def count() -> int:
    path = history_file()
    if not path.is_file():
        return 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0
