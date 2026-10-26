#!/usr/bin/env python3
"""在 v100_original.rom 上生成时钟探测变体 payload（含校验和修复与回读验证）。"""
import struct, hashlib, os

SRC='v100_original.rom'; IMG=(0x031400,0x03e800)
GPC_HDR=0x3156e; GPC_R1=0x315c0          # Applications Clocks 来源
XBAR=0x31597;  SYS=0x315e9               # Max Clocks 候选
PERF3=0x31523                            # perf 末行 sub0 第3个 u16 (=max/4)
A0=0x33c20

def build(name, patches):
    d=bytearray(open(SRC,'rb').read())
    for off,val in patches:
        struct.pack_into('<I' if val>0xffff else '<H', d, off, val)
    s,e=IMG; d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
    assert sum(d[s:e])&0xFF==0
    pay=bytes(d[0x0a00:0x50a00])
    out=f'/tmp/clk_{name}.bin'; open(out,'wb').write(pay)
    print(f'{name:12s} {len(pay)}B sha={hashlib.sha256(pay).hexdigest()[:12]}')
    return out

def hx(off,want):
    d=open(SRC,'rb').read(); return struct.unpack('<H',d[off:off+2])[0]

print('=== 变体 ===')
build('v0_stock',    [])
build('v1_xbar1600', [(XBAR,0x8000|1600),(XBAR+2,400),(SYS,0x8000|1600),(SYS+2,400),(PERF3,400)])
build('v2_gpc1500',  [(GPC_HDR,1500),(GPC_HDR+2,375),(GPC_R1,1500),(GPC_R1+2,375)])
build('v3_a0_100',   [(A0,100000)])
build('v4_combo',    [(XBAR,0x8000|1600),(XBAR+2,400),(SYS,0x8000|1600),(SYS+2,400),(PERF3,400),
                      (GPC_HDR,1500),(GPC_HDR+2,375),(GPC_R1,1500),(GPC_R1+2,375),(A0,100000)])

print('\n=== 回读 v4_combo ===')
d=open('/tmp/clk_v4_combo.bin','rb').read()
o=lambda x:x-0xa00
print(f'  GPC_HDR = {struct.unpack("<H",d[o(GPC_HDR):o(GPC_HDR)+2])[0]} MHz')
print(f'  XBAR    = {struct.unpack("<H",d[o(XBAR):o(XBAR)+2])[0]&0x7fff} MHz (flag={struct.unpack("<H",d[o(XBAR):o(XBAR)+2])[0]>>15})')
print(f'  SYS     = {struct.unpack("<H",d[o(SYS):o(SYS)+2])[0]&0x7fff} MHz')
print(f'  PERF3   = {struct.unpack("<H",d[o(PERF3):o(PERF3)+2])[0]} (x4={struct.unpack("<H",d[o(PERF3):o(PERF3)+2])[0]*4})')
print(f'  A0      = {struct.unpack("<i",d[o(A0):o(A0)+4])[0]} kHz')
