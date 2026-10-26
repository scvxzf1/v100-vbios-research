"""通用界面控件（tui/DESIGN.md §4 中 t5 的 ``tui/ui/widgets.py``）。

本模块只做两件事：**按显示宽度画字符** 与 **把按键归一化成字符串**。
它不导入数据层、不执行任何系统命令、不读写任何文件，任何页面都可以安全复用。

三条硬性约定（对应 DESIGN §7.3 与 §8）：

1. 一切宽度用 :func:`dwidth` 计算（CJK 记 2 列），不依赖 ``len()``，
   否则中文界面会光标错位、边框错行。
2. 一切写入都经过 :func:`safe_addstr` 裁剪，越界只会少画几个字符，
   绝不抛 ``curses.error`` 把整个界面打挂（右下角写最后一格是最常见触发点）。
3. 颜色可用时用 8 色对，不可用时（`has_colors()` 为假或单色终端）
   用「前缀符号 + 亮度/反显」表达语义，保证信息不丢。

对外接口：

* 宽度与文本：``dwidth``、``char_width``、``truncate``、``wrap``、``pad``、``center``
* 文案格式：``fmt_ok``、``fmt_warn``、``fmt_error``、``fmt_progress``、``STATUS_SYMBOL``
* 主题：``Theme``
* 绘图：``Frame``、``hline``、``box``
* 按键：``read_key`` 及 ``KEY_*`` 名字常量、``key_legend``
* 控件：``MenuItem``/``Menu``、``ListView``、``Form``（``NumberField``/``TextField``）、
  ``ConfirmDialog``、``MessageBox``、``TextPane``（日志/帮助/长文本滚动）、``StatusBar``、
  ``ProgressLine``、``Panel``
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

try:                                    # pragma: no cover - 正常 Linux 一定有 curses
    import curses
    CURSES_AVAILABLE = True
except Exception:                       # 极简 Python（缺少 ncurses 构建）
    CURSES_AVAILABLE = False

    class _StubCurses:
        """无 curses 环境的占位：只提供本模块用到的名字，保证导入不炸。

        ``tui/ui.py`` 会先检查 :data:`CURSES_AVAILABLE`，在没有 curses 时
        按 DESIGN §6.1 打印中文提示并以退出码 2 结束（不进界面）。
        """

        A_NORMAL = 0
        A_BOLD = 1 << 1
        A_DIM = 1 << 2
        A_REVERSE = 1 << 4
        A_UNDERLINE = 1 << 5
        COLOR_BLACK = 0
        COLOR_RED = 1
        COLOR_GREEN = 2
        COLOR_YELLOW = 3
        COLOR_BLUE = 4
        COLOR_CYAN = 6
        COLOR_WHITE = 7
        KEY_UP = 259
        KEY_DOWN = 258
        KEY_LEFT = 260
        KEY_RIGHT = 261
        KEY_ENTER = 343
        KEY_BACKSPACE = 263
        KEY_DC = 330
        KEY_HOME = 262
        KEY_END = 360
        KEY_PPAGE = 339
        KEY_NPAGE = 338
        KEY_RESIZE = 410

        @staticmethod
        def has_colors():
            return False

        @staticmethod
        def start_color():
            return None

        @staticmethod
        def use_default_colors():
            return None

        @staticmethod
        def init_pair(*args, **kwargs):
            return None

        @staticmethod
        def color_pair(value):
            return 0

    curses = _StubCurses()

__all__ = [
    "CURSES_AVAILABLE",
    "KIND_NAMES",
    "char_width",
    "dwidth",
    "truncate",
    "wrap",
    "pad",
    "center",
    "strip_width",
    "fit",
    "fmt_ok",
    "fmt_warn",
    "fmt_error",
    "fmt_progress",
    "STATUS_SYMBOL",
    "Theme",
    "Frame",
    "safe_addstr",
    "window_size",
    "KEY_UP",
    "KEY_DOWN",
    "KEY_LEFT",
    "KEY_RIGHT",
    "KEY_SLEFT",
    "KEY_SRIGHT",
    "KEY_ENTER",
    "KEY_ESC",
    "KEY_TAB",
    "KEY_BACKSPACE",
    "KEY_DELETE",
    "KEY_HOME",
    "KEY_END",
    "KEY_PGUP",
    "KEY_PGDN",
    "KEY_F5",
    "KEY_F10",
    "KEY_RESIZE",
    "read_key",
    "key_legend",
    "is_enter",
    "is_cancel",
    "MenuItem",
    "Menu",
    "ListView",
    "Panel",
    "NumberField",
    "TextField",
    "Form",
    "ConfirmDialog",
    "MessageBox",
    "TextPane",
    "StatusBar",
    "ProgressLine",
]


# ─────────────────────────────────────────────────────────────────────────────
# 语义样式名（Theme 与调用方共用；对应 DESIGN §7.2 的完成/提示/错误/进度行）
# ─────────────────────────────────────────────────────────────────────────────
NORMAL = "normal"
TITLE = "title"
OK = "ok"
WARN = "warn"
ERR = "err"
DIM = "dim"
SELECT = "select"
HEADER = "header"
FRAME_LINE = "frame"

KIND_NAMES = (NORMAL, TITLE, OK, WARN, ERR, DIM, SELECT, HEADER, FRAME_LINE)

# 单色终端下用来替代颜色的前缀符号（DESIGN §8「前缀符号 + 亮度」）
STATUS_SYMBOL = {
    OK: "✔",
    WARN: "!",
    ERR: "×",
    DIM: "·",
}


# ─────────────────────────────────────────────────────────────────────────────
# 显示宽度：中文按 2 列算，组合字符按 0 列算
# ─────────────────────────────────────────────────────────────────────────────
def char_width(ch: str) -> int:
    """单个字符的显示宽度。控制字符记 0，东亚宽字符/全角记 2。"""
    if not ch:
        return 0
    if ch == "\t":
        return 4
    code = ord(ch)
    if code < 32 or code == 0x7F:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def dwidth(text) -> int:
    """字符串的显示宽度（不是字符数）。"""
    return sum(char_width(ch) for ch in str(text or ""))


def strip_width(text, width: int) -> str:
    """按显示宽度从左侧截取（供 ``fit`` 内部使用，超宽不补空格）。"""
    if width <= 0:
        return ""
    out = []
    used = 0
    for ch in str(text or ""):
        cw = char_width(ch)
        if used + cw > width:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def truncate(text, width: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超出时补省略号。**只**允许状态行等单行场景使用。"""
    text = str(text or "")
    if width <= 0:
        return ""
    if dwidth(text) <= width:
        return text
    ew = dwidth(ellipsis)
    if ew >= width:
        return strip_width(ellipsis, width)
    return strip_width(text, width - ew) + ellipsis


def _tokens(text: str):
    """切成可换行的最小单位：连续的半角非空白词整体，其余逐字符。"""
    buf = []
    for ch in text:
        if ch.isascii() and not ch.isspace():
            buf.append(ch)
            continue
        if buf:
            yield "".join(buf)
            buf = []
        yield ch
    if buf:
        yield "".join(buf)


def wrap(text, width: int) -> list:
    """按显示宽度换行（CJK 任意处可断，半角词尽量整体保留）。

    ``width`` 为可用的**显示列数**。返回至少一行（空串也返回 ``[""]``），
    保证界面可以「超宽换行而不是截断」（DESIGN §7.3）。
    """
    text = "" if text is None else str(text)
    if width <= 0:
        return [""]
    out = []
    for para in text.split("\n"):
        if para == "":
            out.append("")
            continue
        line, used = "", 0
        for token in _tokens(para):
            tw = dwidth(token)
            if used + tw <= width:
                line += token
                used += tw
                continue
            if line.strip():
                out.append(line.rstrip())
            line, used = "", 0
            if tw > width:                      # 单个超长词：强制逐字符断
                for ch in token:
                    cw = char_width(ch)
                    if used + cw > width:
                        out.append(line)
                        line, used = "", 0
                    line += ch
                    used += cw
            else:
                line, used = token, tw
        out.append(line.rstrip())
    return out or [""]


def pad(text, width: int) -> str:
    """右侧补空格到指定显示宽度（已超宽则原样返回，交给调用方截断）。"""
    text = str(text or "")
    gap = width - dwidth(text)
    return text + " " * gap if gap > 0 else text


def center(text, width: int) -> str:
    """按显示宽度居中（左侧补空格，右侧由调用方铺满）。"""
    text = str(text or "")
    gap = width - dwidth(text)
    if gap <= 0:
        return text
    return " " * (gap // 2) + text


def fit(text, width: int) -> str:
    """**单行**场景的「补齐 + 截断」：先截断到宽度，再补空格铺满。"""
    return pad(truncate(text, width), width)


# ─────────────────────────────────────────────────────────────────────────────
# 文案格式（DESIGN §7.2：错误/提示/完成/进度 四种固定形状）
# ─────────────────────────────────────────────────────────────────────────────
def fmt_ok(text) -> str:
    """完成行。"""
    return f"完成：{text}"


def fmt_warn(code, text) -> str:
    """提示行（黄色，不阻断操作）。"""
    return f"提示[{code}] {text}" if code else f"提示：{text}"


def fmt_progress(text) -> str:
    """进度行（灰色）。"""
    return f"· {text}"


def fmt_error(code, cause, advice: str = "") -> list:
    """错误块：结论 / 原因 / 建议 三段，返回可直接逐行绘制的字符串列表。"""
    lines = [f"错误[{code}] {cause}"]
    if advice:
        lines.append(f"  建议：{advice}")
    return lines


# ─────────────────────────────────────────────────────────────────────────────
# 主题与底层绘图
# ─────────────────────────────────────────────────────────────────────────────
class Theme:
    """颜色对封装。``has_colors()`` 为假时自动降级为亮度/反显。"""

    #: 语义名 → 8 色对编号（1..7，避开 0 号默认对）
    PAIRS = {
        TITLE: 1,     # 青底黑字
        OK: 2,        # 绿
        WARN: 3,      # 黄
        ERR: 4,       # 红
        SELECT: 5,    # 反显
        HEADER: 6,    # 蓝
        DIM: 7,       # 白（低亮度）
    }

    def __init__(self, screen=None, colors=None):
        self.colors = False
        if colors is not None:
            self.colors = bool(colors)
        elif screen is not None:
            try:
                self.colors = bool(curses.has_colors())
            except Exception:
                self.colors = False
        if self.colors:
            try:
                curses.start_color()
                try:
                    curses.use_default_colors()
                except Exception:
                    pass
                curses.init_pair(self.PAIRS[TITLE], curses.COLOR_BLACK, curses.COLOR_CYAN)
                curses.init_pair(self.PAIRS[OK], curses.COLOR_GREEN, -1)
                curses.init_pair(self.PAIRS[WARN], curses.COLOR_YELLOW, -1)
                curses.init_pair(self.PAIRS[ERR], curses.COLOR_RED, -1)
                curses.init_pair(self.PAIRS[SELECT], curses.COLOR_BLACK, curses.COLOR_CYAN)
                curses.init_pair(self.PAIRS[HEADER], curses.COLOR_CYAN, -1)
                curses.init_pair(self.PAIRS[DIM], curses.COLOR_WHITE, -1)
            except Exception:
                self.colors = False

    def attr(self, kind: str = NORMAL) -> int:
        if self.colors and kind in self.PAIRS:
            try:
                base = curses.color_pair(self.PAIRS[kind])
            except Exception:
                base = 0
            if kind == SELECT:
                base |= curses.A_BOLD
            if kind == TITLE:
                base |= curses.A_BOLD
            return base
        # 单色降级：用亮度/反显表达同样的语义差别
        if kind == TITLE:
            return curses.A_REVERSE | curses.A_BOLD
        if kind == OK:
            return curses.A_BOLD
        if kind == WARN:
            return curses.A_BOLD
        if kind == ERR:
            return curses.A_REVERSE | curses.A_BOLD
        if kind == SELECT:
            return curses.A_REVERSE | curses.A_BOLD
        if kind == DIM:
            return curses.A_DIM
        if kind == HEADER:
            return curses.A_BOLD
        return curses.A_NORMAL


def window_size(win) -> tuple:
    """返回 ``(rows, cols)``；读不到时给一个安全的 80×24 兜底。"""
    try:
        rows, cols = win.getmaxyx()
        if rows > 0 and cols > 0:
            return rows, cols
    except Exception:
        pass
    return 24, 80


def safe_addstr(win, y: int, x: int, text, attr: int = 0) -> None:
    """把文本画到 ``(y, x)``，自动裁剪到窗口内，任何越界都不抛异常。"""
    text = "" if text is None else str(text)
    if not text:
        return
    rows, cols = window_size(win)
    if y < 0 or y >= rows or x < 0 or x >= cols:
        return
    text = truncate(text.replace("\t", "    "), cols - x)
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
        return
    except Exception:
        pass
    # 兜底：右下角最后一格写入会抛错，退一格重试
    try:
        if dwidth(text) > 1 and cols - x > 1:
            win.addstr(y, x, strip_width(text, cols - x - 1), attr)
    except Exception:
        pass


def hline(win, y: int, x: int, width: int, ch: str = "─", attr: int = 0) -> None:
    """画一条水平线（宽度按显示列计）。"""
    if width <= 0:
        return
    rows, cols = window_size(win)
    if y < 0 or y >= rows or x >= cols:
        return
    width = min(width, cols - x)
    safe_addstr(win, y, x, ch * width, attr)


def box(win, y: int, x: int, height: int, width: int, title: str = "",
        attr: int = 0, title_attr: int = 0) -> None:
    """画单线边框；``title`` 会嵌到上边框里（超宽自动截断）。"""
    rows, cols = window_size(win)
    height = max(2, min(height, rows - y))
    width = max(2, min(width, cols - x))
    if height < 2 or width < 2:
        return
    safe_addstr(win, y, x, "┌" + "─" * (width - 2) + "┐", attr)
    for row in range(y + 1, y + height - 1):
        safe_addstr(win, row, x, "│", attr)
        safe_addstr(win, row, x + width - 1, "│", attr)
    safe_addstr(win, y + height - 1, x, "└" + "─" * (width - 2) + "┘", attr)
    if title:
        inner = width - 4
        if inner > 0:
            safe_addstr(win, y, x + 2, " " + truncate(title, max(0, inner - 1)) + " ",
                        title_attr or attr)


class Frame:
    """把 ``(win, theme)`` 绑在一起的绘图助手，页面里只和它打交道。"""

    def __init__(self, win, theme: Theme):
        self.win = win
        self.theme = theme or Theme()

    # ── 尺寸 ────────────────────────────────────────────────────────
    @property
    def size(self) -> tuple:
        return window_size(self.win)

    @property
    def rows(self) -> int:
        return window_size(self.win)[0]

    @property
    def cols(self) -> int:
        return window_size(self.win)[1]

    def narrow(self, min_cols: int = 80, min_rows: int = 20) -> bool:
        """是否需要切单列/精简布局（DESIGN §2.3）。"""
        return self.cols < min_cols or self.rows < min_rows

    # ── 绘图 ────────────────────────────────────────────────────────
    def attr(self, kind: str = NORMAL) -> int:
        return self.theme.attr(kind)

    def text(self, y: int, x: int, text, kind: str = NORMAL) -> None:
        safe_addstr(self.win, y, x, text, self.attr(kind))

    def row(self, y: int, text="", kind: str = NORMAL, x: int = 0) -> None:
        """画一整行并铺满剩余宽度（清场用，避免上一帧残留）。"""
        width = self.cols - x
        if width <= 0:
            return
        safe_addstr(self.win, y, x, fit(text, width), self.attr(kind))

    def blank(self, y: int, x: int = 0, width: int = None) -> None:
        width = self.cols - x if width is None else width
        if width > 0:
            safe_addstr(self.win, y, x, " " * width)

    def box(self, y: int, x: int, height: int, width: int, title: str = "",
            kind: str = FRAME_LINE, title_kind: str = TITLE) -> None:
        box(self.win, y, x, height, width, title,
            self.attr(kind), self.attr(title_kind))

    def hline(self, y: int, x: int, width: int, ch: str = "─", kind: str = FRAME_LINE) -> None:
        hline(self.win, y, x, width, ch, self.attr(kind))

    def lines(self, y: int, x: int, width: int, texts, kind: str = NORMAL,
              max_rows: int = 0) -> int:
        """画多行（自动换行），返回实际占用的行数。"""
        used = 0
        for item in texts or []:
            for piece in wrap(item, max(1, width)):
                if max_rows and used >= max_rows:
                    return used
                if y + used >= self.rows:
                    return used
                self.text(y + used, x, piece, kind)
                used += 1
        return used


# ─────────────────────────────────────────────────────────────────────────────
# 按键归一化：把 curses 的 int / 宽字符统一成字符串
# ─────────────────────────────────────────────────────────────────────────────
KEY_UP = "UP"
KEY_DOWN = "DOWN"
KEY_LEFT = "LEFT"
KEY_RIGHT = "RIGHT"
KEY_SLEFT = "SLEFT"
KEY_SRIGHT = "SRIGHT"
KEY_ENTER = "ENTER"
KEY_ESC = "ESC"
KEY_TAB = "TAB"
KEY_BACKSPACE = "BACKSPACE"
KEY_DELETE = "DELETE"
KEY_HOME = "HOME"
KEY_END = "END"
KEY_PGUP = "PGUP"
KEY_PGDN = "PGDN"
KEY_F5 = "F5"
KEY_F10 = "F10"
KEY_RESIZE = "RESIZE"
KEY_NONE = ""


def _int_key_map() -> dict:
    mapping = {
        curses.KEY_UP: KEY_UP,
        curses.KEY_DOWN: KEY_DOWN,
        curses.KEY_LEFT: KEY_LEFT,
        curses.KEY_RIGHT: KEY_RIGHT,
        curses.KEY_ENTER: KEY_ENTER,
        curses.KEY_BACKSPACE: KEY_BACKSPACE,
        curses.KEY_DC: KEY_DELETE,
        curses.KEY_HOME: KEY_HOME,
        curses.KEY_END: KEY_END,
        curses.KEY_PPAGE: KEY_PGUP,
        curses.KEY_NPAGE: KEY_PGDN,
        curses.KEY_RESIZE: KEY_RESIZE,
    }
    # Shift+←→ 与功能键在不同平台缺项，缺了就跳过（界面只少一个快捷方式）
    for attr_name, name in (
        ("KEY_SLEFT", KEY_SLEFT),
        ("KEY_SRIGHT", KEY_SRIGHT),
        ("KEY_F5", KEY_F5),
        ("KEY_F10", KEY_F10),
        ("KEY_F1", "F1"),
        ("KEY_F2", "F2"),
        ("KEY_F3", "F3"),
        ("KEY_F4", "F4"),
        ("KEY_F6", "F6"),
        ("KEY_F7", "F7"),
        ("KEY_F8", "F8"),
        ("KEY_F9", "F9"),
    ):
        code = getattr(curses, attr_name, None)
        if code is not None:
            mapping.setdefault(code, name)
    return mapping


def read_key(win):
    """阻塞读一个键，返回归一化字符串。

    * 宽字符（中文确认词）原样返回单个字符；
    * ``Ctrl-C`` 返回 ``"C-c"``，调用方按「取消」处理，不让界面被打断；
    * 读超时/读取失败返回 :data:`KEY_NONE`。
    """
    mapping = _int_key_map()
    try:
        ch = win.get_wch()
    except KeyboardInterrupt:
        return "C-c"
    except Exception:
        return KEY_NONE
    if isinstance(ch, int):
        if ch == 3:
            return "C-c"
        return mapping.get(ch, f"KEY_{ch}")
    if ch in ("\n", "\r"):
        return KEY_ENTER
    if ch == "\x1b":
        return KEY_ESC
    if ch == "\t":
        return KEY_TAB
    if ch in ("\x7f", "\x08"):
        return KEY_BACKSPACE
    if ch == "\x03":
        return "C-c"
    return ch


def is_enter(key) -> bool:
    return key in (KEY_ENTER, "\n", "\r")


def is_cancel(key) -> bool:
    return key in (KEY_ESC, "C-c")


def key_legend(pairs) -> str:
    """把 ``[(键名, 说明)]`` 拼成底部按键提示行。"""
    return "  ".join(f"{key} {desc}" for key, desc in pairs if key)


# ─────────────────────────────────────────────────────────────────────────────
# 主菜单
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class MenuItem:
    """一个菜单项。``enabled=False`` 时仍然可选中——只有选中才能看到禁用原因。"""

    label: str
    action: str
    enabled: bool = True
    reason: str = ""
    hint: str = ""


class Menu:
    """纵向菜单：上下移动、Enter 触发，禁用项不允许触发但可以选中查看原因。"""

    def __init__(self, items, index: int = 0):
        self.items = list(items)
        self.index = max(0, min(index, len(self.items) - 1)) if self.items else 0

    # ── 状态 ────────────────────────────────────────────────────────
    @property
    def current(self):
        return self.items[self.index] if self.items else None

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)

    def enable(self, action: str, enabled: bool, reason: str = "") -> None:
        """刷新某一项的可用性（状态变化后由页面调用）。"""
        for item in self.items:
            if item.action == action:
                item.enabled = bool(enabled)
                item.reason = "" if enabled else reason

    def activate(self) -> tuple:
        """返回 ``(是否可用, action 或禁用原因)``。"""
        item = self.current
        if item is None:
            return False, "菜单为空。"
        if not item.enabled:
            return False, item.reason or "该入口当前不可用。"
        return True, item.action

    # ── 绘图 ────────────────────────────────────────────────────────
    def render(self, frame: Frame, y: int, x: int, height: int, width: int,
               title: str = "功能") -> None:
        frame.box(y, x, height, width, title)
        inner_w = width - 4
        for offset, item in enumerate(self.items):
            row = y + 1 + offset
            if row >= y + height - 1 or offset >= height - 2:
                break
            cursor = "▸" if offset == self.index else " "
            mark = " " if item.enabled else "×"
            kind = SELECT if offset == self.index else (NORMAL if item.enabled else DIM)
            text = f"{cursor}{mark} {item.label}"
            frame.text(row, x + 2, fit(text, inner_w), kind)


# ─────────────────────────────────────────────────────────────────────────────
# 列表（档案、历史、预检结果）
# ─────────────────────────────────────────────────────────────────────────────
class ListView:
    """带表头、可滚动的只读列表；行由调用方给出「单元格字符串列表」。"""

    def __init__(self, rows=None, headers=(), widths=(), empty_text: str = "（空）"):
        self.rows = list(rows or [])
        self.headers = list(headers or [])
        self.widths = list(widths or [])
        self.empty_text = empty_text
        self.index = 0
        self.scroll = 0

    def set_rows(self, rows) -> None:
        self.rows = list(rows or [])
        self.index = min(self.index, max(0, len(self.rows) - 1))
        self.scroll = 0

    @property
    def current(self):
        return self.rows[self.index] if self.rows else None

    def move(self, delta: int, page: int = 0) -> None:
        if not self.rows:
            return
        if page:
            delta = page
        self.index = max(0, min(self.index + delta, len(self.rows) - 1))

    def _cells(self, row) -> list:
        if isinstance(row, (list, tuple)):
            return [str(c) for c in row]
        return [str(row)]

    def _row_text(self, row, width: int) -> str:
        cells = self._cells(row)
        parts = []
        for offset, cell in enumerate(cells):
            limit = self.widths[offset] if offset < len(self.widths) else 0
            if limit > 0:
                parts.append(fit(cell, limit))
            else:
                parts.append(cell)
        text = " ".join(parts)
        return fit(text, width)

    def render(self, frame: Frame, y: int, x: int, height: int, width: int,
               title: str = "", selected_hint: str = "") -> None:
        if height < 3:
            return
        frame.box(y, x, height, width, title)
        inner_x = x + 2
        inner_w = width - 4
        head_row = y + 1
        body_top = head_row + 1
        body_h = height - 3
        if self.headers:
            head = self._row_text(self.headers, inner_w)
            frame.text(head_row, inner_x, head, HEADER)
            frame.hline(head_row + 1, inner_x, max(0, inner_w), "─")
            body_top = head_row + 2
            body_h = max(1, height - 4)
        else:
            body_top = head_row
            body_h = height - 2
        if not self.rows:
            frame.text(body_top, inner_x, fit(self.empty_text, inner_w), DIM)
            return
        # 让选中行始终可见
        if self.index < self.scroll:
            self.scroll = self.index
        elif self.index >= self.scroll + max(1, body_h):
            self.scroll = self.index - max(1, body_h) + 1
        self.scroll = max(0, min(self.scroll, max(0, len(self.rows) - body_h)))
        for offset in range(body_h):
            idx = self.scroll + offset
            if idx >= len(self.rows):
                break
            text = self._row_text(self.rows[idx], inner_w)
            kind = SELECT if idx == self.index else NORMAL
            frame.text(body_top + offset, inner_x, text, kind)
        if selected_hint and self.rows:
            last = y + height - 1
            tip = fit(selected_hint, inner_w)
            frame.text(last, inner_x, tip, DIM)


# ─────────────────────────────────────────────────────────────────────────────
# 面板：把「标题 + 预排版文本」画进一个框（表单、状态、预览都用它）
# ─────────────────────────────────────────────────────────────────────────────
class Panel:
    """只读文本面板（自动换行、可限制行数）。"""

    def __init__(self, title: str = "", lines=None, kind: str = NORMAL):
        self.title = title
        self.lines = list(lines or [])
        self.kind = kind

    def set(self, lines, title: str = None, kind: str = None) -> None:
        self.lines = list(lines or [])
        if title is not None:
            self.title = title
        if kind is not None:
            self.kind = kind

    def render(self, frame: Frame, y: int, x: int, height: int, width: int) -> int:
        if height < 2 or width < 4:
            return 0
        frame.box(y, x, height, width, self.title)
        return frame.lines(y + 1, x + 2, width - 4, self.lines, self.kind,
                           max_rows=height - 2)


# ─────────────────────────────────────────────────────────────────────────────
# 表单字段
# ─────────────────────────────────────────────────────────────────────────────
class _BaseField:
    """字段公共部分：编辑缓冲、错误行。"""

    kind = "base"

    def __init__(self, key: str, label: str, value=None, unit: str = "",
                 hint: str = "", editable: bool = True):
        self.key = key
        self.label = label
        self.value = value
        self.unit = unit
        self.hint = hint
        self.editable = editable
        self.editing = False
        self.buffer = ""
        self.error = ""

    # ── 编辑态 ──────────────────────────────────────────────────────
    def begin_edit(self) -> None:
        if not self.editable:
            return
        self.editing = True
        self.buffer = self.text_value()
        self.error = ""

    def cancel_edit(self) -> None:
        self.editing = False
        self.buffer = ""
        self.error = ""

    def commit_edit(self) -> bool:
        """提交缓冲；成功返回 True，失败只在 ``error`` 里留原因。"""
        ok = self.apply_buffer(self.buffer)
        if ok:
            self.editing = False
            self.buffer = ""
            self.error = ""
        return ok

    # ── 子类实现 ────────────────────────────────────────────────────
    def text_value(self) -> str:
        return "" if self.value is None else str(self.value)

    def display(self) -> str:
        return self.text_value()

    def apply_buffer(self, text: str) -> bool:
        self.value = text
        return True

    def handle_edit_key(self, key) -> bool:
        """编辑态按键：返回是否消费。"""
        if key == KEY_BACKSPACE:
            self.buffer = self.buffer[:-1]
            return True
        if key == KEY_DELETE:
            self.buffer = ""
            return True
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.buffer += key
            return True
        return False


class NumberField(_BaseField):
    """数值字段：直接输入，或 ← → 步进（Shift 为 10 倍步进）。"""

    kind = "number"

    def __init__(self, key: str, label: str, value=None, minimum: float = None,
                 maximum: float = None, step: float = 1.0, decimals: int = 0,
                 unit: str = "", hint: str = "", editable: bool = True,
                 coarse_multiplier: float = 10.0):
        super().__init__(key, label, value, unit, hint, editable)
        self.minimum = minimum
        self.maximum = maximum
        self.step = step
        self.decimals = decimals
        self.coarse_multiplier = coarse_multiplier

    def text_value(self) -> str:
        if self.value is None:
            return ""
        if self.decimals <= 0:
            return str(int(round(float(self.value))))
        return f"{float(self.value):.{self.decimals}f}"

    def display(self) -> str:
        text = self.text_value() or "未知"
        return f"{text} {self.unit}".strip()

    def parse(self, text: str) -> tuple:
        """返回 ``(数值或 None, 错误文案)``；只接受 ASCII 数字与小数点。"""
        text = (text or "").strip()
        if not text:
            return None, "不能为空。"
        body = text
        if self.decimals <= 0:
            if not body.isdigit():
                return None, "只能输入整数。"
        else:
            if any(ch not in "0123456789." for ch in body) or body.count(".") > 1:
                return None, "只能输入数字与小数点。"
        try:
            value = float(body) if self.decimals > 0 else int(body)
        except ValueError:
            return None, "不是合法数字。"
        if self.minimum is not None and float(value) < float(self.minimum):
            return None, f"不得小于 {self._fmt(self.minimum)}。"
        if self.maximum is not None and float(value) > float(self.maximum):
            return None, f"不得大于 {self._fmt(self.maximum)}。"
        return value, ""

    def _fmt(self, value) -> str:
        if self.decimals <= 0:
            return f"{int(round(float(value)))}"
        return f"{float(value):.{self.decimals}f}"

    def apply_buffer(self, text: str) -> bool:
        value, err = self.parse(text)
        if err:
            self.error = err
            return False
        self.value = value
        return True

    def adjust(self, direction: int, coarse: bool = False) -> bool:
        """← → 调整；已到边界时返回 False（界面给出提示，不静默吞掉按键）。"""
        if self.value is None:
            return False
        delta = self.step * (self.coarse_multiplier if coarse else 1)
        candidate = float(self.value) + direction * delta
        if self.minimum is not None:
            candidate = max(float(self.minimum), candidate)
        if self.maximum is not None:
            candidate = min(float(self.maximum), candidate)
        if self.decimals <= 0:
            candidate = int(round(candidate))
        else:
            candidate = round(candidate, self.decimals)
        if candidate == self.value:
            return False
        self.value = candidate
        self.error = ""
        return True


class TextField(_BaseField):
    """单行文本字段（档案名、备注）。不接受控制字符，超长在提交时拒绝。"""

    kind = "text"

    def __init__(self, key: str, label: str, value: str = "", max_len: int = 60,
                 hint: str = "", editable: bool = True, allow_empty: bool = True):
        super().__init__(key, label, value, "", hint, editable)
        self.max_len = max_len
        self.allow_empty = allow_empty

    def text_value(self) -> str:
        return "" if self.value is None else str(self.value)

    def display(self) -> str:
        return self.text_value() or "（未填写）"

    def apply_buffer(self, text: str) -> bool:
        text = (text or "").strip()
        if not text and not self.allow_empty:
            self.error = "不能为空。"
            return False
        if dwidth(text) > self.max_len * 2:
            self.error = f"过长，最多 {self.max_len} 个字符。"
            return False
        self.value = text
        return True


class Form:
    """字段组：Tab/↑↓ 切换，Enter 进入或提交输入，Esc 退出编辑或返回。"""

    def __init__(self, fields, title: str = ""):
        self.fields = list(fields)
        self.title = title
        self.index = 0
        self.message = ""
        self.message_kind = DIM

    # ── 状态 ────────────────────────────────────────────────────────
    @property
    def current(self):
        return self.fields[self.index] if self.fields else None

    @property
    def editing(self) -> bool:
        field = self.current
        return bool(field and field.editing)

    def values(self) -> dict:
        return {f.key: f.value for f in self.fields}

    def set_message(self, text: str, kind: str = DIM) -> None:
        self.message = text or ""
        self.message_kind = kind

    def move(self, delta: int) -> None:
        if not self.fields:
            return
        if self.editing and self.current:
            self.current.cancel_edit()
        self.index = (self.index + delta) % len(self.fields)

    def next_field(self) -> None:
        self.move(1)

    # ── 按键 ────────────────────────────────────────────────────────
    def handle_key(self, key) -> str:
        """返回动作名：``edit`` / ``commit`` / ``change`` / ``move`` / ``cancel`` / ``submit`` / ``none``。"""
        field = self.current
        if field is None:
            return "none"
        if field.editing:
            if is_enter(key):
                if field.commit_edit():
                    self.set_message("")
                    if self.index < len(self.fields) - 1:
                        self.move(1)          # 提交后落到下一字段，少按一次 Tab
                    return "commit"
                self.set_message(f"错误[E0302] {field.label}{field.error}", ERR)
                return "none"
            if is_cancel(key):
                field.cancel_edit()
                self.set_message("已取消输入。")
                return "cancel"
            if key in (KEY_TAB, KEY_DOWN):
                field.commit_edit()
                self.move(1)
                return "move"
            if key == KEY_UP:
                field.commit_edit()
                self.move(-1)
                return "move"
            if field.handle_edit_key(key):
                return "edit"
            return "none"
        # 非编辑态
        if key in (KEY_UP,):
            self.move(-1)
            return "move"
        if key in (KEY_DOWN, KEY_TAB):
            self.move(1)
            return "move"
        if is_enter(key):
            if field.editable:
                field.begin_edit()
                return "edit"
            return "none"
        if key in (KEY_LEFT, KEY_RIGHT, KEY_SLEFT, KEY_SRIGHT):
            if not isinstance(field, NumberField):
                return "none"
            direction = -1 if key in (KEY_LEFT, KEY_SLEFT) else 1
            coarse = key in (KEY_SLEFT, KEY_SRIGHT)
            if field.adjust(direction, coarse):
                self.set_message("")
                return "change"
            self.set_message(f"提示：{field.label}已达允许范围的边界。", WARN)
            return "none"
        if key == KEY_F10:
            return "submit"
        return "none"

    def check_all(self) -> list:
        """提交前的整体复核：返回错误行列表（空表示可以提交）。"""
        problems = []
        for field in self.fields:
            if field.editing and not field.commit_edit():
                problems.append(f"错误[E0302] {field.label}{field.error}")
            if isinstance(field, NumberField) and field.value is None and field.editable:
                problems.append(f"错误[E0302] {field.label}未填写。")
        return problems

    # ── 绘图 ────────────────────────────────────────────────────────
    def render(self, frame: Frame, y: int, x: int, height: int, width: int) -> None:
        if height < 3 or width < 10:
            return
        frame.box(y, x, height, width, self.title)
        label_w = max((dwidth(f.label) for f in self.fields), default=8)
        label_w = min(label_w, max(6, width // 3))
        value_w = max(8, width - label_w - 8)
        row = y + 1
        limit = y + height - 2
        self._hint_rows = 0
        for offset, field in enumerate(self.fields):
            if row > limit:
                break
            cursor = "▸" if offset == self.index else " "
            body = field.buffer if field.editing else field.display()
            if field.editing:
                body = body + "_"
                kind = SELECT if offset == self.index else NORMAL
            else:
                kind = SELECT if offset == self.index else (NORMAL if field.editable else DIM)
            text = f"{cursor} {pad(field.label, label_w)}  {fit(body, value_w)}"
            frame.text(row, x + 2, fit(text, width - 4), kind)
            row += 1
            if field.editing:
                frame.text(row, x + label_w + 5, fit("输入后回车确认 · Esc 取消",
                                                   max(4, width - label_w - 9)), DIM)
                self._hint_rows += 1
                row += 1
                if field.error:
                    frame.text(row, x + label_w + 5,
                               fit(f"错误[E0302] {field.error}", max(4, width - label_w - 9)), ERR)
                    self._hint_rows += 1
                    row += 1
                break
            if offset == self.index and field.hint and row <= limit:
                frame.text(row, x + label_w + 5,
                           fit(field.hint, max(4, width - label_w - 9)), DIM)
                self._hint_rows += 1
                row += 1
        if self.message and row <= limit:
            kind = self.message_kind or DIM
            frame.text(row, x + 2, fit(self.message, width - 4), kind)


# ─────────────────────────────────────────────────────────────────────────────
# 对话框
# ─────────────────────────────────────────────────────────────────────────────
class _Dialog:
    """居中浮层的公共绘制。"""

    def __init__(self, title: str, lines=None, footer=None):
        self.title = title
        self.lines = list(lines or [])
        self.footer = list(footer or [])

    def _draw(self, frame: Frame, y: int, x: int, height: int, width: int,
              kind: str = NORMAL) -> int:
        frame.box(y, x, height, width, self.title, kind=kind, title_kind=kind)
        used = 0
        for line in self.lines:
            for piece in wrap(line, max(4, width - 4)):
                if y + 1 + used >= y + height - 1:
                    return used
                frame.text(y + 1 + used, x + 2, piece, kind)
                used += 1
        return used


class ConfirmDialog(_Dialog):
    """危险操作的二次确认：必须键入完整中文确认词（DESIGN §6.3），回显 ``*``。

    取消（Esc / Ctrl-C）不产生任何副作用；确认词不匹配时只在框内提示，不关闭。
    """

    def __init__(self, title: str, lines, word: str, danger: bool = True,
                 footer=None, extra_rows: int = 2):
        super().__init__(title, lines, footer)
        self.word = word
        self.danger = danger
        self.buffer = ""
        self.error = ""
        self.extra_rows = extra_rows

    def body_rows(self, width: int) -> int:
        rows = 0
        for line in self.lines:
            rows += len(wrap(line, max(4, width - 4)))
        return rows

    def render(self, frame: Frame, prompt: str = "") -> None:
        width = max(32, min(frame.cols - 2, max(40, frame.cols - 12)))
        body = self.body_rows(width)
        need = body + self.extra_rows + 4
        height = min(frame.rows - 2, max(8, need))
        y = max(0, (frame.rows - height) // 2)
        x = max(0, (frame.cols - width) // 2)
        kind = ERR if self.danger else WARN
        used = self._draw(frame, y, x, height, width, kind)
        row = y + 1 + used
        if row < y + height - 2:
            frame.text(row, x + 2, fit(prompt or f"键入「{self.word}」并回车确认，Esc 取消",
                                      width - 4), kind)
            row += 1
        echo = "*" * len(self.buffer)
        if row < y + height - 2:
            frame.text(row, x + 2, fit(f"确认：{echo}_", width - 4), SELECT)
            row += 1
        if self.error and row < y + height - 1:
            frame.text(row, x + 2, fit(self.error, width - 4), ERR)

    def feed(self, key) -> str:
        """喂键：返回 ``ok`` / ``cancel`` / ``none``。"""
        if is_cancel(key):
            return "cancel"
        if is_enter(key):
            if self.buffer.strip() == self.word:
                return "ok"
            self.error = f"确认词不匹配，请输入「{self.word}」后回车。"
            self.buffer = ""
            return "none"
        if key == KEY_BACKSPACE:
            self.buffer = self.buffer[:-1]
            self.error = ""
            return "none"
        if key == KEY_DELETE:
            self.buffer = ""
            self.error = ""
            return "none"
        if isinstance(key, str) and len(key) == 1 and key.isprintable():
            self.buffer += key
            self.error = ""
            return "none"
        return "none"

    def run(self, frame: Frame, prompt: str = "") -> bool:
        """模态运行，返回是否确认。"""
        while True:
            frame.win.erase()
            self.render(frame, prompt)
            frame.win.refresh()
            key = read_key(frame.win)
            if key == KEY_RESIZE:
                continue
            if not key:
                continue
            result = self.feed(key)
            if result == "ok":
                return True
            if result == "cancel":
                return False


class MessageBox(_Dialog):
    """提示框：展示结论 / 原因 / 建议，任意键返回。"""

    def __init__(self, title: str, lines, kind: str = NORMAL, footer=None):
        super().__init__(title, lines, footer)
        self.kind = kind

    def run(self, frame: Frame, footer_text: str = "任意键返回") -> None:
        while True:
            frame.win.erase()
            width = max(30, min(frame.cols - 2, max(40, frame.cols - 16)))
            body = 0
            for line in self.lines:
                body += len(wrap(line, max(4, width - 4)))
            height = min(frame.rows - 2, max(6, body + 4))
            y = max(0, (frame.rows - height) // 2)
            x = max(0, (frame.cols - width) // 2)
            self._draw(frame, y, x, height, width, self.kind)
            frame.text(y + height - 2, x + 2, fit(footer_text, width - 4), DIM)
            frame.win.refresh()
            key = read_key(frame.win)
            if key == KEY_RESIZE:
                continue
            if key:
                return


class TextPane(_Dialog):
    """可滚动只读文本（日志面板、帮助浮层、长文本预览）。"""

    def __init__(self, title: str, lines, footer=None):
        super().__init__(title, lines, footer)
        self.offset = 0

    def _flat(self, width: int) -> list:
        out = []
        for line in self.lines:
            for piece in wrap(line, max(4, width - 4)):
                out.append(piece)
        return out

    def run(self, frame: Frame, footer_text: str = "↑↓ 滚动  Esc 返回") -> None:
        while True:
            frame.win.erase()
            width, height = frame.cols, frame.rows
            x, y = 0, 0
            frame.box(y, x, height, width, self.title)
            body = self._flat(width)
            visible = max(1, height - 3)
            self.offset = max(0, min(self.offset, max(0, len(body) - visible)))
            for offset in range(visible):
                idx = self.offset + offset
                if idx >= len(body):
                    break
                frame.text(y + 1 + offset, x + 2, fit(body[idx], width - 4), NORMAL)
            frame.text(height - 2, x + 2, fit(footer_text, width - 4), DIM)
            if len(body) > visible:
                frame.text(height - 2, max(x + 2, width - 12),
                           fit(f"{self.offset + 1}-{min(len(body), self.offset + visible)}"
                               f"/{len(body)}", 10), DIM)
            frame.win.refresh()
            key = read_key(frame.win)
            if is_cancel(key) or key in ("q", KEY_ENTER):
                return
            if key in (KEY_DOWN, "j"):
                self.offset += 1
            elif key in (KEY_UP, "k"):
                self.offset -= 1
            elif key == KEY_PGDN:
                self.offset += visible
            elif key == KEY_PGUP:
                self.offset -= visible
            elif key == KEY_HOME:
                self.offset = 0
            elif key == KEY_END:
                self.offset = max(0, len(body) - visible)


# ─────────────────────────────────────────────────────────────────────────────
# 顶部状态栏 / 底部按键行 / 进度行
# ─────────────────────────────────────────────────────────────────────────────
class StatusBar:
    """多行状态栏：``[(标签, 值, 样式)]`` → 自动按宽度换行，窄终端自动合并。"""

    def __init__(self, fields=None, lines=None):
        self.fields = list(fields or [])
        self.lines = list(lines or [])

    def add(self, label: str, value, kind: str = NORMAL) -> None:
        self.fields.append((label, value, kind))

    def clear(self) -> None:
        self.fields = []
        self.lines = []

    def _field_text(self, field) -> str:
        label, value, _kind = field
        return f"{label} {value}"

    def _packed(self, width: int) -> list:
        """把字段按宽度贪心合并成若干行，返回 ``[(文本, 样式)]``。"""
        out = []
        buf, buf_kind = "", NORMAL
        for label, value, kind in self.fields:
            piece = f"{label} {value}"
            candidate = piece if not buf else f"{buf}   {piece}"
            if buf and dwidth(candidate) > width:
                out.append((buf, buf_kind))
                buf, buf_kind = piece, kind
            else:
                buf, buf_kind = candidate, kind
        if buf:
            out.append((buf, buf_kind))
        return out

    def render(self, frame: Frame, y: int, x: int, width: int, height: int,
               mark: str = "") -> int:
        """返回实际占用的行数。``mark`` 为整栏徽标（如「只读模式」）。"""
        rows = 0
        if mark and rows < height:
            frame.text(y, x, fit(mark, width), TITLE)
            rows += 1
        for line in self.lines:
            if rows >= height:
                return rows
            frame.text(y + rows, x, fit(line, width), NORMAL)
            rows += 1
        for text, kind in self._packed(width):
            if rows >= height:
                return rows
            frame.text(y + rows, x, fit(text, width), kind)
            rows += 1
        return rows


class ProgressLine:
    """耗时操作的进度行（``Esc`` 只放弃本地等待，不终止已提交的 systemd 单元）。"""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, label: str):
        self.label = label
        self.count = 0

    def tick(self) -> None:
        self.count += 1

    def text(self) -> str:
        spin = self.FRAMES[self.count % len(self.FRAMES)]
        return fmt_progress(f"{spin} {self.label}")

    def render(self, frame: Frame, y: int, x: int, width: int) -> None:
        frame.text(y, x, fit(self.text(), width), DIM)

