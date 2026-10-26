#!/usr/bin/env python3
"""生成最终配置 payload：功耗 350W + XBAR/SYS 1912 + GPC/R1 1800。"""
import struct, hashlib
SRC='v100_original.rom'; IMG=(0x031400,0x03e800)
A0=0x33c20; GPC_HDR=0x3156e; GPC_R1=0x315c0
XBAR=0x31597; SYS=0x315e9; PERF3=0x31523; ROW4=0x33489
XB=1912; GP=1800; PW=350

d=bytearray(open(SRC,'rb').read())
for o in (XBAR,SYS): struct.pack_into('<HH',d,o,0x8000|XB,XB//4)
struct.pack_into('<H',d,PERF3,XB//4)
for o in (GPC_HDR,GPC_R1): struct.pack_into('<HH',d,o,GP,GP//4)
struct.pack_into('<I',d,ROW4+6,PW*1000); struct.pack_into('<I',d,ROW4+10,PW*1000)
s,e=IMG; d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
assert sum(d[s:e])&0xFF==0
pay=bytes(d[0x0a00:0x50a00])
open('v100_final_payload.bin','wb').write(pay)
print(f'v100_final_payload.bin  {len(pay)} 字节  sha256={hashlib.sha256(pay).hexdigest()}')

# 回读
p=open('v100_final_payload.bin','rb').read(); o=lambda x:x-0xa00
u16=lambda x: struct.unpack('<H',p[o(x):o(x)+2])[0]
u32=lambda x: struct.unpack('<I',p[o(x):o(x)+4])[0]
print(f'  功耗 row4   : {u32(ROW4+2)//1000} / {u32(ROW4+6)//1000} / {u32(ROW4+10)//1000} W')
print(f'  XBAR / SYS  : {u16(XBAR)&0x7fff} / {u16(SYS)&0x7fff} MHz  (ratio {u16(XBAR+2)})')
print(f'  GPC / R1    : {u16(GPC_HDR)} / {u16(GPC_R1)} MHz  (ratio {u16(GPC_HDR+2)})')
print(f'  PERF3       : {u16(PERF3)} (x4={u16(PERF3)*4})')
