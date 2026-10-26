#!/usr/bin/env python3
"""第三轮：验证 Max Clocks = min(XBAR, 1912+A0)，并测 A0 能否突破实际 1777 平台。"""
import struct, hashlib
SRC='v100_original.rom'; IMG=(0x031400,0x03e800)
A0=0x33c20; GPC_HDR=0x3156e; GPC_R1=0x315c0
XBAR=0x31597; SYS=0x315e9; PERF3=0x31523; ROW4=0x33489

def build(name, xbar=None, gpc=None, a0=None, power=None):
    d=bytearray(open(SRC,'rb').read())
    if xbar:
        for o in (XBAR,SYS): struct.pack_into('<HH',d,o,0x8000|xbar,xbar//4)
        struct.pack_into('<H',d,PERF3,xbar//4)
    if gpc:
        for o in (GPC_HDR,GPC_R1): struct.pack_into('<HH',d,o,gpc,gpc//4)
    if a0 is not None: struct.pack_into('<i',d,A0,a0*1000)
    if power:
        struct.pack_into('<I',d,ROW4+6,power*1000); struct.pack_into('<I',d,ROW4+10,power*1000)
    s,e=IMG; d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
    assert sum(d[s:e])&0xFF==0
    pay=bytes(d[0x0a00:0x50a00]); open(f'/tmp/clk3_{name}.bin','wb').write(pay)
    print(f'  {name:24s} xbar={xbar} gpc={gpc} a0={a0} pwr={power}')

print('第三轮变体:')
build('p1_x2000_a0_100', 2000, None, 100)
build('p2_x2000_a0_300', 2000, None, 300)
build('p3_x1912_a0_200', 1912, None, 200)
build('p4_x2000_a0_300_g1500', 2000, 1500, 300, 350)
