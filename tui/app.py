"""应用主循环与命令行入口（t7：集成联调）。

职责边界：

* 解析命令行（``--rom`` / ``--readonly`` / ``--no-color`` / ``--once`` /
  ``--log-dir`` / ``--version`` / ``--help``）；
* 平台闸门（非 Linux 直接中文提示 + 退出码 2，DESIGN §6.1）；
* 把界面（:class:`tui.ui.App`）与数据层/执行层串起来，事件分发在 ``App`` 内部，
  本模块只负责「起、停、兜底」；
* **异常兜底**：任何异常都转成中文提示，并且保证终端一定被恢复
  （``curses.wrapper`` 正常退出会 ``endwin()``；本模块再用 ``termios`` 还原
  启动前保存的终端属性，作为第二道保险，全程不调用任何外部命令）。

分层纪律：本模块只调用 ``env_probe``（只读探测）、``gpu_state``（只读现值）、
``history``（本地 JSONL 写入）与 ``ui``；注入与持久化的实际执行仍在
``tui/inject.py`` / ``tui/persist.py``，由界面按需延迟导入。
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import os
import sys
import traceback
import unicodedata

from . import constants as C
from . import env_probe, gpu_state, history

APP_TITLE = "V100 快捷操作台"
APP_VERSION = "1.0"
PROGRAM = "v100-tui"

#: 平台不满足时的退出码（DESIGN §6.1）
EXIT_PLATFORM = 2
#: 内部错误兜底退出码（错误码分区 E09xx）
EXIT_INTERNAL = 1
#: 正常退出
EXIT_OK = 0

ERR_INTERNAL = "E0901"
ERR_REPORT = "E0902"


# ─────────────────────────────────────────────────────────────────────────────
# 命令行
# ─────────────────────────────────────────────────────────────────────────────
#: argparse 常见英文提示 → 中文（只覆盖会出现在本工具参数上的几种）
_ARG_ERROR_ZH = (
    ("unrecognized arguments:", "无法识别的参数："),
    ("the following arguments are required:", "缺少必需参数："),
    ("invalid choice:", "无效选项："),
    ("expected one argument", "该选项需要一个值"),
    ("ambiguous option:", "选项有歧义："),
    ("not allowed with argument", "不能与该选项同时使用："),
    ("argument ", "参数 "),
)


def _translate_arg_error(message: str) -> str:
    text = str(message)
    for english, chinese in _ARG_ERROR_ZH:
        text = text.replace(english, chinese)
    return text


class _ChineseParser(argparse.ArgumentParser):
    """把 argparse 的框架级文案也换成中文（不引入 gettext 依赖）。"""

    def error(self, message):                       # 参数错误：中文前缀 + 用法
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}：参数错误：{_translate_arg_error(message)}\n"
                         f"提示：用 `--help` 查看完整中文说明。\n")
        raise SystemExit(EXIT_PLATFORM)


def build_parser() -> argparse.ArgumentParser:
    parser = _ChineseParser(
        prog=PROGRAM,
        usage=f"%(prog)s [选项]",
        description=f"{APP_TITLE} v{APP_VERSION}——简体中文终端操作台（仅 Linux）。",
        epilog=(
            "三种核心能力：快速修改（生成本地覆盖镜像，任何权限都可用）、"
            "快速注入（提交独立 systemd 单元，会中断图形会话，需 root）、"
            "记录与持久化（配置档案、操作历史、开机自动注入）。\n"
            "危险操作必须键入中文确认词；本工具不写 flash、不改电压。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    group = parser.add_argument_group("选项")
    group.add_argument("-h", "--help", action="help", help="显示本帮助并退出。")
    group.add_argument("--version", action="version",
                       version=f"{APP_TITLE} v{APP_VERSION}", help="显示版本号并退出。")
    group.add_argument("--rom", metavar="路径", default=None,
                       help="指定基准 VBIOS 文件（完整 dump）；缺省按"
                            "「驱动 sysfs → nvidia-smi 转储 → 指定文件 → 仓库样本」顺序获取。")
    group.add_argument("--readonly", action="store_true",
                       help="只读模式：界面仍可生成覆盖镜像，但不写入任何需要改动的入口。")
    group.add_argument("--no-color", action="store_true", dest="no_color",
                       help="不使用颜色（单色终端或重定向时建议开启）。")
    group.add_argument("--once", action="store_true",
                       help="不进交互界面，只打印一份中文状态报告后退出。")
    group.add_argument("--log-dir", metavar="目录", default=None, dest="log_dir",
                       help="内部错误日志目录（缺省写入缓存目录下的 logs/）。")
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))


# ─────────────────────────────────────────────────────────────────────────────
# 终端保护（第二道保险）
# ─────────────────────────────────────────────────────────────────────────────
def _save_terminal():
    """在进入 curses 之前保存 stdin 的终端属性；非 TTY 返回 ``None``。"""
    try:
        import termios
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return None
        return fd, termios.tcgetattr(fd)
    except Exception:
        return None


def _restore_terminal(saved) -> None:
    """尽最大努力把终端恢复成进入前的样子（不调用 stty 等外部命令）。"""
    if saved is not None:
        fd, attrs = saved
        try:
            import termios
            termios.tcsetattr(fd, termios.TCSADRAIN, attrs)
        except Exception:
            pass
    # 光标可见、退出备用屏幕、关闭全部属性：即便上面拿不到 termios 也能救回来
    try:
        if os.isatty(sys.stdout.fileno()):
            sys.stdout.write("\033[0m\033[?25h\033[?1049l")
            sys.stdout.flush()
    except Exception:
        pass


@contextlib.contextmanager
def monochrome(enabled: bool):
    """``--no-color``：强制界面用单色属性集（语义靠符号与亮度表达）。"""
    if not enabled:
        yield
        return
    from . import widgets as W
    original = W.Theme

    class _NoColorTheme(original):                    # 局部替换，退出即还原
        def __init__(self, screen=None, colors=None):
            super().__init__(screen, colors=False)

    W.Theme = _NoColorTheme
    os.environ.setdefault("NO_COLOR", "1")
    try:
        yield
    finally:
        W.Theme = original


# ─────────────────────────────────────────────────────────────────────────────
# 纯文本报告（--once 或非 TTY）
# ─────────────────────────────────────────────────────────────────────────────
def _power(value) -> str:
    return "未知" if value is None else f"{float(value):.0f} W"


def _mhz(value) -> str:
    return "未知" if value is None else f"{int(value)} MHz"


def _tri(value, yes: str, no: str) -> str:
    if value is None:
        return "未知"
    return yes if value else no


def text_report(env, opts=None) -> int:
    """打印一份只读中文状态报告。失败也不抛异常，只用退出码表达。"""
    lines = [f"{APP_TITLE} v{APP_VERSION}（只读状态报告）",
             f"平台：{getattr(env, 'platform', '未知')}　"
             f"权限：{'root' if getattr(env, 'is_root', False) else '普通用户'}　"
             f"模式：{getattr(env, 'banner', '未知')}",
             f"驱动 registry 接口：{_tri(getattr(env, 'registry_ok', None), '可用', '不可用')}"
             f"　systemd-run：{_tri(getattr(env, 'systemd_run_ok', None), '可用', '不可用')}"
             f"　显示管理器：{getattr(env, 'dm_unit', None) or '未探测到'}"
             f"　lactd：{_tri(getattr(env, 'lactd_active', None), '运行中', '未运行')}"]
    try:
        snap = gpu_state.snapshot(env=env)
    except Exception as exc:                       # 报告失败不影响退出码语义
        lines.append(f"错误[{ERR_REPORT}] 状态读取失败：{exc}")
        _emit(lines)
        return EXIT_INTERNAL

    gpu = getattr(snap, "gpu", None)
    boot = getattr(snap, "boot", None) or {}
    name = getattr(gpu, "name", None) or "未检测到 V100"
    lines += [
        f"显卡：{name}　BDF：{getattr(snap, 'v100_bdf', None) or '未知'}",
        f"VBIOS：{getattr(gpu, 'vbios_version', None) or '未知'}",
        f"功耗上限：{_power(getattr(gpu, 'power_limit_w', None))}"
        f"　硬件上限：{_power(getattr(gpu, 'power_max_w', None))}"
        f"　原厂上限：{_power(getattr(gpu, 'power_default_w', None))}",
        f"核心上报上限：{_mhz(getattr(gpu, 'sm_max_mhz', None))}"
        f"　Applications 时钟：{_mhz(getattr(gpu, 'apps_graphics_mhz', None))}"
        f"　显存频率：{_mhz(getattr(gpu, 'mem_max_mhz', None))}",
        f"内存覆盖在位：{_tri(getattr(snap, 'override', None), '是', '否')}"
        f"　开机持久化：启用 {_tri(boot.get('enabled'), '是', '否')}"
        f" / 运行 {_tri(boot.get('active'), '是', '否')}",
    ]
    for note in list(getattr(snap, "notes", []) or []):
        lines.append(f"· {note}")
    for reason in list(getattr(env, "reasons", []) or []):
        lines.append(f"· {reason}")
    lines.append("交互界面请在终端中直接运行本命令（不带 --once）。")
    _emit(lines)
    return EXIT_OK


def _emit(lines) -> None:
    sys.stdout.write("\n".join(str(x) for x in lines) + "\n")
    sys.stdout.flush()


# ─────────────────────────────────────────────────────────────────────────────
# 内部错误兜底
# ─────────────────────────────────────────────────────────────────────────────
def crash_log_dir(opts=None):
    """错误日志目录：``--log-dir`` > 缓存目录下的 logs/。"""
    if opts is not None and getattr(opts, "log_dir", None):
        return env_probe.ensure_dir(os.path.abspath(opts.log_dir))
    return env_probe.ensure_dir(env_probe.cache_dir() / "logs")


def dump_crash(exc: BaseException, opts=None):
    """把 traceback 写入日志文件；写不成也不要再抛。返回路径或 ``None``。"""
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        path = crash_log_dir(opts) / f"crash-{stamp}.log"
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        path.write_text(f"# {APP_TITLE} v{APP_VERSION} 内部错误\n"
                        f"# 时间：{_dt.datetime.now().astimezone().isoformat()}\n"
                        f"# 平台：{sys.platform}\n\n{text}", encoding="utf-8")
        env_probe.chown_to_invoker(path)
        return path
    except Exception:
        return None


def handle_crash(exc: BaseException, opts=None) -> int:
    """统一内部错误出口：中文提示 + 日志路径，绝不把栈甩给用户。"""
    path = dump_crash(exc, opts)
    detail = f"{type(exc).__name__}: {exc}"
    _emit([f"错误[{ERR_INTERNAL}] 本工具遇到内部错误已退出，终端已恢复。",
           f"  原因：{detail}",
           f"  建议：问题可复现时，把下面的日志发给维护者后重启本工具。"])
    if path is not None:
        _emit([f"  日志：{path}"])
    try:
        history.record("内部错误", history.RESULT_FAILED, detail=detail,
                       log_path=str(path) if path else "")
    except Exception:
        pass
    return EXIT_INTERNAL


# ─────────────────────────────────────────────────────────────────────────────
# 启动流程
# ─────────────────────────────────────────────────────────────────────────────
def run_ui(env, opts=None) -> int:
    """启动交互界面；无论走哪条路径，返回前终端都已恢复。"""
    saved = _save_terminal()
    try:
        from . import ui                          # 延迟导入：--help/--version 不需要 curses
        deps = {}
        rom_path = getattr(opts, "rom", None) if opts is not None else None
        if rom_path:
            deps["rom_path"] = os.path.abspath(rom_path)
        app = ui.App(env=env, deps=deps)
        with monochrome(bool(getattr(opts, "no_color", False) if opts else False)):
            return int(app.run() or EXIT_OK)
    except KeyboardInterrupt:                     # Ctrl-C 视为正常退出（DESIGN §8）
        return EXIT_OK
    except SystemExit as exc:                     # 界面内部的 sys.exit()
        code = exc.code
        return EXIT_OK if code is None else int(code)
    except BaseException as exc:                  # 兜底：任何异常都要恢复终端
        return handle_crash(exc, opts)
    finally:
        _restore_terminal(saved)


def main(argv=None) -> int:
    """进程入口：解析参数 → 交互界面；非 TTY 时打印只读报告。

    平台闸门只有一处：交互路径交给 :meth:`tui.ui.App.run`（内建平台与 curses 检查，
    非 Linux 打印 Linux-only 中文说明并返回 2），本模块**不再判一次**；
    仅纯文本报告分支（不经过界面层）在这里补同样的判定与退出码。
    """
    opts = parse_args(argv)
    env = env_probe.probe(readonly=bool(opts.readonly))

    interactive = (not opts.once) and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        return run_ui(env, opts)

    if not getattr(env, "platform_ok", True):
        sys.stderr.write(env_probe.LINUX_ONLY_MESSAGE.format(
            platform=getattr(env, "platform", "未知")) + "\n")
        return EXIT_PLATFORM
    if not opts.once:
        _emit(["提示[W0103] 当前不是交互式终端，改为打印只读状态报告。",
               "如需界面，请在本机终端（或 SSH 会话）中运行。", ""])
    return text_report(env, opts)


if __name__ == "__main__":                        # pragma: no cover
    raise SystemExit(main())
