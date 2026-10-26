#!/usr/bin/env python3
"""生成本卡可刷写的完整 VBIOS 镜像（330,240 字节）。

与覆盖 payload 的区别：保留 0x0A00 NVGIO 描述符前缀，是 nvflash 认的完整格式。
改动与 v100_final_payload.bin 完全一致（350W + XBAR1912/GPC1800），
且**只动 Image3 数据镜像**，Image0/Image1 代码段逐字节不变。
"""
import struct, hashlib, sys

SRC='v100_original.rom'; OUT='v100_flashable_350w.rom'
IMGS=[('Image0 Legacy',0x000a00,0x00ee00),('Image1 EFI',0x00ee00,0x020600),
      ('Image2 NPDS',0x020600,0x031400),('Image3 NPDS',0x031400,0x03e800),
      ('Image4 NPDS70',0x03e800,0x03fa00),('Image5 NPDS70',0x03fa00,0x050a00)]
DATA_IMG=(0x031400,0x03e800)
FIELDS=[('ENTRY0_Q',0x31523,'<H',478),
        ('GPC_HDR',0x3156e,'<HH',(1800,450)),
        ('GPC_R1',0x315c0,'<HH',(1800,450)),
        ('XBAR',0x31597,'<HH',(0x8000|1912,478)),
        ('SYS',0x315e9,'<HH',(0x8000|1912,478)),
        ('功耗row4 rated',0x3348f,'<I',350000),
        ('功耗row4 max',0x33493,'<I',350000)]

orig=bytearray(open(SRC,'rb').read())
d=bytearray(orig)

for name,off,fmt,val in FIELDS:
    if isinstance(val,tuple): struct.pack_into(fmt,d,off,*val)
    else: struct.pack_into(fmt,d,off,val)

# 只重算 Image3 校验和
s,e=DATA_IMG
d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF

# 自检 1：所有镜像校验和
print('镜像校验和自检:')
for name,s,e in IMGS:
    tot=sum(d[s:e])&0xFF
    expect=140 if name=='Image5 NPDS70' else 0
    ok='OK' if tot==expect else f'FAIL(={tot}, 期望{expect})'
    print(f'  {name:16s} [{s:#08x}:{e:#08x}] 和={tot:3d}  {ok}')
    assert tot==expect, f'{name} 校验和错误'

# 自检 2：代码段必须逐字节不变
print('\n代码段完整性（签名相关）:')
for name,s,e in IMGS[:2]:
    same = d[s:e]==orig[s:e]
    print(f'  {name:16s} 与原始镜像逐字节{"相同 ✅" if same else "不同 ❌"}')
    assert same, f'{name} 被意外改动'

# 自检 3：差异清单
diffs=[i for i in range(len(d)) if d[i]!=orig[i]]
print(f'\n差异字节共 {len(diffs)} 个: {[hex(i) for i in diffs]}')
assert all(DATA_IMG[0]<=i<DATA_IMG[1] for i in diffs), '存在数据镜像之外的改动'

open(OUT,'wb').write(d)
h=hashlib.sha256(d).hexdigest()
print(f'\n已生成 {OUT}  {len(d)} 字节  sha256={h}')
print(f'原厂    {SRC}  {len(orig)} 字节  sha256={hashlib.sha256(bytes(orig)).hexdigest()[:16]}…')
