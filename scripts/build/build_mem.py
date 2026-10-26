#!/usr/bin/env python3
"""显存时钟探测变体。基底保留已生效的 350W + XBAR/GPC 配置。"""
import struct, hashlib
SRC='v100_original.rom'; IMG=(0x031400,0x03e800)
XBAR=0x31597; SYS=0x315e9; PERF3=0x31523
GPC_HDR=0x3156e; GPC_R1=0x315c0; ROW4=0x33489
MEM=0x3152b          # Performance row3 sub2: 877 MHz
MEM_2=0x3152d        # 同处第二个字段 17261
MEM_Q=0x3152f        # 同处第三字段 219 (=877/4)
MEM_BASE=0x31576     # Base Clock row0 sub2 = 17261

def build(name, mem=None, mem2=None, keep_clk=True):
    d=bytearray(open(SRC,'rb').read())
    if keep_clk:   # 保留 350W + 1912/1800
        for o in (XBAR,SYS): struct.pack_into('<HH',d,o,0x8000|1912,478)
        struct.pack_into('<H',d,PERF3,478)
        for o in (GPC_HDR,GPC_R1): struct.pack_into('<HH',d,o,1800,450)
        struct.pack_into('<I',d,ROW4+6,350000); struct.pack_into('<I',d,ROW4+10,350000)
    if mem:
        struct.pack_into('<H',d,MEM,mem); struct.pack_into('<H',d,MEM_Q,mem//4)
    if mem2:
        struct.pack_into('<H',d,MEM_2,mem2); struct.pack_into('<H',d,MEM_BASE,mem2)
    s,e=IMG; d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
    assert sum(d[s:e])&0xFF==0
    pay=bytes(d[0x0a00:0x50a00]); open(f'/tmp/mem_{name}.bin','wb').write(pay)
    print(f'  {name:22s} mem={mem} mem2={mem2}  sha={hashlib.sha256(pay).hexdigest()[:12]}')

print('显存变体:')
build('m0_base', None, None)                       # 对照：只有 350W+时钟
build('m1_1000_min', 1000, None)                   # 只改上报字段
build('m2_1000_full', 1000, round(17261*1000/877)) # 上报字段 + 关联字段
build('m3_1126', 1126, round(17261*1126/877))      # 更激进：+28%
