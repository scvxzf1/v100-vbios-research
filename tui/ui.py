"""TUI 表现层：主界面与菜单导航（tui/DESIGN.md §4 中 t5 的 ``tui/ui.py``）。

分层纪律（DESIGN §2.1，t8 会检查）：

* 本模块**不出现** ``subprocess``、``os.system``、``open("/proc/...")``，
  也不执行任何系统命令；界面里所有事实都来自数据层
  （``env_probe`` / ``gpu_state`` / ``rom_source`` / ``vbios_derive`` /
  ``payload`` / ``profile`` / ``history``），一切状态改变交给执行层
  （``inject`` / ``persist``，按需延迟导入，导入失败只降级不禁用整界面）。
* 全部用户可见文案为简体中文；宽度与换行统一走 :mod:`tui.widgets`，
  中文按 2 列计，超宽换行而不是截断（DESIGN §7.3）。
* 危险操作（注入 / 安装或卸载开机持久化 / 恢复原厂）一律走
  :class:`tui.widgets.ConfirmDialog`，必须键入完整中文确认词（DESIGN §6.3）。

对外接口：

* :func:`run` —— ``run(env=None) -> int``：启动界面，返回进程退出码
* :func:`main` —— ``main(argv=None) -> int``：带 ``--version`` / ``--help`` 的薄包装
* :class:`App` —— ``App(env=None).run() -> int``，供 ``tui/__main__.py`` 复用
"""
from __future__ import annotations

import locale
import sys

from . import constants as C
from . import env_probe, gpu_state, history, payload, profile, rom_source, vbios_derive
from . import widgets as W

__all__ = ["App", "run", "main", "T", "ERROR_CODE_TABLE"]

APP_TITLE = "V100 快捷操作台"
APP_VERSION = "1.0"

#: 低于此尺寸切单列精简布局（DESIGN §2.3）
MIN_COLS = 80
MIN_ROWS = 20
#: 低于此尺寸只显示「终端过小」提示，避免画成乱码
HARD_MIN_COLS = 40
HARD_MIN_ROWS = 12


# ─────────────────────────────────────────────────────────────────────────────
# 文案（DESIGN §7.1 术语表口径：覆盖镜像 / 注入 / 开机持久化 / 配置档案 / 刷写 /
# 摘卡 / 恢复原厂 / 试算 / 驱动 registry 接口 / 功耗上限 / 核心上报上限 …）
# ─────────────────────────────────────────────────────────────────────────────
T = {
    "title": f"{APP_TITLE} v{APP_VERSION}",
    "subtitle_format": "「{name}」",
    "menu_title": "功能",
    "detail_title": "详情",
    "narrow_hint": "终端过小，部分信息已省略。",
    "too_small": (
        "终端过小，界面已暂停渲染。\n"
        "本工具需要至少 {cols}×{rows}；当前 {cur_cols}×{cur_rows}。\n"
        "请放大终端窗口，或按 q 退出后改用 `python3 -m tui --once`（纯文本输出）。"
    ),
    "no_curses": (
        "错误[E0105] 当前 Python 缺少 curses 模块，无法进入界面。\n"
        "  建议：安装 ncurses 支持，或改用 `python3 -m tui --once`（纯文本输出）。"
    ),
    "labels": {
        "gpu": "显卡",
        "name": "型号",
        "vbios": "VBIOS",
        "power": "功耗上限",
        "sm": "核心上报上限",
        "clock_max": "频率上限",
        "mem": "显存频率",
        "override": "覆盖",
        "persist": "开机持久化",
        "rights": "权限",
        "driver": "驱动",
        "session": "图形会话",
        "unknown": "未知",
        "root": "root（可改动系统）",
        "readonly": "只读模式（未使用 sudo）",
        "present": "在位",
        "absent": "不在位",
        "enabled": "已启用",
        "disabled": "未启用",
        "no_v100": "未检测到 V100",
    },
    "menu": {
        "edit": "快速修改",
        "inject": "快速注入",
        "records": "记录与持久化",
        "status": "查看当前状态",
        "history": "查看历史",
        "quit": "退出",
    },
    "pages": {
        "edit": "快速修改",
        "inject": "快速注入",
        "records": "记录与持久化",
        "profiles": "配置档案",
        "persist_install": "安装开机持久化",
        "persist_uninstall": "卸载开机持久化",
        "restore": "恢复原厂",
        "status": "当前状态",
        "history": "操作历史",
        "help": "帮助：术语与错误码",
        "log": "本次会话消息",
    },
    "keys": {
        "menu": [("↑↓", "选择"), ("Enter", "进入"), ("F5", "刷新"),
                 ("?", "帮助"), ("l", "消息"), ("q", "退出")],
        "back": [("Esc", "返回")],
        "form": [("↑↓/Tab", "切换字段"), ("←→", "±1"), ("Shift+←→", "±10"),
                 ("Enter", "输入"), ("F10", "预览差异"), ("Esc", "返回")],
        "preview": [("Enter", "生成覆盖镜像"), ("Esc", "返回修改")],
        "result": [("s", "保存为档案"), ("i", "立即注入"), ("Esc", "返回主菜单")],
        "inject": [("Enter", "继续"), ("F5", "重新预检"), ("Esc", "返回")],
        "profiles": [("↑↓", "选择"), ("Enter", "载入到快速修改"), ("n", "新建"),
                     ("e", "改名或备注"), ("x", "删除"), ("F5", "刷新"), ("Esc", "返回")],
        "history": [("↑↓", "选择"), ("Enter", "详情"), ("d", "导出为文本"),
                    ("F5", "刷新"), ("Esc", "返回")],
        "status": [("F5", "重新读取"), ("?", "帮助"), ("Esc", "返回")],
        "text": [("↑↓", "滚动"), ("PgUp/PgDn", "翻页"), ("Esc", "返回")],
    },
    "records_menu": {
        "profiles": "配置档案列表",
        "install": "安装开机持久化",
        "uninstall": "卸载开机持久化",
        "restore": "恢复原厂",
        "back": "返回主菜单",
    },
    "inject_steps": [
        "① 停止 lactd（若在运行，先记录以便失败后恢复）",
        "② 停止显示管理器（{dm}）",
        "③ 卸载内核模块：nvidia_drm / nvidia_modeset / nvidia_uvm / nvidia（每个重试 3 次）",
        "④ 检查 /dev/nvidia* 是否仍被占用（有占用即中止）",
        "⑤ 摘卡：向 /sys/bus/pci/devices/{bdf}/remove 写 1",
        "⑥ 重新加载 nvidia 模块（此时只探测显示卡）",
        "⑦ 把覆盖镜像写入驱动 registry 接口",
        "⑧ 重新扫描总线，让卡带覆盖上线",
        "⑨ 校验 nvidia-smi 上报的功耗上限与核心上报上限",
        "⑩ 恢复 lactd 与显示管理器；任何阶段失败都回滚到原厂状态",
    ],
    "terms": [
        "覆盖镜像（payload）：写进驱动 registry 接口的 327,680 字节数据，掉电即失效。",
        "覆盖：内存覆盖机制本身，重启后消失，不改 flash。",
        "注入：把覆盖镜像通过摘卡 / 重新扫描总线的流程交给驱动，会中断图形会话。",
        "开机持久化：由 systemd 单元在开机时自动注入，需要安装到系统目录。",
        "配置档案（profile）：只记录语义值（W / MHz），字段偏移每次都重新推导。",
        "刷写：写 flash，本工具不提供刷写入口。",
        "摘卡 / 重新扫描总线：向 /sys 写 remove 与 rescan，不等于下电或热拔。",
        "恢复原厂：卸载模块后再加载，丢弃当前覆盖，不写 flash。",
        "试算：只生成覆盖镜像、不写硬件。",
        "驱动 registry 接口：/proc/driver/nvidia/registry，一键一值。",
        "功耗上限：nvidia-smi 的 power limit，本工具可写到 150–350 W。",
        "核心上报上限：Max SM clock 的上报值；1785 MHz 以上仅影响上报值。",
    ],
    "status_notes": {
        "mem_missing": "显存频率未取到（数据层暂未提供对应字段）。",
        "no_rom": "尚未读取基准 ROM；进入「快速修改」时才会读取。",
        "no_payload": "尚未生成覆盖镜像。",
    },
}

#: 错误码速查（编号登记见 tui/constants.py，界面只做展示）
ERROR_CODE_TABLE = (
    (C.ERR_PLATFORM, "不是 Linux 平台"),
    (C.ERR_NOT_ROOT, "需要 root 权限"),
    (C.ERR_INJECT_DISABLED, "注入入口已禁用"),
    (C.ERR_REGISTRY_MISSING, "驱动 registry 接口不可用"),
    (C.ERR_ROM_READ, "基准 ROM 读取失败"),
    (C.ERR_DERIVE, "字段偏移推导失败"),
    (C.ERR_ROM_TOO_SHORT, "ROM 长度不足"),
    (C.ERR_RANGE, "数值超出允许范围"),
    (C.ERR_RATIO, "比率字段与目标值不同构"),
    (C.ERR_OUTSIDE, "改动落在允许窗口之外"),
    (C.ERR_BUILD, "覆盖镜像生成失败"),
    (C.ERR_PROFILE_CORRUPT, "档案文件损坏"),
    (C.ERR_PROFILE_VERSION, "档案版本高于本工具"),
    (C.ERR_PROFILE_INVALID, "不是本工具的档案"),
    (C.ERR_PROFILE_IO, "档案读写失败"),
    (C.ERR_HISTORY_IO, "历史记录读写失败"),
    (C.WARN_SAME_AS_CURRENT, "目标值与现值相同"),
    (C.WARN_CLOCK_PHYSICAL, "超出 1785 MHz 物理上限"),
    (C.WARN_PERF_ALIGN, "Performance 字段按 4 MHz 对齐"),
    (C.WARN_ROM_MISMATCH, "档案基于另一份固件"),
    ("E0401", "目标设备不存在"),
    ("E0403", "设备被进程占用"),
    ("E0404", "已有注入任务在运行"),
    ("E0405", "覆盖镜像尺寸或 sha256 不符"),
    ("E0406", "运行或日志目录不可写"),
    ("E0407", "缺 systemd-run，无法安全提交独立单元"),
    ("E0408", "提交注入单元失败"),
    ("E0409", "提交恢复单元失败"),
    ("E0410", "半摘卡危险态"),
    ("E0502", "deploy/install.sh 或替换覆盖镜像失败"),
    ("E0503", "deploy/uninstall.sh 失败"),
    ("E0504", "deploy 未参数化，只能安装标准 350 W"),
    ("E0505", "覆盖镜像与档案记录不符"),
    ("E0506", "注入进行中，拒绝安装或卸载"),
    ("E0901", "界面内部错误（页面处理异常）"),
)


# ─────────────────────────────────────────────────────────────────────────────
# 数值 → 中文展示
# ─────────────────────────────────────────────────────────────────────────────
def _fmt_power(value) -> str:
    return "未知" if value is None else f"{float(value):g} W"


def _fmt_mhz(value) -> str:
    return "未知" if value is None else f"{int(value)} MHz"


def _fmt_tri(value, yes: str, no: str, unknown: str = "未知") -> str:
    if value is None:
        return unknown
    return yes if value else no


def _fmt_count(value) -> str:
    """字节数按千分位展示（DESIGN §7.3 的「327,680 字节」口径）。"""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "未知"


def _fmt_path(value, keep_parts: int = 3) -> str:
    """展示用的路径缩短：绝对路径只保留末尾几段，避免整条个人目录刷满状态行。

    只影响**显示**，不改任何实际路径；仓库内相对路径原样保留。
    """
    text = str(value or "")
    if not text.startswith("/"):
        return text
    parts = [part for part in text.split("/") if part]
    if len(parts) <= keep_parts:
        return text
    return "…/" + "/".join(parts[-keep_parts:])


def _fmt_sha(text, keep: int = 12) -> str:
    text = str(text or "")
    return text if len(text) <= keep else text[:keep] + "…"


_MEM_CLOCK_ATTRS = ("mem_max_mhz", "max_mem_mhz", "mem_clock_mhz")


def _mem_clock(gpu):
    """显存频率：数据层若提供就直接用，否则返回 ``None``（界面显示「未知」）。"""
    for attr in _MEM_CLOCK_ATTRS:
        value = getattr(gpu, attr, None)
        if value is not None:
            return value
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 应用主体
# ─────────────────────────────────────────────────────────────────────────────
class App:
    """主界面与页面路由。

    :param env: 已探测的环境（:class:`tui.env_probe.EnvInfo`）；缺省自行探测
    :param deps: 可选的替身字典（便于离线测试注入``{"snapshot": fn}``等）
    """

    def __init__(self, env=None, deps=None):
        self.deps = dict(deps or {})
        self.env = env if env is not None else env_probe.probe()
        self.theme = None
        self.snap = None                 # gpu_state.Snapshot
        self.rom = None                  # rom_source.RomCandidate
        self.layout = None               # vbios_derive.Layout
        self.intent = None               # payload.EditIntent
        self.payload = None              # payload.Payload
        self.payload_path = None         # 覆盖镜像落盘路径
        self.rom_path = self.deps.get("rom_path")
        self.message = "↑↓ 选择功能，Enter 进入，q 退出。"
        self.message_kind = W.DIM
        self.log_lines = []
        self.handoff = ""                # 注入提交后要在退出时打印的交接说明
        self.exit_code = 0
        self.narrow = False
        self.menu_index = 0
        self._exec_errors = []

    # ── 依赖替身 ────────────────────────────────────────────────────
    def _fn(self, name: str, default):
        return self.deps.get(name, default)

    # ── 消息 ────────────────────────────────────────────────────────
    def _note(self, text: str, kind: str = W.DIM) -> None:
        self.message = text
        self.message_kind = kind
        self.log_lines.append(text)
        if len(self.log_lines) > 400:
            del self.log_lines[:100]

    def _ok(self, text: str) -> None:
        self._note(W.fmt_ok(text), W.OK)

    def _warn(self, code: str, text: str) -> None:
        self._note(W.fmt_warn(code, text), W.WARN)

    def _error(self, code: str, cause: str, advice: str = "") -> None:
        self._note(f"错误[{code}] {cause}", W.ERR)
        if advice:
            self.log_lines.append(f"  建议：{advice}")

    # ── 状态刷新（只读探测，绝不变更系统）────────────────────────────
    def refresh(self) -> None:
        snapshot_fn = self._fn("snapshot", gpu_state.snapshot)
        try:
            try:
                self.snap = snapshot_fn(env=self.env)
            except TypeError:                    # 替身可能只接受位置参数
                self.snap = snapshot_fn()
        except Exception as exc:                    # 数据层异常不得打挂界面
            self.snap = None
            self._error("E0901", f"状态读取失败：{exc}", "可稍后按 F5 重试。")
            return
        self._resume_notice()

    # ── 启动闸门与主循环 ────────────────────────────────────────────
    def run(self) -> int:
        """启动界面，返回进程退出码（0 正常 / 2 平台或终端不可用）。"""
        if not getattr(self.env, "platform_ok", True):
            platform = getattr(self.env, "platform", "未知")
            sys.stderr.write(env_probe.LINUX_ONLY_MESSAGE.format(platform=platform) + "\n")
            return 2
        if not W.CURSES_AVAILABLE:
            sys.stderr.write(T["no_curses"] + "\n")
            return 2
        try:
            import curses
        except Exception:                            # pragma: no cover
            sys.stderr.write(T["no_curses"] + "\n")
            return 2
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass                                     # 中文输入退化为 ASCII，不影响显示
        self.refresh()
        try:
            code = curses.wrapper(self._main)
        except KeyboardInterrupt:
            code = 0
        if self.handoff:
            sys.stdout.write("\n" + self.handoff + "\n")
            sys.stdout.flush()
        return int(code or 0)

    def _main(self, stdscr) -> int:
        import curses
        stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except Exception:
            pass
        try:
            curses.ESCDELAY = 25
        except Exception:
            pass
        self.theme = W.Theme(stdscr)
        try:
            size = stdscr.getmaxyx()
            self.narrow = size[1] < MIN_COLS or size[0] < MIN_ROWS
        except Exception:
            self.narrow = False
        hint = self._take_resume_hint()
        if hint is not None:
            self._show_resume_hint(stdscr, hint)
        return self._menu_loop(stdscr)

    # ── 主菜单 ──────────────────────────────────────────────────────
    def _menu_spec(self):
        env = self.env
        labels = T["menu"]
        items = [
            W.MenuItem(labels["edit"], "edit",
                       enabled=bool(env.can_edit),
                       reason=env.disable_reason("edit")
                       or "当前环境无法生成覆盖镜像。",
                       hint="只做本地字节运算：不写 registry、不摘卡、不动驱动。"),
            W.MenuItem(labels["inject"], "inject",
                       enabled=False,
                       reason="尚未生成覆盖镜像，请先使用「快速修改」。",
                       hint="提交为独立 systemd 单元，会中断图形会话。"),
            W.MenuItem(labels["records"], "records", enabled=True,
                       hint="配置档案、开机持久化、恢复原厂。"),
            W.MenuItem(labels["status"], "status", enabled=True,
                       hint="只读展示显卡、驱动与持久化的当前事实。"),
            W.MenuItem(labels["history"], "history", enabled=True,
                       hint="追加式 JSONL，只读视图。"),
            W.MenuItem(labels["quit"], "quit", enabled=True),
        ]
        # 注入入口需要同时满足：环境允许 + 已有覆盖镜像；禁用原因优先说环境问题
        inject_item = items[1]
        env_reason = env.disable_reason("inject")
        payload_reason = (None if self.payload_path is not None
                          else "尚未生成覆盖镜像，请先使用「快速修改」。")
        inject_item.enabled = bool(env.can_inject and self.payload_path is not None)
        inject_item.reason = env_reason or payload_reason or ""
        return items

    def _menu_loop(self, stdscr) -> int:
        menu = W.Menu(self._menu_spec(), self.menu_index)
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            if cols < HARD_MIN_COLS or rows < HARD_MIN_ROWS:
                self._draw_too_small(stdscr, frame)
                key = W.read_key(stdscr)
                if key in ("q", "Q"):
                    return 0
                self.refresh()
                continue
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame)
            footer_row = rows - 2
            panel_h = max(6, footer_row - body_top)
            if self.narrow:
                menu.render(frame, body_top, 0, min(panel_h, len(menu.items) + 2), cols)
                frame.text(body_top + min(panel_h, len(menu.items) + 2), 0,
                           W.fit(T["narrow_hint"], cols), W.DIM)
            else:
                menu_w = max(24, min(30, cols // 3))
                menu_h = min(panel_h, len(menu.items) + 2)
                menu.render(frame, body_top, 0, menu_h, menu_w)
                self._render_menu_hint(frame, menu, body_top + menu_h, 0,
                                       panel_h - menu_h, menu_w)
                self._render_status_panel(frame, body_top, menu_w + 1, panel_h,
                                          cols - menu_w - 1)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["menu"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key:
                continue
            if key == W.KEY_F5:
                self.refresh()
                self._ok("已重新读取当前状态。")
                menu = W.Menu(self._menu_spec(), menu.index)
                continue
            if key in ("q", "Q"):
                return self.exit_code
            if key == "?":
                self._help(stdscr)
                continue
            if key == "l":
                self._text_pane(stdscr, T["pages"]["log"], self.log_lines or ["（暂无消息）"])
                continue
            if key in (W.KEY_UP, "k"):
                menu.move(-1)
                continue
            if key in (W.KEY_DOWN, "j"):
                menu.move(1)
                continue
            if key == W.KEY_RESIZE:
                self.refresh()
                continue
            if W.is_enter(key):
                ok, action = menu.activate()
                self.menu_index = menu.index
                if not ok:
                    self._modal(stdscr, "该入口当前不可用",
                                W.fmt_error(self._disabled_code(menu.current), action),
                                W.WARN)
                    continue
                if action == "quit":
                    return self.exit_code
                # 页面可以链式跳转（例如「生成完成 → 立即注入」）
                while True:
                    result = self._dispatch(action, stdscr)
                    if result == "quit":
                        return self.exit_code
                    if not result or result == "menu":
                        break
                    action = result
                menu = W.Menu(self._menu_spec(), menu.index)

    @staticmethod
    def _disabled_code(item) -> str:
        """禁用原因对应的错误码（DESIGN §7.2 分区：E01xx 环境与权限）。"""
        action = getattr(item, "action", "")
        if action == "inject":
            return C.ERR_INJECT_DISABLED
        if action in ("install", "uninstall", "restore"):
            return C.ERR_NOT_ROOT
        return C.ERR_PLATFORM

    def _dispatch(self, action: str, stdscr) -> str:
        handlers = {
            "edit": self._page_edit,
            "inject": self._page_inject,
            "records": self._page_records,
            "status": self._page_status,
            "history": self._page_history,
        }
        handler = handlers.get(action)
        if handler is None:
            return "menu"
        try:
            return handler(stdscr) or "menu"
        except Exception as exc:                 # 页面异常不得打挂整个界面
            self._error("E0901", f"页面处理异常：{exc}", "请按 F5 刷新后重试。")
            self._modal(stdscr, "界面异常", [
                f"错误[E0901] 页面处理异常：{exc}",
                "  建议：按 F5 刷新后重试；若反复出现请记录复现步骤。",
            ], W.ERR)
            return "menu"

    # ── 顶部状态栏 ──────────────────────────────────────────────────
    def _banner(self) -> str:
        env = self.env
        text = getattr(env, "banner", "") or ""
        if not text:
            text = T["labels"]["root"] if env.is_root else T["labels"]["readonly"]
        return text

    def _status_fields(self) -> list:
        """顶部状态栏字段（DESIGN §2.3）：VBIOS / 功耗 / 频率 / 显存 / 覆盖 / 持久化 / 权限。"""
        snap = self.snap
        gpu = getattr(snap, "gpu", None) if snap else None
        labels = T["labels"]
        bdf = (getattr(snap, "v100_bdf", None) if snap else None) \
            or getattr(gpu, "bdf", None) or labels["no_v100"]
        power = getattr(gpu, "power_limit_w", None)
        power_max = getattr(gpu, "power_max_w", None)
        power_text = labels["unknown"] if power is None else _fmt_power(power)
        if power_max is not None:
            power_text = f"{power_text} / 上限 {_fmt_power(power_max)}"
        fields = [
            (labels["gpu"], bdf, W.NORMAL),
            (labels["name"], getattr(gpu, "name", None) or labels["unknown"], W.NORMAL),
            (labels["vbios"], getattr(gpu, "vbios_version", None) or labels["unknown"],
             W.NORMAL),
            (labels["power"], power_text, W.NORMAL),
            (labels["sm"], _fmt_mhz(getattr(gpu, "sm_max_mhz", None)), W.NORMAL),
        ]
        if not self.narrow:
            # 窄终端让 Applications 时钟（频率上限）先让位，
            # 保证显存 / 覆盖 / 持久化 / 权限这几项始终可见
            fields.append((labels["clock_max"],
                           _fmt_mhz(getattr(gpu, "apps_graphics_mhz", None)), W.NORMAL))
        fields.append((labels["mem"], _fmt_mhz(_mem_clock(gpu)), W.NORMAL))
        fields.append((labels["override"],
                       _fmt_tri(getattr(snap, "override", None) if snap else None,
                                labels["present"], labels["absent"]), W.NORMAL))
        fields.append((labels["persist"], self._persist_short(), W.NORMAL))
        fields.append((labels["rights"],
                       labels["root"] if self.env.is_root else labels["readonly"],
                       W.OK if self.env.is_root else W.WARN))
        return fields

    def _persist_short(self) -> str:
        boot = (getattr(self.snap, "boot", None) or {}) if self.snap else {}
        labels = T["labels"]
        enabled = boot.get("enabled")
        if boot.get("unknown"):
            return labels["unknown"]
        return _fmt_tri(enabled, labels["enabled"], labels["disabled"])

    def _draw_top(self, frame: W.Frame, subtitle: str = "") -> int:
        rows, cols = frame.size
        title = T["title"] if not subtitle else f"{T['title']}  « {subtitle} »"
        frame.row(0, W.fit(title, cols), W.TITLE)
        banner = self._banner()
        if banner and W.dwidth(title) + W.dwidth(banner) + 4 <= cols:
            frame.text(0, cols - W.dwidth(banner) - 1, banner, W.WARN)
        height = 2 if rows < 16 else 3
        bar = W.StatusBar(fields=self._status_fields())
        used = bar.render(frame, 1, 0, cols, height)
        line_row = min(rows - 1, 1 + used)
        frame.hline(line_row, 0, cols, "─")
        return min(rows, line_row + 1)

    def _render_status_panel(self, frame: W.Frame, y: int, x: int, height: int,
                             width: int) -> None:
        if height < 4 or width < 20:
            return
        frame.box(y, x, height, width, T["detail_title"])
        lines = self._detail_lines()
        frame.lines(y + 1, x + 2, width - 4, lines, W.NORMAL, max_rows=height - 2)

    def _detail_lines(self) -> list:
        snap = self.snap
        gpu = getattr(snap, "gpu", None) if snap else None
        lines = []
        if gpu is not None:
            lines.append(f"型号：{gpu.name or '未知'}")
            lines.append(f"设备地址：{gpu.bdf or '未知'}")
            temp = "未知" if gpu.temp_c is None else f"{gpu.temp_c} °C"
            lines.append(f"温度：{temp}　当前核心：{_fmt_mhz(gpu.sm_current_mhz)}")
            lines.append(f"出厂功耗上限：{_fmt_power(gpu.power_default_w)}")
            if _mem_clock(gpu) is None:
                lines.append(W.fmt_progress(T["status_notes"]["mem_missing"]))
        lines.append(f"覆盖：{self._override_text()}    "
                     f"开机持久化：{self._persist_short()}")
        lines.append(f"驱动 registry 接口：{'可用' if self.env.registry_ok else '不可用'}    "
                     f"systemd-run：{'可用' if self.env.systemd_run_ok else '不可用'}")
        lines.append(f"图形会话：{self.env.dm_unit or '未探测到'}"
                     f"（{'运行中' if self.env.dm_active else '未运行'}）    "
                     f"lactd：{_fmt_tri(self.env.lactd_active, '运行中', '未运行')}")
        if self.layout is not None:
            lines.append(f"基准 ROM：{self._rom_source_text()}")
            lines.append(f"ROM sha256：{_fmt_sha(getattr(self.rom, 'sha256', ''))}    "
                         f"字段定位：已推导")
        else:
            lines.append(W.fmt_progress(T["status_notes"]["no_rom"]))
        if self.payload_path is not None:
            lines.append(f"覆盖镜像：{self.payload_path.name}"
                         f"（{_fmt_sha(getattr(self.payload, 'sha256', ''))}）")
        else:
            lines.append(W.fmt_progress(T["status_notes"]["no_payload"]))
        for text in (getattr(snap, "notes", None) or []) if snap else []:
            lines.append(W.fmt_progress(text))
        for text in self.env.reasons or []:
            lines.append(W.fmt_progress(text))
        return lines

    def _override_text(self) -> str:
        labels = T["labels"]
        value = getattr(self.snap, "override", None) if self.snap else None
        return _fmt_tri(value, labels["present"], labels["absent"])

    def _render_menu_hint(self, frame: W.Frame, menu: W.Menu, y: int, x: int,
                          height: int, width: int) -> None:
        if height < 4 or width < 20:
            return
        item = menu.current
        lines = []
        if item is not None:
            lines.append(f"· {item.label}")
            if item.hint:
                lines.extend(W.wrap(item.hint, max(4, width - 4)))
            if not item.enabled:
                lines.append("")
                lines.extend(W.fmt_error(C.ERR_INJECT_DISABLED if item.action == "inject"
                                         else C.ERR_PLATFORM, item.reason))
            else:
                lines.append("")
                lines.append("Enter 进入")
        panel = W.Panel("当前项", lines)
        panel.render(frame, y, x, height, width)

    def _draw_message(self, frame: W.Frame, y: int) -> None:
        kind = self.message_kind or W.DIM
        text = self.message or ""
        symbol = W.STATUS_SYMBOL.get(kind, "")
        if symbol and not text.startswith(symbol):
            text = f"{symbol} {text}"
        frame.hline(y - 1, 0, frame.cols, "─")
        frame.row(y, text, kind)

    def _draw_keys(self, frame: W.Frame, y: int, pairs) -> None:
        frame.row(y, W.key_legend(pairs), W.DIM)

    def _draw_too_small(self, stdscr, frame: W.Frame) -> None:
        cols, rows = frame.cols, frame.rows
        stdscr.erase()
        text = T["too_small"].format(cols=MIN_COLS, rows=MIN_ROWS,
                                     cur_cols=cols, cur_rows=rows)
        frame.lines(0, 0, cols, text.split("\n"), W.WARN)
        frame.row(rows - 1, "按 q 退出；放大终端后自动恢复。", W.DIM)
        stdscr.refresh()

    # ── 通用浮层 ────────────────────────────────────────────────────
    def _modal(self, stdscr, title: str, lines, kind: str = W.NORMAL,
               footer: str = "任意键返回") -> None:
        W.MessageBox(title, lines, kind).run(W.Frame(stdscr, self.theme), footer)

    def _text_pane(self, stdscr, title: str, lines) -> None:
        W.TextPane(title, lines).run(W.Frame(stdscr, self.theme),
                                     W.key_legend(T["keys"]["text"]))

    def _confirm(self, stdscr, title: str, lines, word: str, danger: bool = True,
                 prompt: str = "") -> bool:
        return W.ConfirmDialog(title, lines, word, danger=danger).run(
            W.Frame(stdscr, self.theme), prompt)

    def _busy(self, stdscr, text: str, subtitle: str = "") -> None:
        frame = W.Frame(stdscr, self.theme)
        stdscr.erase()
        top = self._draw_top(frame, subtitle)
        W.ProgressLine(text).render(frame, min(top, frame.rows - 1), 0, frame.cols)
        stdscr.refresh()

    # ── 启动时的危险态提示（DESIGN §9.4）────────────────────────────
    def _resume_notice(self) -> None:
        self._resume = None
        try:
            from . import inject as inject_mod          # 执行层只读接口
        except Exception as exc:
            self._exec_errors.append(f"执行层不可用：{exc}")
            return
        try:
            hint = inject_mod.resume_hint()
        except Exception as exc:
            self._exec_errors.append(f"上次任务状态读取失败：{exc}")
            return
        self._resume = hint

    def _take_resume_hint(self):
        return getattr(self, "_resume", None)

    def _show_resume_hint(self, stdscr, hint) -> None:
        lines = [getattr(hint, "title", "未完成任务"), ""]
        detail = getattr(hint, "detail", "")
        if detail:
            lines.extend(W.wrap(detail, 68))
        advice = getattr(hint, "advice", "")
        if advice:
            lines.append("")
            lines.extend(W.wrap(advice, 68))
        actions = getattr(hint, "actions", ()) or ()
        if actions:
            lines.append("")
            lines.append("可执行动作：" + "；".join(actions))
        self._modal(stdscr, "检测到上次任务可能未完成", lines, W.WARN)

    # ── 帮助 ────────────────────────────────────────────────────────
    def _help(self, stdscr) -> None:
        lines = ["【术语表】"]
        for text in T["terms"]:
            lines.append(f"· {text}")
        lines.append("")
        lines.append("【错误与提示编号速查】")
        for code, meaning in ERROR_CODE_TABLE:
            lines.append(f"· {code}　{meaning}")
        lines.append("")
        lines.append("【按键】")
        for title, pairs in T["keys"].items():
            lines.append(f"· {W.key_legend(pairs)}")
        self._text_pane(stdscr, T["pages"]["help"], lines)

    # ── 一键准备：读 ROM + 推导字段表 ───────────────────────────────
    def _rom_source_text(self) -> str:
        """基准 ROM 来源的中文说明（绝对路径已缩短，不展示完整个人目录）。"""
        rom = self.rom
        if rom is None:
            return "未知"
        label = str(getattr(rom, "source_label", "") or "")
        raw = _fmt_path(getattr(rom, "source", "") or "")
        if label and raw and label != raw:
            return f"{label}（{raw}）"
        return raw or label or "未知"

    def _prepare_layout(self, stdscr) -> bool:
        if self.layout is not None:
            return True
        loader = self._fn("load_rom", rom_source.load_rom)
        self._busy(stdscr, "正在读取基准 ROM…（可能需要数十秒，按 Esc 需在结束后返回）",
                   T["pages"]["edit"])
        try:
            candidate = loader(bdf=getattr(self.snap, "v100_bdf", None),
                               explicit_path=self.rom_path,
                               allow_sys_enable=bool(self.env.is_root))
        except rom_source.RomError as exc:
            self._error(getattr(exc, "code", C.ERR_ROM_READ), str(exc))
            self._modal(stdscr, "基准 ROM 读取失败",
                        W.fmt_error(getattr(exc, "code", C.ERR_ROM_READ),
                                    getattr(exc, "reason", str(exc)),
                                    getattr(exc, "advice", "")), W.ERR)
            return False
        except Exception as exc:
            self._error(C.ERR_ROM_READ, f"基准 ROM 读取异常：{exc}")
            self._modal(stdscr, "基准 ROM 读取失败",
                        W.fmt_error(C.ERR_ROM_READ, f"读取异常：{exc}",
                                    "请用 --rom 指定固件路径后重试。"), W.ERR)
            return False
        self._busy(stdscr, "正在推导字段偏移…（失败即中止，不猜偏移）", T["pages"]["edit"])
        try:
            layout = vbios_derive.derive_layout(candidate.data)
        except vbios_derive.DeriveError as exc:
            self._error(getattr(exc, "code", C.ERR_DERIVE), str(exc))
            self._modal(stdscr, "字段定位失败",
                        W.fmt_error(getattr(exc, "code", C.ERR_DERIVE),
                                    getattr(exc, "reason", str(exc)),
                                    getattr(exc, "advice", "")), W.ERR)
            return False
        except Exception as exc:
            self._error(C.ERR_DERIVE, f"字段推导异常：{exc}")
            self._modal(stdscr, "字段定位失败",
                        W.fmt_error(C.ERR_DERIVE, f"推导异常：{exc}",
                                    "请确认 tools/derive_tables.py 未被改动。"), W.ERR)
            return False
        self.rom = candidate
        self.layout = layout
        if self.intent is None:
            self.intent = payload.EditIntent.from_layout(layout)
        self._ok(f"基准 ROM 已就绪（{self._rom_source_text()}，"
                 f"sha256 {_fmt_sha(candidate.sha256)}）。")
        return True

    # ── 快速修改 ────────────────────────────────────────────────────
    def _edit_fields(self) -> list:
        layout = self.layout
        labels = layout.labels()
        current = layout.current_values()
        start = self.intent or payload.EditIntent.from_layout(layout)
        power = start.power_w if start.power_w is not None else current[C.FIELD_POWER]
        fields = [
            W.NumberField(
                C.FIELD_POWER, labels[C.FIELD_POWER], power,
                minimum=max(float(C.POWER_MIN_W), float(layout.power_low_w)),
                maximum=float(C.POWER_MAX_W),
                step=10.0, decimals=int(C.POWER_DECIMALS) or 1, unit="W",
                coarse_multiplier=5.0,
                hint=(f"范围 {C.POWER_MIN_W:.0f}–{C.POWER_MAX_W:.0f} W，"
                      f"←→ 每档 10 W；必须大于该行下限 {layout.power_low_w:.0f} W"),
            ),
        ]
        for key in C.CLOCK_FIELDS:
            value = start.clocks.get(key)
            if value is None:
                value = current.get(key)
            fields.append(W.NumberField(
                key, labels.get(key, key), value,
                minimum=int(C.CLOCK_MIN_MHZ), maximum=int(C.CLOCK_MAX_MHZ),
                step=1, decimals=0, unit="MHz", coarse_multiplier=10.0,
                hint=(f"范围 {C.CLOCK_MIN_MHZ}–{C.CLOCK_MAX_MHZ} MHz；"
                      f"←→ ±1，Shift+←→ ±10；现值 {current.get(key, '未知')} MHz"),
            ))
        return fields

    def _intent_from_form(self, form: W.Form):
        values = form.values()
        clocks = {k: int(values[k]) for k in C.CLOCK_FIELDS if values.get(k) is not None}
        power = values.get(C.FIELD_POWER)
        return payload.EditIntent(power_w=None if power is None else float(power),
                                  clocks=clocks)

    def _page_edit(self, stdscr) -> str:
        if not self.env.can_edit:
            self._modal(stdscr, "快速修改不可用",
                        W.fmt_error(C.ERR_PLATFORM,
                                    self.env.disable_reason("edit") or "当前环境不可用。"),
                        W.ERR)
            return "menu"
        if not self._prepare_layout(stdscr):
            return "menu"
        form = W.Form(self._edit_fields(), title="字段与目标值")
        self.intent = self._intent_from_form(form)
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            if cols < HARD_MIN_COLS or rows < HARD_MIN_ROWS:
                self._draw_too_small(stdscr, frame)
                if W.read_key(stdscr) in ("q", "Q"):
                    return "menu"
                continue
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame, T["pages"]["edit"])
            footer_row = rows - 2
            panel_h = max(8, footer_row - body_top)
            form_h = panel_h
            if self.narrow:
                form.render(frame, body_top, 0, form_h, cols)
            else:
                form_w = max(34, cols // 2)
                form.render(frame, body_top, 0, form_h, form_w)
                self._render_edit_detail(frame, body_top, form_w + 1, form_h,
                                         cols - form_w - 1)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["form"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if W.is_cancel(key) and not form.editing:
                return "menu"
            if key in ("q", "Q") and form.editing is False:
                return "menu"
            action = form.handle_key(key)
            if action == "submit":
                intent = self._intent_from_form(form)
                problems = form.check_all()
                if problems:
                    form.set_message(problems[0], W.ERR)
                    continue
                issues = payload.validate_intent(intent, self.layout)
                blocking = payload.blocking_issues(issues)
                if blocking:
                    form.set_message(f"错误[{blocking[0].code}] {blocking[0].message}", W.ERR)
                    continue
                self.intent = intent
                result = self._edit_preview(stdscr, issues)
                if result == "generate":
                    generated = self._edit_generate(stdscr)
                    if generated == "inject":
                        return "inject"
                    if generated == "menu":
                        return "menu"
                    # 生成成功后重新起一张表单，值取最新意图
                    form = W.Form(self._edit_fields(), title="字段与目标值")
                continue
            if action == "change":
                self.intent = self._intent_from_form(form)

    def _render_edit_detail(self, frame: W.Frame, y: int, x: int, height: int,
                            width: int) -> None:
        if height < 4 or width < 20:
            return
        layout = self.layout
        labels = layout.labels()
        current = layout.current_values()
        lines = ["字段：现值 → 目标"]
        for key in (C.FIELD_POWER,) + C.CLOCK_FIELDS:
            unit = "W" if key == C.FIELD_POWER else "MHz"
            cur = current.get(key)
            target = (self.intent.power_w if key == C.FIELD_POWER and self.intent
                      else (self.intent.clocks.get(key) if self.intent else None))
            label = labels.get(key, key)
            cur_text = "未知" if cur is None else f"{float(cur):g} {unit}"
            target_text = "未改动" if target is None else f"{float(target):g} {unit}"
            marker = "" if target is None or cur is None or float(target) == float(cur) \
                else "  ←"
            lines.append(f"· {label}：{cur_text} → {target_text}{marker}")
        lines.append("")
        lines.append(f"基准 ROM：{self._rom_source_text()}")
        lines.append(f"sha256：{_fmt_sha(getattr(self.rom, 'sha256', ''))}"
                     f"　长度：{_fmt_count(getattr(self.rom, 'size', layout.rom_length))} 字节")
        lines.append(f"数据镜像：[{layout.data_image_start:#x}..{layout.data_image_end:#x}]"
                     f"　校验和末字节：{layout.checksum_at:#x}")
        for warning in layout.warnings or []:
            lines.append(W.fmt_warn(C.WARN_ROM_MISMATCH, str(warning)))
        if any((self.intent.clocks.get(k) or 0) > C.WARN_CLOCK_MHZ for k in C.CLOCK_FIELDS):
            lines.append(W.fmt_warn(C.WARN_CLOCK_PHYSICAL,
                                    f"{C.WARN_CLOCK_MHZ} MHz 以上为出厂电压下的物理上限，"
                                    f"超出部分只影响上报值，实际频率不会提升。"))
        frame.box(y, x, height, width, "现值与目标")
        frame.lines(y + 1, x + 2, width - 4, lines, W.NORMAL, max_rows=height - 2)

    def _edit_preview(self, stdscr, issues) -> str:
        """差异预览页：确认后才会调用数据层生成覆盖镜像。"""
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            stdscr.erase()
            body_top = self._draw_top(frame, "预览差异")
            footer_row = rows - 2
            lines = self._diff_lines(issues)
            panel_h = max(8, footer_row - body_top)
            W.Panel("即将写入的语义值（不触硬件）", lines, W.NORMAL).render(
                frame, body_top, 0, panel_h, cols)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["preview"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if W.is_cancel(key) or key in ("q", "Q"):
                return "back"
            if W.is_enter(key):
                return "generate"

    def _diff_lines(self, issues) -> list:
        layout = self.layout
        labels = layout.labels()
        lines = []
        diff = (self.intent.diff_against(layout) if self.intent else {})
        if not diff:
            lines.append(W.fmt_warn(C.WARN_SAME_AS_CURRENT,
                                    "目标值与现值相同，无需生成覆盖镜像。"))
        for key, pair in diff.items():
            current, target = pair
            unit = "W" if key == C.FIELD_POWER else "MHz"
            label = labels.get(key, key)
            lines.append(f"· {label}：{float(current):g} {unit} → {float(target):g} {unit}")
        lines.append("")
        lines.append("说明：本页只做本地字节运算，不写驱动 registry 接口、不摘卡、不动驱动。")
        lines.append("")
        for issue in issues or []:
            text = f"{issue.message}"
            if issue.level == "error":
                lines.append(W.fmt_error(issue.code, text)[0])
            else:
                lines.append(W.fmt_warn(issue.code, text))
        if not issues:
            lines.append(W.fmt_ok("目标值通过全部校验。"))
        return lines

    def _edit_generate(self, stdscr) -> str:
        """调用数据层生成并回读校验，然后展示摘要。"""
        self._busy(stdscr, "正在生成覆盖镜像并回读校验…", "生成覆盖镜像")
        try:
            built = payload.build(self.rom.data, self.intent, self.layout)
        except payload.PayloadError as exc:
            self._error(exc.code, exc.reason, exc.advice)
            self._modal(stdscr, "覆盖镜像生成失败",
                        W.fmt_error(exc.code, exc.reason, exc.advice), W.ERR)
            return "stay"
        except Exception as exc:
            self._error(C.ERR_BUILD, f"生成异常：{exc}")
            self._modal(stdscr, "覆盖镜像生成失败",
                        W.fmt_error(C.ERR_BUILD, f"生成异常：{exc}",
                                    "请用 tools/build_payload.py 复现。"), W.ERR)
            return "stay"
        try:
            path = payload.write(built, name_hint=f"v100-{float(self.intent.power_w):g}w")
        except payload.PayloadError as exc:
            self._error(exc.code, exc.reason, exc.advice)
            self._modal(stdscr, "覆盖镜像落盘失败",
                        W.fmt_error(exc.code, exc.reason, exc.advice), W.ERR)
            return "stay"
        self.payload = built
        self.payload_path = path
        self._ok(f"覆盖镜像已生成：{path.name}（{_fmt_count(built.size)} 字节，sha256 "
                 f"{_fmt_sha(built.sha256)}）。")
        self._record("生成", "成功",
                     f"功耗 {float(self.intent.power_w):g} W，sha256 {built.sha256[:8]}",
                     payload_sha256=built.sha256, size=built.size)
        return self._edit_result(stdscr)

    def _edit_result(self, stdscr) -> str:
        built = self.payload
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            stdscr.erase()
            body_top = self._draw_top(frame, "生成结果")
            footer_row = rows - 2
            lines = self._result_lines(built)
            W.Panel("覆盖镜像摘要", lines, W.NORMAL).render(
                frame, body_top, 0, max(8, footer_row - body_top), cols)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["result"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if key in ("s", "S"):
                self._save_profile_dialog(stdscr)
                continue
            if key in ("i", "I"):
                if self.payload_path is None:
                    self._modal(stdscr, "无法注入", ["尚未生成覆盖镜像。"], W.WARN)
                    continue
                return "inject"
            if W.is_cancel(key) or key in ("q", "Q"):
                return "menu"

    def _result_lines(self, built) -> list:
        layout = self.layout
        summary = built.summary or {}
        first = summary.get("first_offset")
        last = summary.get("last_offset")
        lines = [
            f"· 体积：{_fmt_count(built.size)} 字节"
            f"（约定 {_fmt_count(C.PAYLOAD_WINDOW_LENGTH)} 字节）",
            f"· sha256：{built.sha256}",
            f"· 落盘路径：{self.payload_path.name if self.payload_path else '未落盘'}",
            f"· 改动字节数：{_fmt_count(summary.get('changed_bytes', 0))} 个",
        ]
        if first is not None and last is not None:
            inside = layout.data_image_start <= first and last < layout.data_image_end
            lines.append(f"· 改动区间：[{first:#x}..{last:#x}]"
                         f"（{'落在唯一数据镜像内' if inside else '警告：越出数据镜像'}）")
        try:
            old = self.rom.data[layout.checksum_at]
            new = built.data[layout.checksum_at - C.PAYLOAD_WINDOW_START]
            lines.append(f"· 校验和末字节：{old:#04x} → {new:#04x}（已重算）")
        except Exception:
            lines.append(W.fmt_progress("校验和末字节：本固件结构不支持直接比对"))
        lines.append(W.fmt_ok("回读校验通过，字段逐项与目标一致。"))
        for warning in (summary.get("warnings") or []):
            lines.append(W.fmt_warn(C.WARN_CLOCK_PHYSICAL, str(warning)))
        lines.append("")
        lines.append("下一步：s 保存为配置档案　i 立即注入（会中断图形会话）　Esc 返回主菜单")
        return lines

    @staticmethod
    def _dialog_submit(action: str, form: W.Form) -> bool:
        """对话框的提交判定：F10，或最后一个字段回车确认后即提交。"""
        if action == "submit":
            return True
        return action == "commit" and form.index >= len(form.fields) - 1

    def _save_profile_dialog(self, stdscr) -> None:
        if self.intent is None or self.layout is None:
            self._modal(stdscr, "无法保存", ["当前没有可保存的目标值。"], W.WARN)
            return
        default_name = f"{float(self.intent.power_w):g}W-快速档"
        form = W.Form([
            W.TextField("display_name", "档案名称", default_name, max_len=C.SLUG_MAX_LEN,
                        allow_empty=False, hint="显示名称可含中文；文件名会自动转成 slug"),
            W.TextField("note", "备注", "", max_len=60, hint="可留空"),
        ], title="保存为配置档案")
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            stdscr.erase()
            body_top = self._draw_top(frame, "保存为配置档案")
            form.render(frame, body_top, 0, max(8, rows - 3 - body_top), cols)
            self._draw_message(frame, rows - 2)
            self._draw_keys(frame, rows - 1, [("Enter", "保存"), ("Esc", "取消")])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if W.is_cancel(key) and not form.editing:
                return
            if key in ("q", "Q") and not form.editing:
                return
            action = form.handle_key(key)
            if not self._dialog_submit(action, form):
                continue
            problems = form.check_all()
            if problems:
                form.set_message(problems[0], W.ERR)
                continue
            values = form.values()
            name = values.get("display_name") or default_name
            note = values.get("note") or ""
            try:
                taken = [meta.slug for meta in profile.list_profiles()]
                slug = profile.slugify(name, taken=taken)
                prof = profile.Profile.from_intent(self.intent, slug, display_name=name,
                                                   note=note, layout=self.layout,
                                                   rom_candidate=self.rom)
                prof.payload = {
                    "sha256": getattr(self.payload, "sha256", ""),
                    "size": getattr(self.payload, "size", None),
                    "cache_name": self.payload_path.name if self.payload_path else "",
                }
                path = profile.save(prof)
            except profile.ProfileError as exc:
                form.set_message(f"错误[{getattr(exc, 'code', C.ERR_PROFILE_IO)}] "
                                 f"{getattr(exc, 'reason', str(exc))}", W.ERR)
                continue
            except Exception as exc:
                form.set_message(f"错误[{C.ERR_PROFILE_IO}] 保存失败：{exc}", W.ERR)
                continue
            self._ok(f"配置档案已保存：{name}（{path.name}）。")
            self._record("存档", "成功", f"{name}（{slug}）", slug=slug)
            self._modal(stdscr, "已保存配置档案", [
                f"完成：档案「{name}」已写入用户配置目录（{path.name}）。",
                "档案只记录语义值（W / MHz）；字段偏移每次都会重新推导。",
            ], W.OK)
            return

    # ── 快速注入 ────────────────────────────────────────────────────
    def _inject_module(self):
        try:
            from . import inject as inject_mod
            return inject_mod
        except Exception as exc:
            self._exec_errors.append(str(exc))
            return None

    def _persist_module(self):
        try:
            from . import persist as persist_mod
            return persist_mod
        except Exception as exc:
            self._exec_errors.append(str(exc))
            return None

    def _build_job(self, inject_mod, stdscr):
        bdf = getattr(self.snap, "v100_bdf", None)
        if not bdf:
            self._modal(stdscr, "无法注入", [
                W.fmt_error(C.ERR_PLATFORM, "未检测到 V100，注入需要总线上的 V100 设备。",
                            "接好显卡后按 F5 重新读取。")[0],
            ], W.ERR)
            return None
        gpu = getattr(self.snap, "gpu", None)
        power = float(self.intent.power_w) if self.intent and self.intent.power_w else None
        if power is None:
            power = getattr(gpu, "power_limit_w", None)
        if power is None:
            self._modal(stdscr, "无法注入", [
                W.fmt_error(C.ERR_RANGE, "未确定目标功耗上限。",
                            "请先回到「快速修改」生成覆盖镜像。")[0],
            ], W.ERR)
            return None
        try:
            return inject_mod.job_from_payload(
                self.payload_path, power, bdf,
                sm_max_mhz=(self.intent.clocks.get(C.FIELD_XBAR) if self.intent else None),
                dm_unit=self.env.dm_unit or "",
                lactd_unit=C.LACTD_UNIT,
                note="由 TUI 快速注入提交",
            )
        except Exception as exc:
            self._modal(stdscr, "无法注入", [
                W.fmt_error(C.ERR_INJECT_DISABLED, f"构造注入任务失败：{exc}",
                            "请回到「快速修改」重新生成覆盖镜像。")[0],
            ], W.ERR)
            return None

    def _page_inject(self, stdscr) -> str:
        inject_mod = self._inject_module()
        if inject_mod is None:
            self._modal(stdscr, "注入不可用", [
                W.fmt_error(C.ERR_INJECT_DISABLED, "执行层模块不可用。",
                            "请确认仓库 tui/inject.py 完整后重试。")[0],
            ], W.ERR)
            return "menu"
        reason = self.env.disable_reason("inject")
        if reason:
            self._modal(stdscr, "快速注入已禁用",
                        W.fmt_error(C.ERR_INJECT_DISABLED, reason.split("。")[0] + "。",
                                    "退出本工具后执行 `sudo python3 -m tui`（在仓库根目录）。"),
                        W.ERR)
            return "menu"
        if self.payload_path is None:
            self._modal(stdscr, "尚未生成覆盖镜像", [
                W.fmt_error(C.ERR_RANGE, "没有可注入的覆盖镜像。",
                            "请先在「快速修改」生成覆盖镜像。")[0],
            ], W.WARN)
            return "menu"
        job = self._build_job(inject_mod, stdscr)
        if job is None:
            return "menu"
        checks = []
        prechecked = False
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame, T["pages"]["inject"])
            footer_row = rows - 2
            panel_h = max(8, footer_row - body_top)
            if self.narrow:
                lines = self._inject_lines(job, checks)
                W.Panel("预检与将执行的步骤", lines).render(frame, body_top, 0, panel_h, cols)
                blocking_ok = self._checks_ok(checks)
            else:
                left_w = max(34, cols // 2)
                view = W.ListView([self._check_row(c) for c in checks],
                                  headers=("结论", "检查项", "说明"),
                                  widths=(6, 22, 0),
                                  empty_text="（尚未执行预检）")
                view.render(frame, body_top, 0, panel_h, left_w, "预检（只读）")
                self._render_inject_steps(frame, body_top, left_w + 1, panel_h,
                                          cols - left_w - 1, job)
                blocking_ok = self._checks_ok(checks)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["inject"])
            stdscr.refresh()
            if not prechecked:
                self._busy(stdscr, "正在执行预检（只读）…", T["pages"]["inject"])
                checks = self._run_precheck(inject_mod, job, stdscr)
                if checks is None:
                    return "menu"
                prechecked = True
                continue
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if key in (W.KEY_F5, "r", "R"):
                checks = self._run_precheck(inject_mod, job, stdscr)
                if checks is None:
                    return "menu"
                continue
            if W.is_cancel(key) or key in ("q", "Q"):
                return "menu"
            if W.is_enter(key):
                if not blocking_ok:
                    blocked = [c for c in checks
                               if not getattr(c, "ok", False)
                               and getattr(c, "blocking", True)]
                    first = blocked[0] if blocked else None
                    code = (getattr(first, "code", "") or C.ERR_INJECT_DISABLED
                            if first is not None else C.ERR_INJECT_DISABLED)
                    self._modal(stdscr, "预检未通过，禁止提交", [
                        f"错误[{code}] 预检存在阻断项，本工具不会提交注入任务。",
                        f"  原因：{getattr(first, 'title', '')}"
                        f"{('：' + first.detail) if getattr(first, 'detail', '') else ''}",
                        "  建议：按 F5 重新预检；仍失败请先处理阻断项。",
                    ], W.ERR)
                    continue
                if not self._confirm_inject(stdscr, job):
                    self._note("已取消注入，未产生任何副作用。")
                    continue
                return self._submit_inject(stdscr, inject_mod, job)

    def _run_precheck(self, inject_mod, job, stdscr):
        try:
            return list(inject_mod.precheck(job))
        except Exception as exc:
            self._modal(stdscr, "预检失败", [
                W.fmt_error(C.ERR_INJECT_DISABLED, f"预检执行异常：{exc}",
                            "请改用 `journalctl` 手工核对环境后重试。")[0],
            ], W.ERR)
            return None

    def _check_row(self, check) -> tuple:
        if getattr(check, "ok", False):
            mark = "通过"
        elif getattr(check, "blocking", True):
            mark = "阻止"
        else:
            mark = "提示"
        return (mark, getattr(check, "title", ""), getattr(check, "detail", "") or "")

    def _checks_ok(self, checks) -> bool:
        return all(getattr(c, "ok", False) or not getattr(c, "blocking", True)
                   for c in checks or [])

    def _inject_lines(self, job, checks) -> list:
        lines = ["【预检（只读）】"]
        for check in checks or []:
            mark = "通过" if getattr(check, "ok", False) else (
                "阻止" if getattr(check, "blocking", True) else "提示")
            lines.append(f"· {mark}　{getattr(check, 'title', '')}"
                         f"{('：' + check.detail) if getattr(check, 'detail', '') else ''}")
        lines.append("")
        lines.extend(self._steps_lines(job))
        return lines

    def _steps_lines(self, job) -> list:
        bdf = getattr(job, "bdf", "未知")
        dm = self.env.dm_unit or "（未探测到，注入脚本内会再探测）"
        lines = ["【将执行的完整步骤】"]
        for step in T["inject_steps"]:
            lines.append(step.format(dm=dm, bdf=bdf))
        lines.append("")
        lines.append(f"覆盖镜像：{self.payload_path.name}")
        lines.append(f"payload sha256：{_fmt_sha(getattr(self.payload, 'sha256', ''))}")
        lines.append(f"目标功耗上限：{_fmt_power(getattr(job, 'power_w', None))}"
                     f"　目标核心上报上限：{_fmt_mhz(getattr(job, 'sm_max_mhz', None))}")
        return lines

    def _render_inject_steps(self, frame: W.Frame, y: int, x: int, height: int,
                             width: int, job) -> None:
        if height < 4 or width < 20:
            return
        panel = W.Panel("将执行的完整步骤", self._steps_lines(job))
        panel.render(frame, y, x, height, width)

    def _confirm_inject(self, stdscr, job) -> bool:
        gpu = getattr(self.snap, "gpu", None)
        lines = [
            f"目标设备：{getattr(job, 'bdf', '未知')}　"
            f"固件版本：{getattr(gpu, 'vbios_version', None) or '未知'}",
            f"目标功耗上限：{_fmt_power(getattr(job, 'power_w', None))}"
            f"（现值 {_fmt_power(getattr(gpu, 'power_limit_w', None))}）",
        ]
        if self.intent is not None:
            layout = self.layout
            for key, pair in self.intent.diff_against(layout).items():
                if key == C.FIELD_POWER:
                    continue
                label = layout.labels().get(key, key)
                lines.append(f"{label}：{float(pair[0]):g} MHz → {float(pair[1]):g} MHz")
        lines.extend([
            "「图形会话将中断，本界面会被关闭」——这是摘卡与重新扫描总线的必然结果。",
            f"预计耗时 20–60 秒；失败会自动回滚到原厂状态。",
            f"覆盖镜像 sha256：{_fmt_sha(getattr(self.payload, 'sha256', ''), 32)}",
        ])
        return self._confirm(stdscr, "危险操作确认：快速注入", lines, "注入",
                             prompt="确认后注入会中断图形会话，请键入「注入」并回车")

    def _submit_inject(self, stdscr, inject_mod, job) -> str:
        self._busy(stdscr, "正在提交独立 systemd 单元…", T["pages"]["inject"])
        try:
            handle = inject_mod.submit(job)
        except Exception as exc:
            self._modal(stdscr, "注入提交失败", [
                W.fmt_error(C.ERR_INJECT_DISABLED, f"提交异常：{exc}",
                            "本工具不会在界面进程内直接卸载驱动，请检查 systemd 后重试。")[0],
            ], W.ERR)
            return "menu"
        if not getattr(handle, "ok", False):
            self._error(getattr(handle, "code", C.ERR_INJECT_DISABLED),
                        getattr(handle, "message", "注入提交失败。"),
                        getattr(handle, "advice", ""))
            self._modal(stdscr, "注入提交失败", W.fmt_error(
                getattr(handle, "code", C.ERR_INJECT_DISABLED),
                getattr(handle, "message", "注入提交失败。"),
                getattr(handle, "advice", "")), W.ERR)
            return "menu"
        unit = getattr(handle, "unit", "") or "（单元名未知）"
        log_path = getattr(handle, "log", "") or ""
        self._record("注入", "已提交", f"单元 {unit}，功耗 "
                     f"{_fmt_power(getattr(job, 'power_w', None))}",
                     unit=unit, payload_sha256=getattr(self.payload, "sha256", ""))
        hint = getattr(handle, "journal_hint", "") or ""
        lines = [f"完成：注入任务已提交为独立单元 {unit}。",
                 "图形会话将在数秒后中断，本界面会随之关闭。",
                 "请在 TTY 或 SSH 会话查看进度：",
                 f"`journalctl -u {unit} -f`"]
        if log_path:
            lines.append(f"日志：{_fmt_path(log_path)}")
        if hint:
            lines.insert(0, hint)
        self.handoff = "\n".join(lines)
        self._ok(f"注入任务已提交：{unit}。界面即将退出。")
        return "quit"

    # ── 记录与持久化 ────────────────────────────────────────────────
    def _page_records(self, stdscr) -> str:
        persist_mod = self._persist_module()
        menu = W.Menu(self._records_items(persist_mod), 0)
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame, T["pages"]["records"])
            footer_row = rows - 2
            panel_h = max(8, footer_row - body_top)
            if self.narrow:
                menu.render(frame, body_top, 0, min(panel_h, len(menu.items) + 2), cols)
                lines = self._persist_lines(persist_mod)
                W.Panel("开机持久化现状", lines).render(
                    frame, body_top + len(menu.items) + 2, 0,
                    max(4, panel_h - len(menu.items) - 2), cols)
            else:
                menu_w = max(26, min(34, cols // 3))
                menu_h = min(panel_h, len(menu.items) + 2)
                menu.render(frame, body_top, 0, menu_h, menu_w, T["pages"]["records"])
                lines = self._persist_lines(persist_mod)
                W.Panel("开机持久化现状", lines).render(
                    frame, body_top, menu_w + 1, panel_h, cols - menu_w - 1)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, [("↑↓", "选择"), ("Enter", "进入"),
                                              ("Esc", "返回"), ("l", "消息")])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if key == "l":
                self._text_pane(stdscr, T["pages"]["log"], self.log_lines or ["（暂无消息）"])
                continue
            if W.is_cancel(key) or key in ("q", "Q"):
                return "menu"
            if key in (W.KEY_UP, "k"):
                menu.move(-1)
                continue
            if key in (W.KEY_DOWN, "j"):
                menu.move(1)
                continue
            if W.is_enter(key):
                ok, action = menu.activate()
                if not ok:
                    self._modal(stdscr, "该入口当前不可用", [action], W.WARN)
                    continue
                if action == "back":
                    return "menu"
                if action == "profiles":
                    self._page_profiles(stdscr)
                elif action == "install":
                    self._persist_install(stdscr, persist_mod)
                elif action == "uninstall":
                    self._persist_uninstall(stdscr, persist_mod)
                elif action == "restore":
                    result = self._restore_factory(stdscr)
                    if result == "quit":
                        return "quit"
                menu = W.Menu(self._records_items(persist_mod), menu.index)

    def _records_items(self, persist_mod) -> list:
        labels = T["records_menu"]
        can_persist = self.env.can_persist
        persist_reason = self.env.disable_reason("persist") or ""
        if persist_mod is None:
            persist_reason = "执行层模块不可用（tui/persist.py 缺失）。"
            can_persist = False
        payload_ready = self.payload_path is not None
        items = [
            W.MenuItem(labels["profiles"], "profiles", enabled=True,
                       hint="列表 / 新建 / 改名 / 删除；只记录语义值。"),
            W.MenuItem(labels["install"], "install",
                       enabled=bool(can_persist and payload_ready),
                       reason=(persist_reason if not can_persist
                               else "尚未生成覆盖镜像，请先使用「快速修改」。"),
                       hint="调用 deploy/install.sh，写入 /etc/v100 与 systemd 单元。"),
            W.MenuItem(labels["uninstall"], "uninstall",
                       enabled=bool(can_persist), reason=persist_reason,
                       hint="调用 deploy/uninstall.sh；卸载后内存覆盖仍在。"),
            W.MenuItem(labels["restore"], "restore",
                       enabled=bool(self.env.can_restore),
                       reason=self.env.disable_reason("restore") or "",
                       hint="卸载模块并重新加载，丢弃当前覆盖。"),
            W.MenuItem(labels["back"], "back", enabled=True),
        ]
        return items

    def _persist_lines(self, persist_mod) -> list:
        labels = T["labels"]
        boot = (getattr(self.snap, "boot", None) or {}) if self.snap else {}
        lines = [
            f"单元：{C.BOOT_UNIT}",
            f"启用状态：{_fmt_tri(boot.get('enabled'), '已启用', '未启用')}"
            f"　运行状态：{_fmt_tri(boot.get('active'), '运行中', '未运行')}",
            f"已安装覆盖镜像：{'在位' if boot.get('payload_present') else '不在位'}"
            f"　路径：{C.BOOT_PAYLOAD_PATH}",
        ]
        if persist_mod is not None:
            try:
                state = persist_mod.state()
            except Exception as exc:
                state = None
                lines.append(W.fmt_progress(f"持久化状态读取失败：{exc}"))
            if state is not None:
                lines.append(f"期望功耗：{_fmt_power(getattr(state, 'expected_power_w', None))}"
                             f"（drop-in：{'在位' if getattr(state, 'dropin_present', False) else '不在位'}）")
                lines.append(f"来源档案：{getattr(state, 'source_slug', '') or '未记录'}"
                             f"　参数化支持："
                             f"{'是' if getattr(state, 'param_supported', False) else '否'}")
                sha = getattr(state, "payload_sha256", "")
                if sha:
                    lines.append(f"已安装 payload sha256：{_fmt_sha(sha, 20)}")
                last = getattr(state, "boot_log_last", "")
                if last:
                    lines.append(W.fmt_progress(f"开机日志最后一行：{last}"))
                for note in getattr(state, "notes", ()) or ():
                    lines.append(W.fmt_progress(str(note)))
        lines.append("")
        lines.append("提示：卸载开机持久化不会移除内存中已生效的覆盖；"
                     "要立刻恢复原厂请使用「恢复原厂」或重启。")
        return lines

    def _page_profiles(self, stdscr) -> None:
        view = W.ListView([], headers=("档案", "功耗", "频率", "来源", "更新时间"),
                          widths=(18, 7, 18, 9, 0), empty_text="（暂无配置档案）")
        self._reload_profiles(view)
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame, T["pages"]["profiles"])
            footer_row = rows - 2
            panel_h = max(8, footer_row - body_top)
            view.render(frame, body_top, 0, panel_h, cols, "配置档案（只记录语义值）")
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["profiles"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if W.is_cancel(key) or key in ("q", "Q"):
                return
            if key in (W.KEY_UP, "k"):
                view.move(-1)
                continue
            if key in (W.KEY_DOWN, "j"):
                view.move(1)
                continue
            if key == W.KEY_F5:
                self._reload_profiles(view)
                self._ok("档案列表已刷新。")
                continue
            if key in ("n", "N"):
                self._profile_new(stdscr)
                self._reload_profiles(view)
                continue
            if key in ("e", "E"):
                self._profile_edit(stdscr, view)
                self._reload_profiles(view)
                continue
            if key in ("x", "X"):
                self._profile_delete(stdscr, view)
                self._reload_profiles(view)
                continue
            if W.is_enter(key):
                if self._profile_load(stdscr, view):
                    return

    def _reload_profiles(self, view: W.ListView) -> None:
        try:
            metas = profile.list_profiles()
        except Exception as exc:
            view.set_rows([])
            self._error(C.ERR_PROFILE_IO, f"档案列表读取失败：{exc}")
            return
        rows = []
        for meta in metas:
            name = meta.display_name or meta.slug
            if getattr(meta, "readonly", False):
                name += "（只读）"
            if getattr(meta, "error", ""):
                name += "（损坏）"
            rows.append((name, _fmt_power(meta.power_w),
                         getattr(meta, "clocks_display", "未记录"),
                         getattr(meta, "source", "本机生成"),
                         getattr(meta, "updated_display", "未知")))
        view.set_rows(rows)
        view._metas = metas          # 供选择时取回 slug（界面内部使用）
        if not metas:
            self._warn(C.ERR_PROFILE_CORRUPT, "暂无配置档案；可用「快速修改」生成后保存。")

    def _selected_meta(self, view: W.ListView):
        metas = getattr(view, "_metas", [])
        if not metas:
            return None
        index = min(view.index, len(metas) - 1)
        return metas[index]

    def _profile_load(self, stdscr, view: W.ListView) -> bool:
        meta = self._selected_meta(view)
        if meta is None:
            self._modal(stdscr, "没有可载入的档案", ["请先用「快速修改」生成覆盖镜像并保存档案。"],
                        W.WARN)
            return False
        if getattr(meta, "error", ""):
            self._modal(stdscr, "档案不可用", [
                f"错误[{C.ERR_PROFILE_CORRUPT}] 该档案无法读取：{meta.error}",
                "  建议：删除该档案后重新保存。",
            ], W.ERR)
            return False
        self._busy(stdscr, f"正在载入档案「{meta.display_name or meta.slug}」…")
        try:
            loaded = profile.load(meta.slug)
        except profile.ProfileError as exc:
            self._modal(stdscr, "档案载入失败",
                        W.fmt_error(getattr(exc, "code", C.ERR_PROFILE_IO),
                                    getattr(exc, "reason", str(exc)),
                                    getattr(exc, "advice", "")), W.ERR)
            return False
        if not self._prepare_layout(stdscr):
            return False
        current_sha = getattr(self.rom, "sha256", "")
        recorded = ""
        try:
            recorded = str((loaded.target or {}).get("rom_sha256") or "")
        except Exception:
            recorded = ""
        if recorded and current_sha and recorded != current_sha:
            self._warn(C.WARN_ROM_MISMATCH,
                       "该档案基于另一份固件，字段偏移已重新推导。")
        self.intent = loaded.to_intent()
        self.payload = None
        self.payload_path = None
        self._ok(f"已载入档案「{loaded.display_name or loaded.slug}」，"
                 f"功耗 {_fmt_power(loaded.power_w)}。")
        return True

    def _profile_new(self, stdscr) -> None:
        if self.intent is None:
            self._modal(stdscr, "无法新建档案", [
                W.fmt_error(C.ERR_PROFILE_INVALID, "当前没有可保存的目标值。",
                            "请先在「快速修改」中设置字段。")[0],
            ], W.WARN)
            return
        self._save_profile_dialog(stdscr)

    def _profile_edit(self, stdscr, view: W.ListView) -> None:
        meta = self._selected_meta(view)
        if meta is None:
            return
        try:
            loaded = profile.load(meta.slug)
        except profile.ProfileError as exc:
            self._modal(stdscr, "档案不可用",
                        W.fmt_error(getattr(exc, "code", C.ERR_PROFILE_IO),
                                    getattr(exc, "reason", str(exc))), W.ERR)
            return
        if getattr(loaded, "readonly", False):
            self._modal(stdscr, "档案为只读", [
                f"错误[{C.ERR_PROFILE_VERSION}] 该档案版本高于本工具，只能查看。",
                "  建议：升级本工具后再编辑。",
            ], W.WARN)
            return
        form = W.Form([
            W.TextField("display_name", "档案名称", loaded.display_name or loaded.slug,
                        max_len=C.SLUG_MAX_LEN, allow_empty=False),
            W.TextField("note", "备注", loaded.note or "", max_len=60),
        ], title="改名或备注")
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            stdscr.erase()
            body_top = self._draw_top(frame, "改名或备注")
            form.render(frame, body_top, 0, max(8, rows - 3 - body_top), cols)
            self._draw_message(frame, rows - 2)
            self._draw_keys(frame, rows - 1, [("Enter", "保存"), ("Esc", "取消")])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if W.is_cancel(key) and not form.editing:
                return
            if key in ("q", "Q") and not form.editing:
                return
            action = form.handle_key(key)
            if not self._dialog_submit(action, form):
                continue
            problems = form.check_all()
            if problems:
                form.set_message(problems[0], W.ERR)
                continue
            values = form.values()
            loaded.display_name = values.get("display_name") or loaded.display_name
            loaded.note = values.get("note") or ""
            try:
                profile.save(loaded)
            except profile.ProfileError as exc:
                form.set_message(f"错误[{getattr(exc, 'code', C.ERR_PROFILE_IO)}] "
                                 f"{getattr(exc, 'reason', str(exc))}", W.ERR)
                continue
            self._ok(f"档案「{loaded.display_name}」已更新。")
            self._record("存档", "成功", f"改名/备注：{loaded.display_name}",
                         slug=loaded.slug)
            return

    def _profile_delete(self, stdscr, view: W.ListView) -> None:
        meta = self._selected_meta(view)
        if meta is None:
            return
        name = meta.display_name or meta.slug
        lines = [
            f"即将删除配置档案：「{name}」（{meta.slug}.json）",
            "只会删除本用户的档案文件，不影响显卡与系统状态。",
            "已生成的覆盖镜像缓存不受影响。",
        ]
        if not self._confirm(stdscr, "危险操作确认：删除配置档案", lines, "删除",
                             danger=False):
            self._note("已取消删除。")
            return
        try:
            profile.delete(meta.slug)
        except Exception as exc:
            self._modal(stdscr, "删除失败", [
                f"错误[{C.ERR_PROFILE_IO}] 删除档案失败：{exc}",
                "  建议：检查配置目录权限后重试。",
            ], W.ERR)
            return
        self._ok(f"配置档案已删除：{name}。")
        self._record("存档", "成功", f"删除：{name}", slug=meta.slug)

    # ── 开机持久化 ──────────────────────────────────────────────────
    def _persist_target(self, persist_mod, stdscr):
        if self.payload_path is None or self.intent is None:
            self._modal(stdscr, "无法安装", [
                W.fmt_error(C.ERR_RANGE, "尚未生成覆盖镜像。",
                            "请先在「快速修改」生成覆盖镜像。")[0],
            ], W.WARN)
            return None
        try:
            name = f"{float(self.intent.power_w):g}W-快速档"
            return persist_mod.make_target(self.payload_path, float(self.intent.power_w),
                                          slug="", display_name=name)
        except Exception as exc:
            self._modal(stdscr, "无法安装", [
                W.fmt_error(C.ERR_BUILD, f"构造持久化目标失败：{exc}",
                            "请检查覆盖镜像文件是否可读。")[0],
            ], W.ERR)
            return None

    def _persist_install(self, stdscr, persist_mod) -> None:
        target = self._persist_target(persist_mod, stdscr)
        if target is None:
            return
        try:
            steps, failure = persist_mod.preflight_install(target)
        except Exception as exc:
            self._modal(stdscr, "安装前检查失败", [
                W.fmt_error(C.ERR_BUILD, f"安装前检查异常：{exc}",
                            "请稍后重试。")[0],
            ], W.ERR)
            return
        if failure is not None:
            self._modal(stdscr, "安装前检查未通过", W.fmt_error(
                getattr(failure, "code", C.ERR_BUILD),
                getattr(failure, "message", "安装前检查未通过。"),
                getattr(failure, "advice", "")), W.ERR)
            return
        try:
            plan = persist_mod.install_plan(
                target, observed_max_limit_w=getattr(
                    getattr(self.snap, "gpu", None), "power_max_w", None))
        except Exception as exc:
            self._modal(stdscr, "无法生成安装清单", [
                W.fmt_error(C.ERR_BUILD, f"生成安装清单失败：{exc}", "请稍后重试。")[0],
            ], W.ERR)
            return
        lines = ["将写入以下路径："]
        for path in getattr(plan, "files_to_write", ()) or ():
            lines.append(f"· {path}")
        lines.append(f"期望功耗：{_fmt_power(getattr(plan, 'expected_power_w', None))}")
        lines.append("会先执行 deploy/install.sh 的 MANIFEST.sha256 完整性校验，不绕过。")
        for note in getattr(plan, "notes", ()) or ():
            lines.append(W.fmt_progress(str(note)))
        if not self._confirm(stdscr, "危险操作确认：安装开机持久化", lines, "安装"):
            self._note("已取消安装开机持久化。")
            return
        self._busy(stdscr, "正在安装开机持久化…", T["pages"]["persist_install"])
        try:
            result = persist_mod.install(target)
        except Exception as exc:
            self._modal(stdscr, "安装失败", [
                W.fmt_error(C.ERR_BUILD, f"安装过程异常：{exc}",
                            "请查看 deploy/README.md 的手工安装步骤。")[0],
            ], W.ERR)
            return
        self._show_persist_result(stdscr, result, "安装")
        self.refresh()

    def _persist_uninstall(self, stdscr, persist_mod) -> None:
        lines = [
            f"即将卸载开机持久化：删除 {C.BOOT_UNIT} 及其安装的文件。",
            f"覆盖镜像：{C.BOOT_PAYLOAD_PATH}",
            "卸载后当前内存中的覆盖仍然生效；要立刻恢复原厂请使用「恢复原厂」或重启。",
        ]
        if not self._confirm(stdscr, "危险操作确认：卸载开机持久化", lines, "卸载"):
            self._note("已取消卸载开机持久化。")
            return
        self._busy(stdscr, "正在卸载开机持久化…", T["pages"]["persist_uninstall"])
        try:
            result = persist_mod.uninstall()
        except Exception as exc:
            self._modal(stdscr, "卸载失败", [
                W.fmt_error(C.ERR_BUILD, f"卸载过程异常：{exc}",
                            "请查看 deploy/README.md 的手工卸载步骤。")[0],
            ], W.ERR)
            return
        self._show_persist_result(stdscr, result, "卸载")
        self.refresh()

    def _show_persist_result(self, stdscr, result, action: str) -> None:
        ok = bool(getattr(result, "ok", False))
        lines = []
        for step in getattr(result, "steps", ()) or ():
            mark = "完成" if getattr(step, "ok", False) else "失败"
            detail = getattr(step, "detail", "")
            lines.append(f"· {mark}　{getattr(step, 'title', '')}"
                         f"{('：' + detail) if detail else ''}")
        message = getattr(result, "message", "")
        advice = getattr(result, "advice", "")
        code = getattr(result, "code", "")
        if ok:
            lines.insert(0, W.fmt_ok(message or f"{action}开机持久化成功。"))
        else:
            lines.insert(0, f"错误[{code}] {message or f'{action}开机持久化失败。'}")
            if advice:
                lines.append(f"  建议：{advice}")
        for note in getattr(result, "notes", ()) or ():
            lines.append(W.fmt_progress(str(note)))
        self._modal(stdscr, f"{action}开机持久化：{'成功' if ok else '失败'}", lines,
                    W.OK if ok else W.ERR)
        if ok:
            self._record(f"持久化{action}", "成功", message or "", code=code)
            self._ok(f"{action}开机持久化完成。")
        else:
            self._record(f"持久化{action}", "失败", f"{code} {message}", code=code)
            self._error(code or C.ERR_BUILD, message or f"{action}开机持久化失败。", advice)

    # ── 恢复原厂 ────────────────────────────────────────────────────
    def _restore_factory(self, stdscr) -> str:
        bdf = getattr(self.snap, "v100_bdf", None)
        if not bdf:
            self._modal(stdscr, "无法恢复原厂", [
                W.fmt_error(C.ERR_PLATFORM, "未检测到 V100，无法定位要摘除的设备。",
                            "接好显卡后按 F5 重新读取。")[0],
            ], W.ERR)
            return "menu"
        inject_mod = self._inject_module()
        if inject_mod is None:
            self._modal(stdscr, "恢复原厂不可用", [
                W.fmt_error(C.ERR_INJECT_DISABLED, "执行层模块不可用。",
                            "请确认仓库 tui/inject.py 完整后重试。")[0],
            ], W.ERR)
            return "menu"
        gpu = getattr(self.snap, "gpu", None)
        boot = (getattr(self.snap, "boot", None) or {}) if self.snap else {}
        lines = [
            f"当前覆盖值：功耗上限 {_fmt_power(getattr(gpu, 'power_limit_w', None))}"
            f"　核心上报上限 {_fmt_mhz(getattr(gpu, 'sm_max_mhz', None))}",
            f"原厂值：功耗上限 {_fmt_power(getattr(gpu, 'power_default_w', None))}"
            f"　（恢复即丢弃当前覆盖，不写 flash）",
            "会卸载 nvidia 系列内核模块再重新加载，图形会话将中断。",
        ]
        if boot.get("enabled"):
            lines.append("检测到开机持久化已启用：重启后仍会自动注入，"
                         "如不再需要请先在「记录与持久化」中卸载。")
        if not self._confirm(stdscr, "危险操作确认：恢复原厂", lines, "恢复"):
            self._note("已取消恢复原厂。")
            return "menu"
        self._busy(stdscr, "正在提交恢复原厂任务…", T["pages"]["restore"])
        try:
            handle = inject_mod.restore_factory_order(
                bdf=bdf, factory_power_w=getattr(gpu, "power_default_w", None),
                dm_unit=self.env.dm_unit or "",
                lactd_unit=C.LACTD_UNIT)
        except Exception as exc:
            self._modal(stdscr, "恢复原厂提交失败", [
                W.fmt_error(C.ERR_INJECT_DISABLED, f"提交异常：{exc}",
                            "请检查 systemd 状态后重试。")[0],
            ], W.ERR)
            return "menu"
        if not getattr(handle, "ok", False):
            self._modal(stdscr, "恢复原厂提交失败", W.fmt_error(
                getattr(handle, "code", C.ERR_INJECT_DISABLED),
                getattr(handle, "message", "恢复原厂提交失败。"),
                getattr(handle, "advice", "")), W.ERR)
            return "menu"
        unit = getattr(handle, "unit", "") or "（单元名未知）"
        self._record("恢复原厂", "已提交", f"单元 {unit}", unit=unit)
        self.handoff = "\n".join([
            f"完成：恢复原厂任务已提交为独立单元 {unit}。",
            "图形会话将在数秒后中断，本界面会随之关闭。",
            f"`journalctl -u {unit} -f`",
        ])
        return "quit"

    # ── 查看当前状态 ────────────────────────────────────────────────
    def _page_status(self, stdscr) -> None:
        offset = 0
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame, T["pages"]["status"])
            footer_row = rows - 2
            panel_h = max(8, footer_row - body_top)
            lines = self._status_lines()
            frame.box(body_top, 0, panel_h, cols, T["pages"]["status"])
            flattened = []
            for line in lines:
                flattened.extend(W.wrap(line, max(4, cols - 4)))
            visible = max(1, panel_h - 2)
            offset = max(0, min(offset, max(0, len(flattened) - visible)))
            for row in range(visible):
                idx = offset + row
                if idx >= len(flattened):
                    break
                frame.text(body_top + 1 + row, 2, W.fit(flattened[idx], cols - 4), W.NORMAL)
            if len(flattened) > visible:
                frame.text(body_top, max(2, cols - 14),
                           W.fit(f"{offset + 1}-{min(len(flattened), offset + visible)}"
                                 f"/{len(flattened)}", 12), W.DIM)
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["status"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if key in (W.KEY_F5, "r", "R"):
                self.refresh()
                offset = 0
                self._ok("已重新读取当前状态。")
                continue
            if key == "?":
                self._help(stdscr)
                continue
            if key in (W.KEY_DOWN, "j"):
                offset += 1
                continue
            if key in (W.KEY_UP, "k"):
                offset = max(0, offset - 1)
                continue
            if key == W.KEY_PGDN:
                offset += max(1, panel_h - 2)
                continue
            if key == W.KEY_PGUP:
                offset = max(0, offset - max(1, panel_h - 2))
                continue
            if W.is_cancel(key) or key in ("q", "Q"):
                return

    def _status_lines(self) -> list:
        snap = self.snap
        gpu = getattr(snap, "gpu", None) if snap else None
        labels = T["labels"]
        lines = ["【显卡】"]
        if gpu is None:
            lines.append(W.fmt_progress("未能读取显卡状态。"))
        else:
            lines.extend([
                f"· 设备地址：{gpu.bdf or '未知'}　型号：{gpu.name or '未知'}",
                f"· VBIOS 版本：{gpu.vbios_version or '未知'}",
                f"· 功耗上限：{_fmt_power(gpu.power_limit_w)}"
                f"　硬件上限：{_fmt_power(gpu.power_max_w)}"
                f"　出厂默认：{_fmt_power(gpu.power_default_w)}",
                f"· 核心上报上限：{_fmt_mhz(gpu.sm_max_mhz)}"
                f"　Applications 时钟：{_fmt_mhz(gpu.apps_graphics_mhz)}",
                f"· 当前核心频率：{_fmt_mhz(gpu.sm_current_mhz)}"
                f"　温度：{'未知' if gpu.temp_c is None else str(gpu.temp_c) + ' °C'}",
            ])
            mem = _mem_clock(gpu)
            lines.append(f"· 显存频率：{_fmt_mhz(mem)}"
                         + ("" if mem is not None
                            else "　" + W.fmt_progress(T["status_notes"]["mem_missing"])))
            for note in getattr(gpu, "notes", []) or []:
                lines.append(W.fmt_progress(str(note)))
        lines.append("")
        lines.append("【覆盖与开机持久化】")
        lines.append(f"· 覆盖：{self._override_text()}")
        boot = (getattr(snap, "boot", None) or {}) if snap else {}
        lines.append(f"· 开机持久化：{self._persist_short()}"
                     f"　单元：{C.BOOT_UNIT}"
                     f"　已安装镜像：{'在位' if boot.get('payload_present') else '不在位'}")
        size = boot.get("payload_size")
        lines.append(f"· 已安装镜像体积：{_fmt_count(size) + ' 字节' if size else '未知'}")
        lines.append("")
        lines.append("【环境与权限】")
        lines.append(f"· 平台：{getattr(self.env, 'platform', '未知')}"
                     f"　权限：{'root' if self.env.is_root else '普通用户（只读模式）'}"
                     f"　调用者：{getattr(self.env, 'user', '') or '未知'}")
        lines.append(f"· 驱动 registry 接口：{'可用' if self.env.registry_ok else '不可用'}"
                     f"　systemd-run：{'可用' if self.env.systemd_run_ok else '不可用'}")
        lines.append(f"· 显示管理器：{self.env.dm_unit or '未探测到'}"
                     f"（{'运行中' if self.env.dm_active else '未运行'}）"
                     f"　lactd：{_fmt_tri(self.env.lactd_active, '运行中', '未运行')}")
        for text in self.env.reasons or []:
            lines.append(W.fmt_progress(str(text)))
        for text in self._exec_errors:
            lines.append(W.fmt_progress(str(text)))
        lines.append("")
        lines.append("【基准 ROM 与字段定位】")
        if self.layout is None:
            lines.append(W.fmt_progress(T["status_notes"]["no_rom"]))
        else:
            lines.append(f"· 来源：{self._rom_source_text()}"
                         f"　sha256：{_fmt_sha(getattr(self.rom, 'sha256', ''), 20)}")
            lines.append(f"· 长度：{_fmt_count(self.layout.rom_length)} 字节"
                         f"　数据镜像：[{self.layout.data_image_start:#x}"
                         f"..{self.layout.data_image_end:#x}]")
            lines.extend(f"· {text}" for text in self.layout.describe())
        lines.append("")
        lines.append("【上一次注入任务】")
        lines.extend(self._last_job_lines())
        return lines

    def _last_job_lines(self) -> list:
        inject_mod = self._inject_module()
        if inject_mod is None:
            return [W.fmt_progress("执行层不可用，无法读取上次任务。")]
        try:
            state = inject_mod.read_job_state()
        except Exception as exc:
            return [W.fmt_progress(f"读取上次任务失败：{exc}")]
        if state is None or not getattr(state, "unit", ""):
            return [W.fmt_progress("没有正在运行或最近完成的注入任务。")]
        lines = [f"· 单元：{state.unit}　阶段：{getattr(state, 'phase', '未知')}"
                 f"　结果：{getattr(state, 'result', '') or '未结束'}"]
        if getattr(state, "detail", ""):
            lines.append(W.fmt_progress(state.detail))
        if getattr(state, "expected_power_w", None) is not None:
            lines.append(f"· 目标功耗：{_fmt_power(state.expected_power_w)}"
                         f"　目标核心上报上限：{_fmt_mhz(getattr(state, 'sm_max_mhz', None))}")
        if getattr(state, "log_path", ""):
            lines.append(f"· 日志：{_fmt_path(state.log_path)}")
        return lines

    # ── 查看历史 ────────────────────────────────────────────────────
    def _page_history(self, stdscr) -> None:
        view = W.ListView([], headers=("时间", "动作", "结果", "摘要"),
                          widths=(19, 12, 8, 0), empty_text="（暂无历史记录）")
        events = self._reload_history(view)
        while True:
            frame = W.Frame(stdscr, self.theme)
            rows, cols = frame.size
            self.narrow = cols < MIN_COLS or rows < MIN_ROWS
            stdscr.erase()
            body_top = self._draw_top(frame, T["pages"]["history"])
            footer_row = rows - 2
            panel_h = max(8, footer_row - body_top)
            view.render(frame, body_top, 0, panel_h, cols, "操作历史（时间倒序，只读）")
            self._draw_message(frame, footer_row)
            self._draw_keys(frame, rows - 1, T["keys"]["history"])
            stdscr.refresh()
            key = W.read_key(stdscr)
            if not key or key == W.KEY_RESIZE:
                continue
            if W.is_cancel(key) or key in ("q", "Q"):
                return
            if key in (W.KEY_UP, "k"):
                view.move(-1)
                continue
            if key in (W.KEY_DOWN, "j"):
                view.move(1)
                continue
            if key == W.KEY_F5:
                events = self._reload_history(view)
                self._ok("历史记录已刷新。")
                continue
            if key in ("d", "D"):
                try:
                    path = history.export_text()
                except Exception as exc:
                    self._modal(stdscr, "导出失败", [
                        f"错误[{C.ERR_HISTORY_IO}] 导出历史失败：{exc}",
                        "  建议：检查缓存目录权限后重试。",
                    ], W.ERR)
                    continue
                self._ok(f"历史已导出：{_fmt_path(path)}")
                continue
            if W.is_enter(key):
                index = min(view.index, max(0, len(events) - 1))
                if not events:
                    self._modal(stdscr, "没有历史记录", ["暂无可查看的记录。"], W.WARN)
                    continue
                event = events[index]
                lines = [f"{key_}：{value}" for key_, value in (event or {}).items()]
                self._text_pane(stdscr, "历史记录详情", lines or ["（空记录）"])

    def _reload_history(self, view: W.ListView) -> list:
        try:
            events = list(history.tail(200))
        except Exception as exc:
            self._error(C.ERR_HISTORY_IO, f"历史记录读取失败：{exc}")
            view.set_rows([])
            return []
        rows = []
        for event in events:
            rows.append((str(event.get("time") or event.get("ts") or "未知"),
                         str(event.get("action") or ""),
                         str(event.get("result") or ""),
                         str(event.get("detail") or "")))
        view.set_rows(rows)
        if not rows:
            self._warn(C.ERR_HISTORY_IO, "暂无历史记录。")
        return events

    # ── 历史写入（失败不得影响主流程）───────────────────────────────
    def _record(self, action: str, result: str, detail: str = "", **fields) -> None:
        recorder = self._fn("record", history.record)
        try:
            recorder(action, result, detail, **fields)
        except Exception as exc:                    # 记录失败只提示，不阻断
            self.log_lines.append(W.fmt_progress(f"历史记录写入失败：{exc}"))


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────
def run(env=None) -> int:
    """启动界面。``env`` 缺省时自行探测（仅 Linux）。"""
    return App(env=env).run()


def main(argv=None) -> int:
    """``python3 -m tui`` 的薄包装（完整参数解析由 t7 的 ``__main__`` 负责）。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--version" in argv:
        print(f"{APP_TITLE} v{APP_VERSION}")
        return 0
    if "--help" in argv or "-h" in argv:
        print(f"{APP_TITLE} v{APP_VERSION}：简体中文 TUI（仅 Linux）。")
        print("用法：python3 -m tui [--rom 路径] [--readonly] [--version] [--help]")
        print("危险操作（注入 / 开机持久化 / 恢复原厂）需要 root，且必须键入中文确认词。")
        return 0
    readonly = "--readonly" in argv
    env = env_probe.probe(readonly=readonly)
    return App(env=env).run()


if __name__ == "__main__":            # pragma: no cover - 便于本地手工启动
    raise SystemExit(main())
