#!/usr/bin/env python3
"""从 v100_full_spi.bin 构造覆盖 payload（自动裁掉 0xA00 前缀 + 逐镜像修校验和）。
用法: mk_payload.py <out.bin> [fileoffset=u32_dec ...]
fileoffset 用文件坐标（v100_full_spi.bin 里的偏移）。
"""
import struct, sys, hashlib

BASE  = 'v100_full_spi.bin'
IMGS  = [(0x00a00,0x0f000,0x00),(0x0f000,0x1fe00,0x03),
         (0x1fe00,0x30e00,0xe0),(0x30e00,0x3e200,0xe0),
         (0x3e200,0x3ee00,0x70),(0x3ee00,0x4fa00,0x70)]
# 尾部 0x70 镜像原始 sum=100，属特殊尾部约定，不强行归零
ZERO_SUM_OK = {0x00a00,0x0f000,0x1fe00,0x30e00,0x3e200}

out = sys.argv[1]
edits = []
for a in sys.argv[2:]:
    o, v = a.split('=')
    edits.append((int(o, 0), int(v, 0)))

d = bytearray(open(BASE, 'rb').read())
for o, v in edits:
    struct.pack_into('<I', d, o, v)

touched = set()
for s, e, code in IMGS:
    if any(s <= o < e for o, _ in edits):
        touched.add(s)
        if s in ZERO_SUM_OK:
            d[e-1] = 0
            d[e-1] = (256 - (sum(d[s:e-1]) & 0xFF)) & 0xFF
            assert sum(d[s:e]) & 0xFF == 0, f'checksum fail {s:#x}'
            print(f'  校验和已修 [{s:#x}:{e:#x}]')
        else:
            print(f'  镜像 [{s:#x}:{e:#x}] 尾部约定特殊，未改末字节')

pay = bytes(d[0x0a00:0x4fa00])
open(out, 'wb').write(pay)
print(f'{out}: {len(pay)} 字节 sha256={hashlib.sha256(pay).hexdigest()[:16]}')
print(f'  改动落在镜像: {[hex(x) for x in sorted(touched)]}')
