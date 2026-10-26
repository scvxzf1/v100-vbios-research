#!/usr/bin/env python3
"""以编程器完整 dump 为基底，生成可刷写的 1 MiB 修改版。

改动与已验证的覆盖配置一致：功耗 350W + XBAR/SYS 1912 + GPC/R1 1800 + ENTRY0_Q 478
只动 Image3 数据镜像的 20 字节及其校验和；Image0/Image1 代码段、Image5 动态区、
0xA00 NVGIO 前缀区全部原样保留。
"""
import struct, hashlib

SRC='v100_full_dump_20261025.bin'
OUT='v100_FLASH_350w_FULL.bin'
DATA_IMG=(0x031400,0x03e800)
IMGS=[('Image0 Legacy',0x000a00,0x00ee00),('Image1 EFI',0x00ee00,0x020600),
      ('Image2 NPDS',0x020600,0x031400),('Image3 NPDS',0x031400,0x03e800),
      ('Image4 NPDS70',0x03e800,0x03fa00),('Image5 NPDS70',0x03fa00,0x050a00)]
FIELDS=[('ENTRY0_Q',0x31523,'<H',478),
        ('GPC_HDR',0x3156e,'<HH',(1800,450)),
        ('GPC_R1',0x315c0,'<HH',(1800,450)),
        ('XBAR',0x31597,'<HH',(0x8000|1912,478)),
        ('SYS',0x315e9,'<HH',(0x8000|1912,478)),
        ('功耗row4 rated',0x3348f,'<I',350000),
        ('功耗row4 max',0x33493,'<I',350000)]

orig=bytearray(open(SRC,'rb').read())
d=bytearray(orig)
print(f'基底: {SRC}  {len(orig)} 字节  sha256={hashlib.sha256(bytes(orig)).hexdigest()[:16]}…')

print('\n字段写入与回读:')
for name,off,fmt,val in FIELDS:
    old=struct.unpack(fmt,d[off:off+struct.calcsize(fmt)])
    if isinstance(val,tuple): struct.pack_into(fmt,d,off,*val)
    else: struct.pack_into(fmt,d,off,val)
    new=struct.unpack(fmt,d[off:off+struct.calcsize(fmt)])
    print(f'  {name:16s} @{off:#08x}  {old} -> {new}')

# 仅重算 Image3 校验和；Image5 的动态日志区不归零化（其和随运行变化，非固定校验）
s,e=DATA_IMG
d[e-1]=0; d[e-1]=(256-(sum(d[s:e-1])&0xFF))&0xFF
d[0x3e7ff]=d[e-1]
print(f'\nImage3 校验和 -> {d[e-1]}  (重算后 sum%256={sum(d[s:e])&0xFF})')

print('\n镜像校验和:')
for name,s,e in IMGS:
    tot=sum(d[s:e])&0xFF
    note='' if name.startswith('Image5') else ('  OK' if tot==0 else '  FAIL')
    print(f'  {name:16s} 和={tot:3d}{note}')
    if not name.startswith('Image5'): assert tot==0

print('\n不变性检查:')
for name,s,e in IMGS[:2]:
    print(f'  {name:16s} 逐字节未变: {"是 ✅" if d[s:e]==orig[s:e] else "否 ❌"}')
    assert d[s:e]==orig[s:e]
for name,s,e in [('前缀区',0x0,0xa00),('Image4',0x3e800,0x3fa00),('Image5',0x3fa00,0x50a00),
                 ('结尾 0xFF 区',0x50a00,0x100000)]:
    print(f'  {name:16s} 逐字节未变: {"是 ✅" if d[s:e]==orig[s:e] else "否 ❌"}')
    assert d[s:e]==orig[s:e]

diffs=[i for i in range(len(d)) if d[i]!=orig[i]]
print(f'\n差异字节 {len(diffs)} 个: {[hex(i) for i in diffs]}')
assert all(DATA_IMG[0]<=i<DATA_IMG[1] for i in diffs), '存在 Image3 之外的改动！'

open(OUT,'wb').write(d)
print(f'\n已生成 {OUT}  {len(d)} 字节 ({hex(len(d))})')
print(f'  sha256={hashlib.sha256(bytes(d)).hexdigest()}')
