#!/usr/bin/env python3
"""生成驱动 registry 覆盖镜像（payload）：功耗上限 + XBAR/SYS + GPC/R1 字段。

两种用法，行为一致：

1. 命令行（与历史版本的既有用法向后兼容）:
     python3 tools/build_payload.py
     python3 tools/build_payload.py --src v100_original.rom --out v100_final_payload.bin
     python3 tools/build_payload.py --power 330 --xbar 1785 --gpc 1785 --perf 1785

2. 作为可导入模块（TUI 数据层使用）:
     from build_payload import build_payload
     payload = build_payload(rom_bytes, power_w=350,
                             clocks={"pstate_xbar_mhz": 1912, ...})

设计约束（见 tui/DESIGN.md §9.3(2)）:
  * 所有字段偏移一律来自 tools/derive_tables.py 的 derive() 动态推导，
    本文件不出现任何字面量字段偏移；换固件版本可自动重定位。
  * power_w / clocks 为 None 时使用历史默认值（350 W / 1912 / 1912 / 1800 / 1800 / 1912）。
  * payload = rom[0xA00 : 0xA00 + 327680]：这是驱动接口协议常量（README.md §三），
    不是字段偏移。
  * 生成后做严格差异检查：改动字节必须落在唯一一个 code==0xE0 数据镜像内，
    且该镜像与写入点必须完整落在 payload 窗口内，否则抛 ValueError（绝不产出可疑镜像）。
"""
import argparse
import hashlib
import importlib.util
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── 驱动接口协议常量（来源: README.md §三「payload 格式」）──────────────────
PAYLOAD_START = 0x0A00        # 覆盖镜像在 ROM 内的起始偏移
PAYLOAD_LENGTH = 0x50000      # 327680 字节

# ── 历史默认目标值（来源: docs/01 结论表、deploy/v100-boot-patch.sh 的既有默认）──
DEFAULT_POWER_W = 350
DEFAULT_CLOCKS = {
    'pstate_xbar_mhz': 1912,
    'pstate_sys_mhz': 1912,
    'pstate_gpc_hdr_mhz': 1800,
    'pstate_gpc_r1_mhz': 1800,
    'performance_entry0_mhz': 1912,
}

# P-State 表里带「有效标志位」的字段（来源: tools/derive_tables.py 的 flag = v & 0x8000）
PSTATE_FLAG = 0x8000
# 数据镜像类型 NPDS Image3（来源: tools/derive_tables.py 的 IMG_TYPE_DATA）
IMG_TYPE_DATA = 0xE0

# ── 命令行参数范围（来源: tui/DESIGN.md §3.1 字段表、§6.5 输入校验）──────────
POWER_MIN_W = 150.0
POWER_MAX_W = 350.0
CLOCK_MIN_MHZ = 300
CLOCK_MAX_MHZ = 1912


def _derive_module():
    """以文件路径载入 tools/derive_tables.py，避免依赖 sys.path 与安装位置。"""
    path = REPO_ROOT / 'tools' / 'derive_tables.py'
    spec = importlib.util.spec_from_file_location('v100_derive_tables', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'无法载入 {path}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_clocks(clocks):
    """把调用方给的 clocks 与默认值合并，并校验键名与取值。"""
    out = dict(DEFAULT_CLOCKS)
    if clocks:
        unknown = sorted(k for k in clocks if k not in DEFAULT_CLOCKS)
        if unknown:
            raise ValueError(f'未知的 clocks 键: {unknown}')
        for k, v in clocks.items():
            if v is None:
                continue
            try:
                mhz = int(v)
            except (TypeError, ValueError):
                raise ValueError(f'{k} 必须是整数 MHz，收到 {v!r}')
            if mhz <= 0:
                raise ValueError(f'{k} 必须为正整数 MHz，收到 {v!r}')
            out[k] = mhz
    return out


def build_payload(rom, power_w=None, clocks=None, dt=None):
    """按目标功耗与频率字段生成驱动 registry 覆盖镜像。

    参数:
        rom:      基准 VBIOS 镜像（bytes/bytearray，完整 dump，不是 payload 切片）
        power_w:  目标功耗上限（W）；None 表示沿用历史默认 350 W
        clocks:   dict，键同 tui/DESIGN.md §5.2 的 clocks 段
        dt:       已载入的 derive_tables 模块（可省略，内部自动载入）

    返回:
        bytes，长度恒为 PAYLOAD_LENGTH（327,680）

    异常:
        ValueError: 输入非法、超出该固件的可用范围、或出现越界改动
    """
    if not rom:
        raise ValueError('基准 ROM 为空')

    dt = dt or _derive_module()
    src = bytes(rom)
    d = bytearray(src)
    r = dt.derive(d)          # 所有偏移从这一步取得；固件不认识就直接失败

    pw = DEFAULT_POWER_W if power_w is None else float(power_w)
    clk = _resolve_clocks(clocks)

    row = r['power']['row']
    new_mw = int(round(pw * 1000))
    if new_mw <= row['low']:
        raise ValueError(
            f'新功耗上限必须大于该行下限 {row["low"] / 1000:.1f} W，收到 {pw:.1f} W')

    ps = r['pstate']
    pf = r['performance']
    writes = []               # [(偏移, 名称)]，用于窗口自检

    # 1) P-State 四个字段：XBAR / SYS 带标志位；GPC_HDR / GPC_R1 为裸 MHz
    for key, clock_key, flagged in (('xbar', 'pstate_xbar_mhz', True),
                                    ('sys', 'pstate_sys_mhz', True),
                                    ('gpc_hdr', 'pstate_gpc_hdr_mhz', False),
                                    ('gpc_r1', 'pstate_gpc_r1_mhz', False)):
        at = ps[key]['at']
        mhz = clk[clock_key]
        value = (PSTATE_FLAG | mhz) if flagged else mhz
        struct.pack_into('<HH', d, at, value, mhz // 4)
        writes.append((at, key))

    # 2) Performance ENTRY0 第 3 字段（u16 = MHz/4），偏移由 derive() 导出
    perf_at = pf['entry0_3rd_at']
    struct.pack_into('<H', d, perf_at, clk['performance_entry0_mhz'] // 4)
    writes.append((perf_at, 'performance_entry0'))

    # 3) 功耗行：rated 与 max 两个 u32 都要写（与 README §三 一致）
    struct.pack_into('<I', d, row['at'] + 6, new_mw)
    struct.pack_into('<I', d, row['at'] + 10, new_mw)
    writes.append((row['at'] + 6, 'power_rated'))
    writes.append((row['at'] + 10, 'power_max'))

    # 4) 只重算「唯一包含改动」的数据镜像校验和
    touched = [i for i in r['images']
               if i['code'] == IMG_TYPE_DATA and i['start'] <= row['at'] < i['end']]
    if len(touched) != 1:
        raise ValueError(f'功耗行不在唯一数据镜像内（命中 {len(touched)} 个）')
    lo, hi = touched[0]['start'], touched[0]['end']
    dt.checksums(d, touched)

    # 5) 严格差异检查：改动必须全部落在该数据镜像内
    diff = [i for i in range(len(d)) if d[i] != src[i]]
    if not diff:
        raise ValueError('目标值与现固件完全相同，未产生任何改动')
    outside = [i for i in diff if not (lo <= i < hi)]
    if outside:
        raise ValueError(f'出现数据镜像之外的改动: {[hex(x) for x in outside[:8]]}')

    # 6) payload 窗口自检：写入点、数据镜像都必须完整落在窗口内
    win_lo, win_hi = PAYLOAD_START, PAYLOAD_START + PAYLOAD_LENGTH
    for at, what in writes:
        if not (win_lo <= at < win_hi):
            raise ValueError(f'{what} 的偏移 {at:#x} 不在 payload 窗口内')
    if not (win_lo <= lo and hi <= win_hi):
        raise ValueError(f'数据镜像 [{lo:#x}:{hi:#x}] 未被 payload 窗口完整覆盖')
    for im in r['images']:
        if im['code'] == IMG_TYPE_DATA and not (win_lo <= im['start'] and im['end'] <= win_hi):
            raise ValueError(f'数据镜像 {im["start"]:#x}..{im["end"]:#x} 不在 payload 窗口内')

    payload = bytes(d[win_lo:win_hi])
    if len(payload) != PAYLOAD_LENGTH:
        raise ValueError(f'payload 长度 {len(payload)} != {PAYLOAD_LENGTH}')

    # 7) 回读校验：把 payload 重新放回等长缓冲再推导一次，逐字段比对 + 校验和归零
    check = bytearray(b'\x00' * PAYLOAD_START) + bytearray(payload)
    r2 = dt.derive(check)
    got = {
        'power_rated_w': r2['power']['row']['rated'] / 1000,
        'power_max_w': r2['power']['row']['max'] / 1000,
        'pstate_xbar_mhz': r2['pstate']['xbar']['mhz'],
        'pstate_sys_mhz': r2['pstate']['sys']['mhz'],
        'pstate_gpc_hdr_mhz': r2['pstate']['gpc_hdr']['mhz'],
        'pstate_gpc_r1_mhz': r2['pstate']['gpc_r1']['mhz'],
        'performance_entry0_mhz': r2['performance']['entry0_3rd_u16'] * 4,
    }
    expect = {
        'power_rated_w': pw,
        'power_max_w': pw,
        'pstate_xbar_mhz': clk['pstate_xbar_mhz'],
        'pstate_sys_mhz': clk['pstate_sys_mhz'],
        'pstate_gpc_hdr_mhz': clk['pstate_gpc_hdr_mhz'],
        'pstate_gpc_r1_mhz': clk['pstate_gpc_r1_mhz'],
        # ENTRY0 字段以 MHz/4 编码，只能落在 4 的整数倍上：按编码后的有效值比对
        'performance_entry0_mhz': (clk['performance_entry0_mhz'] // 4) * 4,
    }
    mismatch = {k: (expect[k], got[k]) for k in expect
                if abs(float(got[k]) - float(expect[k])) > 1e-6}
    if mismatch:
        raise ValueError(f'回读字段与目标不符（期望, 实得）: {mismatch}')
    # 校验和比对以「原固件基线」为参照：原固件本身可能有非零镜像（如末尾 0x70 段），
    # 只要没有新增非零镜像就算通过。
    base_bad = {(i['start'], i['end']) for i in r['images']
                if sum(src[i['start']:i['end']]) & 0xFF}
    new_bad = [(i['start'], i['end']) for i in r2['images']
               if (i['start'], i['end']) not in base_bad
               and sum(check[i['start']:i['end']]) & 0xFF]
    if new_bad:
        raise ValueError(f'回读出现新的非零校验和镜像: {[(hex(a), hex(b)) for a, b in new_bad]}')
    return payload


def build_payload_report(rom, power_w=None, clocks=None):
    """生成 payload 并返回 (payload, 摘要 dict)，供 CLI 与 TUI 展示。"""
    dt = _derive_module()
    pw = DEFAULT_POWER_W if power_w is None else float(power_w)
    clk = _resolve_clocks(clocks)
    payload = build_payload(rom, power_w=pw, clocks=clk, dt=dt)

    r = dt.derive(bytearray(rom))
    win_lo, win_hi = PAYLOAD_START, PAYLOAD_START + PAYLOAD_LENGTH
    end = min(len(rom), win_hi)
    diff = [i for i in range(win_lo, end)
            if payload[i - win_lo] != rom[i]]
    row = r['power']['row']
    data_img = next((i for i in r['images']
                     if i['code'] == IMG_TYPE_DATA and i['start'] <= row['at'] < i['end']), None)
    return payload, dict(
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        changed_bytes=len(diff),
        first_offset=min(diff) if diff else None,
        last_offset=max(diff) if diff else None,
        power_w=pw,
        clocks=clk,
        power_row_index=row['i'],
        power_old_w=row['rated'] / 1000,
        power_low_w=row['low'] / 1000,
        xbar_old_mhz=r['pstate']['xbar']['mhz'],
        gpc_old_mhz=r['pstate']['gpc_hdr']['mhz'],
        perf_old_mhz=r['performance']['entry0_3rd_u16'] * 4,
        perf_effective_mhz=(clk['performance_entry0_mhz'] // 4) * 4,
        data_image=(data_img['start'], data_img['end']) if data_img else None,
    )


def _power_arg(text):
    """--power 的参数校验：只接受 150–350 W，越界给中文错误（不抛栈）。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'功耗必须是数字，收到 {text!r}')
    if not (POWER_MIN_W <= value <= POWER_MAX_W):
        raise argparse.ArgumentTypeError(
            f'功耗必须在 {POWER_MIN_W:.0f}–{POWER_MAX_W:.0f} W 之间，收到 {value:g} W')
    return value


def _mhz_arg(text):
    """--xbar/--sys/--gpc/--perf 的参数校验：只接受 300–1912 MHz。"""
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f'频率必须是整数 MHz，收到 {text!r}')
    if not (CLOCK_MIN_MHZ <= value <= CLOCK_MAX_MHZ):
        raise argparse.ArgumentTypeError(
            f'频率必须在 {CLOCK_MIN_MHZ}–{CLOCK_MAX_MHZ} MHz 之间，收到 {value} MHz')
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='生成 V100 VBIOS 驱动 registry 覆盖镜像（payload）',
        epilog='示例：python3 tools/build_payload.py firmware/v100_original_nvflash.rom '
               '--power 350 --out /tmp/v100_payload.bin')
    ap.add_argument('rom', nargs='?', default=None,
                    help='基准 ROM（完整 dump，如 firmware/v100_original_nvflash.rom）；'
                         '缺省时在当前目录找 v100_original.rom')
    ap.add_argument('--src', default=None,
                    help='（兼容旧写法）基准 ROM 路径，等价于位置参数')
    ap.add_argument('--out', default='v100_final_payload.bin',
                    help='输出文件名，默认 v100_final_payload.bin')
    ap.add_argument('--power', type=_power_arg, default=DEFAULT_POWER_W,
                    help=f'功耗上限 (W)，范围 {POWER_MIN_W:.0f}–{POWER_MAX_W:.0f}，'
                         f'默认 {DEFAULT_POWER_W}')
    ap.add_argument('--xbar', type=_mhz_arg, default=DEFAULT_CLOCKS['pstate_xbar_mhz'],
                    help='XBAR / 核心上报上限 (MHz)')
    ap.add_argument('--sys', type=_mhz_arg, default=None,
                    help='SYS (MHz)，缺省与 --xbar 同值')
    ap.add_argument('--gpc', type=_mhz_arg, default=DEFAULT_CLOCKS['pstate_gpc_hdr_mhz'],
                    help='Applications GPC_HDR / GPC_R1 (MHz)')
    ap.add_argument('--perf', type=_mhz_arg, default=None,
                    help='Performance ENTRY0 (MHz)，缺省与 --xbar 同值')
    a = ap.parse_args(argv)

    src_text = a.rom or a.src or 'v100_original.rom'
    src = Path(src_text)
    if not src.is_file():
        ap.error(f'基准 ROM 不存在或不可读：{src_text}')
    rom = src.read_bytes()
    clocks = {
        'pstate_xbar_mhz': a.xbar,
        'pstate_sys_mhz': a.sys if a.sys is not None else a.xbar,
        'pstate_gpc_hdr_mhz': a.gpc,
        'pstate_gpc_r1_mhz': a.gpc,
        'performance_entry0_mhz': a.perf if a.perf is not None else a.xbar,
    }
    try:
        payload, rep = build_payload_report(rom, power_w=a.power, clocks=clocks)
    except (ValueError, RuntimeError) as exc:
        ap.error(str(exc))
    out = Path(a.out)
    out.write_bytes(payload)
    print(f'输出文件 : {out.resolve()}')
    print(f'体积     : {rep["size"]} 字节  ({rep["size"] // 1024} KiB)')
    print(f'sha256   : {rep["sha256"]}')
    print(f'基准 ROM : {src}  ({len(rom)} 字节)')
    print(f'  功耗 row{rep["power_row_index"]}   : {rep["power_old_w"]:.0f} W -> {a.power:.0f} W')
    print(f'  XBAR / SYS    : {rep["xbar_old_mhz"]} -> {clocks["pstate_xbar_mhz"]} / '
          f'{clocks["pstate_sys_mhz"]} MHz')
    print(f'  GPC / R1      : {rep["gpc_old_mhz"]} -> {clocks["pstate_gpc_hdr_mhz"]} / '
          f'{clocks["pstate_gpc_r1_mhz"]} MHz')
    print(f'  PERF3         : {rep["perf_old_mhz"]} -> '
          f'{rep["perf_effective_mhz"]} MHz'
          + ('' if rep["perf_effective_mhz"] == clocks["performance_entry0_mhz"]
             else f'（该字段按 4 MHz 对齐，{clocks["performance_entry0_mhz"]} '
                  f'实际写为 {rep["perf_effective_mhz"]}）'))
    print(f'  改动字节数    : {rep["changed_bytes"]}  '
          f'(区间 {rep["first_offset"]:#x}..{rep["last_offset"]:#x})')
    print()
    print('下一步：将本文件作为覆盖镜像写入 registry，或交给开机持久化单元使用。')
    print('注意：注入会中断图形会话；本工具不写 flash、不改电压。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
