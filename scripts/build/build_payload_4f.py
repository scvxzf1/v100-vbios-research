#!/usr/bin/env python3
"""在真实 88.00.4F.00.09 ROM 上改 TGP row4 功耗，输出覆盖/刷写 payload。
用法: build_payload_4f.py <out.bin> <max_watts> [min_watts]
"""
import struct, sys, hashlib
out, maxw = sys.argv[1], int(sys.argv[2])
minw = int(sys.argv[3]) if len(sys.argv) > 3 else None

SRC = 'v100_original.rom'
d = bytearray(open(SRC, 'rb').read())
ROW4 = 0x33489
DATA_IMG = (0x031400, 0x03e800)      # 含功耗表的 NPDS 0xE0 镜像

lo, rt, mx = struct.unpack('<III', d[ROW4+2:ROW4+14])
print(f'原始 row4: min={lo/1000:.0f}W rated={rt/1000:.0f}W max={mx/1000:.0f}W')
struct.pack_into('<I', d, ROW4+6,  maxw*1000)
struct.pack_into('<I', d, ROW4+10, maxw*1000)
if minw: struct.pack_into('<I', d, ROW4+2, minw*1000)

s, e = DATA_IMG
d[e-1] = 0
d[e-1] = (256 - (sum(d[s:e-1]) & 0xFF)) & 0xFF
assert sum(d[s:e]) & 0xFF == 0, 'checksum fail'
print(f'  -> 新 row4: min={(minw or lo/1000):.0f}W rated={maxw}W max={maxw}W')
print(f'  数据镜像 [{s:#x}:{e:#x}] 校验和已归零')

pay = bytes(d[0x0a00:0x50a00])
open(out, 'wb').write(pay)
print(f'{out}: {len(pay)} 字节 sha256={hashlib.sha256(pay).hexdigest()[:16]}')
# 回读
p2 = open(out, 'rb').read()
lo2, rt2, mx2 = struct.unpack('<III', p2[ROW4-0xa00+2:ROW4-0xa00+14])
print(f'  回读 row4: min={lo2/1000:.0f}W rated={rt2/1000:.0f}W max={mx2/1000:.0f}W')
