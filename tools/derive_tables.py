#!/usr/bin/env python3
"""V100 (GV100 PG503) VBIOS 字段定位与修改 —— 纯动态推导，不硬编码绝对值。

所有表位置都从 NPDS Image3 的 BIT 指针解析，因此在同一镜像上一次推导、一次校验。
编辑仅限 Image3（NPDS 0xE0），Image0/EFI 不触碰。

用法:
  python3 v100_derive.py <rom>                          # 只报告
  python3 v100_derive.py <rom> --power 350 --out o.rom  # 改功耗上限
"""
import argparse
import hashlib
import struct
import sys

IMG_TYPE_DATA = 0xE0


def images(d):
    """返回 (start, end, hdr, code_type, dev) 列表。"""
    out = []
    for start in range(0, len(d) - 25, 512):
        if d[start:start + 2] not in (b'\x55\xaa', b'VN'):
            continue
        hdr = start + struct.unpack('<H', d[start + 24:start + 26])[0]
        magic = b'PCIR' if d[start:start + 2] == b'\x55\xaa' else b'NPDS'
        if d[hdr:hdr + 4] != magic:
            continue
        size = struct.unpack('<H', d[hdr + 16:hdr + 18])[0] * 512
        if not size:
            continue
        out.append(dict(start=start, end=start + size, hdr=hdr,
                        code=d[hdr + 20],
                        dev=struct.unpack('<H', d[hdr + 6:hdr + 8])[0]))
    return out


def tokens(d, imgs):
    """解析第一个 legacy PCIR 镜像里的 BIT token 表。"""
    leg = [i for i in imgs if i['code'] == 0 and i['dev'] != 0x0000]
    if not leg:
        raise SystemExit('no legacy PCIR image')
    leg = leg[0]
    import re
    m = [x.start() for x in re.finditer(re.escape(b'\xff\xb8BIT\x00'), d)
         if leg['start'] <= x.start() < leg['end']]
    if len(m) != 1:
        raise SystemExit(f'expected 1 BIT table, found {len(m)}')
    bit = m[0]
    hs, ts, cnt = d[bit + 8], d[bit + 9], d[bit + 10]
    if sum(d[bit:bit + hs]) & 0xFF:
        raise SystemExit('BIT header checksum nonzero')
    out = {}
    for i in range(cnt):
        o = bit + hs + ts * i
        tid, ver, size, ptr = struct.unpack('<BBHH', d[o:o + 6])
        out[chr(tid)] = dict(ver=ver, size=size, raw=ptr, at=leg['start'] + ptr)
    return out, leg


def resolve(ptr, leg, efi_size):
    """BIT raw pointer -> 文件物理偏移。"""
    return leg['start'] + ptr + (efi_size if ptr >= (leg['end'] - leg['start']) else 0)


def derive(d):
    imgs = images(d)
    toks, leg = tokens(d, imgs)
    efi = [i for i in imgs if i['code'] == 3]
    efi_size = efi[0]['end'] - efi[0]['start'] if efi else 0
    data_imgs = [i for i in imgs if i['code'] == IMG_TYPE_DATA]
    if not data_imgs:
        raise SystemExit('no NPDS 0xE0 image')

    def ptr(tok, off):
        raw = struct.unpack('<I', d[toks[tok]['at'] + off: toks[tok]['at'] + off + 4])[0]
        return resolve(raw, leg, efi_size) if raw else None

    r = dict(images=imgs, tokens=toks, efi_size=efi_size, data_images=data_imgs)

    # ---- Power table (P+0x2c) ----
    p = ptr('P', 0x2c)
    hs, es, ec = d[p + 1], d[p + 2], d[p + 3]
    if (d[p], hs, es) != (0x30, 0x27, 0x36):
        raise SystemExit(f'power table header unexpected at {p:#x}: '
                         f'{d[p]:#04x} {hs:#04x} {es:#04x}')
    idx = d[p + 10]
    rows = []
    for i in range(ec):
        row = p + hs + es * i
        lo, rt, mx = struct.unpack('<III', d[row + 2:row + 14])
        rows.append(dict(i=i, at=row, cls=d[row] & 15, ch=d[row + 1],
                         low=lo, rated=rt, max=mx))
    r['power'] = dict(at=p, header_size=hs, entry_size=es, count=ec,
                      tgp_index=idx, row=rows[idx], rows=rows,
                      roles=[x for x in rows if x['rated'] > 1000])

    # ---- Performance (P+0x00) ----
    q = ptr('P', 0x00)
    if d[q] != 0x50:
        raise SystemExit(f'performance table version {d[q]:#x} != 0x50')
    hs2, sz, cnt, ss, sc = d[q + 1:q + 6]
    stride = sz + ss * sc
    last = q + hs2 + cnt * stride - stride
    # 注: entry_size / entry0_3rd_at 为附加导出字段（TUI 数据层需要写回该 u16，
    # 不得在别处重算 last+entry_size+4 这个公式）；既有键名与含义保持不变。
    r['performance'] = dict(at=q, header_size=hs2, rows=cnt, stride=stride,
                            entry_size=sz,
                            last_row=last,
                            entry0_3rd_at=last + sz + 4,
                            entry0_3rd_u16=struct.unpack(
                                '<H', d[last + sz + 4:last + sz + 6])[0])

    # ---- Base Clock / P-State (P+0x38) ----
    b = ptr('P', 0x38)
    if d[b] != 0x20:
        raise SystemExit(f'base clock version {d[b]:#x} != 0x20')
    hs3, sz3, cnt3, ss3, sc3 = d[b + 1:b + 6]
    stride3 = sz3 + ss3 * sc3
    rows3 = []
    for i in range(cnt3):
        row = b + hs3 + stride3 * i
        subs = []
        for j in range(sc3):
            o = row + sz3 + ss3 * j
            v = struct.unpack('<H', d[o:o + 2])[0]
            subs.append(dict(at=o, raw=v, flag=v & 0x8000, mhz=v & 0x7FFF,
                             ratio=struct.unpack('<H', d[o + 2:o + 4])[0]))
        rows3.append(dict(i=i, at=row, tag=d[row], subs=subs))
    r['pstate'] = dict(at=b, header_size=hs3, rows=cnt3, stride=stride3,
                       row=rows3,
                       gpc_hdr=rows3[0]['subs'][0],
                       xbar=rows3[1]['subs'][0],
                       gpc_r1=rows3[2]['subs'][0],
                       sys=rows3[3]['subs'][0])

    # ---- VFE (P+0x20) ----
    v = ptr('P', 0x20)
    ver, hs4 = d[v], d[v + 1]
    vsize, vcount, esize = d[v + 2], d[v + 3], d[v + 4]
    vn = d[v + 5]
    base_guess = v + hs4 + vsize * vcount
    clamp = struct.pack('<f', 1912.0)
    hits = []
    pos = v
    while True:
        pos = d.find(clamp, pos, v + 0x2000)
        if pos < 0:
            break
        hits.append(pos)
        pos += 1
    base = (hits[0] - 8 - 14 * esize) if hits else None
    r['vfe'] = dict(at=v, version=ver, header_size=hs4, var_size=vsize,
                    var_count=vcount, entry_size=esize, slots=vn,
                    base_formula=base_guess, base_derived=base,
                    clamp_1912_at=hits[:2],
                    slot13=base + 13 * esize if base else None,
                    slot14=base + 14 * esize if base else None)

    # ---- Factory clock record (P+0x98) ----
    o = ptr('P', 0x98)
    hs5 = d[o + 1]
    hdr = bytes(d[o:o + min(hs5, 16)])
    ent = 5
    a0 = dict(at=o + hs5 + 1, flag=d[o + hs5],
              khz=struct.unpack('<i', d[o + hs5 + 1:o + hs5 + 5])[0])
    a1 = dict(at=o + hs5 + ent + 1, flag=d[o + hs5 + ent],
              khz=struct.unpack('<i', d[o + hs5 + ent + 1:o + hs5 + ent + 5])[0])
    r['factory'] = dict(at=o, header=hdr, header_size=hs5, A0=a0, A1=a1,
                        size=hs5 + 4 * ent)
    return r


def checksums(d, imgs, only_end=None):
    """逐镜像令 additive checksum 归零；返回 [(start,end,old,new)]。"""
    out = []
    for im in imgs:
        s, e = im['start'], im['end']
        if only_end is not None and not (s <= only_end < e):
            continue
        want = (256 - (sum(d[s:e - 1]) & 0xFF)) & 0xFF
        old = d[e - 1]
        d[e - 1] = want
        assert sum(d[s:e]) & 0xFF == 0, f'checksum fix failed [{s:x}:{e:x}]'
        out.append((s, e, old, want))
    return out


def report(r, d):
    print(f'镜像链:')
    for im in r['images']:
        print(f'  {im["start"]:#08x}..{im["end"]:#08x} code={im["code"]:#04x} '
              f'dev={im["dev"]:04x} size={im["end"]-im["start"]:6d}'
              f'{"  <-- 数据区" if im["code"]==IMG_TYPE_DATA else ""}')
    print()
    pw = r['power']
    print(f'功耗表 @ {pw["at"]:#x}  hdr={pw["header_size"]} entry={pw["entry_size"]} '
          f'count={pw["count"]} tgp_index={pw["tgp_index"]}')
    for row in pw['rows']:
        if row['rated'] == 0:
            continue
        tag = ' <== TGP(生效行)' if row['i'] == pw['tgp_index'] else ''
        print(f'  row{row["i"]:2d} @{row["at"]:#07x} cls={row["cls"]:2d} ch={row["ch"]:3d} '
              f'min={row["low"]/1000:8.2f}W rated={row["rated"]/1000:8.2f}W '
              f'max={row["max"]/1000:8.2f}W{tag}')
    print()
    pf = r['performance']
    print(f'Performance @ {pf["at"]:#x}  entry0 第3字段(u16=MHz/4) 末行={pf["entry0_3rd_u16"]} '
          f'-> {pf["entry0_3rd_u16"]*4} MHz')
    print()
    ps = r['pstate']
    print(f'Base Clock / P-State @ {ps["at"]:#x}  rows={ps["rows"]} stride={ps["stride"]}')
    for k, label in (('gpc_hdr', 'P-State header GPC (PSTATE_HDR)'),
                     ('xbar', 'P-State row0 XBAR (inert)'),
                     ('gpc_r1', 'P-State row1 GPC (PSTATE_R1)'),
                     ('sys', 'P-State row2 SYS (inert)')):
        s = ps[k]
        print(f'  {label:38s} @{s["at"]:#07x}  raw={s["raw"]:#06x} '
              f'flag={s["flag"]:#06x} mhz={s["mhz"]:5d} ratio={s["ratio"]}')
    print()
    vf = r['vfe']
    print(f'VFE @ {vf["at"]:#x}  ver={vf["version"]:#04x} var={vf["var_size"]}x{vf["var_count"]} '
          f'entry={vf["entry_size"]} slots={vf["slots"]}')
    print(f'  base(编码公式)={vf["base_formula"]:#x}  base(由1912.0钳位反推)={vf["base_derived"]:#x}')
    if vf['base_derived']:
        for i, so in ((13, vf['slot13']), (14, vf['slot14'])):
            fl = struct.unpack('<f', bytes(d[so + 8:so + 12]))[0]
            print(f'  slot{i:2d} @ {so:#x}  +8 处 f32 = {fl:.4f}')
    print()
    fa = r['factory']
    print(f'工厂时钟记录 @ {fa["at"]:#x}  header={fa["header"].hex(" ")} hs={fa["header_size"]}')
    print(f'  A0 (GPC  offset) @ {fa["A0"]["at"]:#07x}  flag={fa["A0"]["flag"]}  {fa["A0"]["khz"]} kHz')
    print(f'  A1 (mem  offset) @ {fa["A1"]["at"]:#07x}  flag={fa["A1"]["flag"]}  {fa["A1"]["khz"]} kHz')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rom')
    ap.add_argument('--power', type=float, help='新功耗上限 (W)')
    ap.add_argument('--min-power', type=float, default=None, help='新功耗下限 (W)')
    ap.add_argument('--out', help='输出文件')
    a = ap.parse_args()

    raw = open(a.rom, 'rb').read()
    print(f'== {a.rom}  {len(raw)} 字节  sha256={hashlib.sha256(raw).hexdigest()[:16]}...')
    d = bytearray(raw)
    r = derive(d)
    report(r, d)
    print()

    if a.power is None:
        print('未指定 --power，仅报告。')
        return 0
    if not a.out:
        raise SystemExit('--power 需要 --out')

    row = r['power']['row']
    new_mw = int(round(a.power * 1000))
    print(f'功耗行 row{row["i"]} @ {row["at"]:#07x}: '
          f'{row["rated"]/1000:.2f}W -> {a.power:.2f}W')
    if new_mw <= row['low']:
        raise SystemExit('新上限必须大于最小值')

    old = (row['rated'], row['max'])
    struct.pack_into('<I', d, row['at'] + 6, new_mw)
    struct.pack_into('<I', d, row['at'] + 10, new_mw)
    if a.min_power is not None:
        struct.pack_into('<I', d, row['at'] + 2, int(round(a.min_power * 1000)))

    # 只重算被改动镜像的校验和；其它镜像保持逐字节不变
    touched = [i for i in r['images'] if i['code'] == IMG_TYPE_DATA
               and i['start'] <= row['at'] < i['end']]
    if len(touched) != 1:
        raise SystemExit(f'功耗行不在唯一数据镜像内: {touched}')
    fixed = checksums(d, touched)
    for s, e, o, n in fixed:
        print(f'  校验和 [{s:#x}:{e:#x}] 末字节 {o:#04x} -> {n:#04x}')
    # 严格差异检查：改动必须全部落在该镜像内
    lo, hi = touched[0]['start'], touched[0]['end']
    diff = [i for i in range(len(d)) if d[i] != raw[i]]
    outside = [i for i in diff if not (lo <= i < hi)]
    print(f'  改动字节数={len(diff)}  区间=[{min(diff):#x}..{max(diff):#x}]'
          f'  数据镜像=[{lo:#x}:{hi:#x}]  越界改动={len(outside)}')
    if outside:
        raise SystemExit(f'越界改动 {[hex(x) for x in outside[:10]]}')

    open(a.out, 'wb').write(d)
    print(f'\n已写出 {a.out}  ({len(d)} 字节)')
    print(f'  sha256={hashlib.sha256(d).hexdigest()}')

    # 回读校验
    d2 = bytearray(open(a.out, 'rb').read())
    r2 = derive(d2)
    rr = r2['power']['row']
    print(f'回读: row{rr["i"]} rated={rr["rated"]/1000:.2f}W max={rr["max"]/1000:.2f}W '
          f'(原 {old[0]/1000:.2f}W/{old[1]/1000:.2f}W)')
    bad = [i for i in r2['images'] if sum(d2[i['start']:i['end']]) & 0xFF]
    print(f'回读: 非零校验和镜像 {[(hex(i["start"]),hex(i["end"])) for i in bad] or "无"}')
    badimg0 = (bytes(d[ [i for i in r['images'] if i['code']==0][0]['start'] :
                         [i for i in r['images'] if i['code']==0][0]['end']])
               != bytes(raw[ [i for i in r['images'] if i['code']==0][0]['start'] :
                              [i for i in r['images'] if i['code']==0][0]['end']]))
    print(f'回读: Image0 是否被改动 = {badimg0}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
