#!/usr/bin/env python3
"""P38 字段测试。基底=最终配置(350W+XBAR1912+GPC1800)，只改 P38。"""
import struct, hashlib
SRC='v100_original.rom'; IMGS=[(0x000a00,0x00ee00),(0x00ee00,0x020600),(0x020600,0x031400),
                               (0x031400,0x03e800),(0x03e800,0x03fa00)]
XBAR=0x31597; SYS=0x315e9; PERF3=0x31523; GPC_HDR=0x3156e; GPC_R1=0x315c0
ROW4=0x33489; P38A=0x38ea9; P38B=0x391fb

def fix(d):
    for s,e in IMGS:
        d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
        assert sum(d[s:e])&0xFF==0

def build(name, p38=None):
    d=bytearray(open(SRC,'rb').read())
    for o in (XBAR,SYS): struct.pack_into('<HH',d,o,0x8000|1912,478)
    struct.pack_into('<H',d,PERF3,478)
    for o in (GPC_HDR,GPC_R1): struct.pack_into('<HH',d,o,1800,450)
    struct.pack_into('<I',d,ROW4+6,350000); struct.pack_into('<I',d,ROW4+10,350000)
    if p38 is not None:
        struct.pack_into('<H',d,P38A,p38); struct.pack_into('<H',d,P38B,p38)
    fix(d)
    pay=bytes(d[0x0a00:0x50a00]); open(f'/tmp/p38_{name}.bin','wb').write(pay)
    print(f'  {name:14s} P38={p38 if p38 else "原厂1530"}  sha={hashlib.sha256(pay).hexdigest()[:12]}')

print('P38 变体:')
build('q0_ctrl',  None)
build('q1_1912',  1912)
build('q2_2000',  2000)
build('q3_1200',  1200)
