#!/usr/bin/env python3
"""向下调整显存频率的探测变体。基底=纯原厂，只动显存相关字节。

镜像链（88.00.4F.00.09）:
  0x000A00..0x00EE00 PCIR 0x00 | 0x00EE00..0x020600 PCIR 0x03
  0x020600..0x031400 NPDS 0xE0 | 0x031400..0x03E800 NPDS 0xE0
  0x03E800..0x03FA00 NPDS 0x70 | 0x03FA00..0x050A00 NPDS 0x70 (尾和=140，勿动)
"""
import struct, hashlib
SRC='v100_original.rom'
IMAGES=[(0x000a00,0x00ee00),(0x00ee00,0x020600),(0x020600,0x031400),
        (0x031400,0x03e800),(0x03e800,0x03fa00)]   # 前 5 个归零校验和
MEM_DATA=0x3152b      # 数据区：Performance row3 sub2 频率
MEM_DATA_Q=0x3152f    # 同处比率字段 (=频率/4)
MEM_CODE=0x7e72       # Image0 clkprog 记录里的 877
MEM_EFI1=0x1154d      # Image1 副本
MEM_EFI2=0x1c206      # Image1 副本

def fix(d):
    for s,e in IMAGES:
        d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
        assert sum(d[s:e])&0xFF==0, f'checksum fail {s:#x}'

def build(name, mhz, targets):
    d=bytearray(open(SRC,'rb').read())
    for t in targets:
        struct.pack_into('<H', d, t, mhz)
    if MEM_DATA in targets:
        struct.pack_into('<H', d, MEM_DATA_Q, mhz//4)
    fix(d)
    pay=bytes(d[0x0a00:0x50a00]); open(f'/tmp/md_{name}.bin','wb').write(pay)
    print(f'  {name:16s} mem={mhz}  改={[hex(t) for t in targets]}  sha={hashlib.sha256(pay).hexdigest()[:12]}')

print('显存下调变体（基底=纯原厂）:')
build('d0_stock',  877, [])
build('d1_data600', 600, [MEM_DATA])
build('d2_code600', 600, [MEM_CODE])
build('d3_both600', 600, [MEM_DATA, MEM_CODE])
build('d4_all600',  600, [MEM_DATA, MEM_CODE, MEM_EFI1, MEM_EFI2])
