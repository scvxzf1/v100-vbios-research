#!/usr/bin/env python3
"""第二轮：探测时钟上限能推到多高。"""
import struct, hashlib
SRC='v100_original.rom'; IMG=(0x031400,0x03e800)
GPC_HDR=0x3156e; GPC_R1=0x315c0
XBAR=0x31597; SYS=0x315e9; PERF3=0x31523
ROW4=0x33489

def build(name, xbar=None, gpc=None, power=None):
    d=bytearray(open(SRC,'rb').read())
    if xbar:
        for o in (XBAR,SYS):
            struct.pack_into('<H',d,o,0x8000|xbar); struct.pack_into('<H',d,o+2,xbar//4)
        struct.pack_into('<H',d,PERF3,xbar//4)
    if gpc:
        for o in (GPC_HDR,GPC_R1):
            struct.pack_into('<H',d,o,gpc); struct.pack_into('<H',d,o+2,gpc//4)
    if power:
        struct.pack_into('<I',d,ROW4+6,power*1000); struct.pack_into('<I',d,ROW4+10,power*1000)
    s,e=IMG; d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
    assert sum(d[s:e])&0xFF==0
    pay=bytes(d[0x0a00:0x50a00]); out=f'/tmp/clk2_{name}.bin'; open(out,'wb').write(pay)
    print(f'  {name:16s} xbar={xbar} gpc={gpc} power={power}  sha={hashlib.sha256(pay).hexdigest()[:12]}')

print('第二轮变体:')
build('w1_x1700', 1700)
build('w2_x1800', 1800)
build('w3_x1912', 1912)
build('w4_x1912g1912', 1912, 1912)
build('w5_x2000', 2000)
print('组合候选（350W + 时钟）:')
build('c1_350w_x1600', 1600, 1500, 350)
build('c2_350w_x1912', 1912, 1912, 350)
