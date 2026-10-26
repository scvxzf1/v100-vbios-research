#!/usr/bin/env python3
"""从 SPI dump 生成 4 种 vbios 覆盖 payload 变体。"""
import struct, sys
d = bytearray(open('v100_stage1_350w.bin','rb').read())

IMGS = [(0x00a00,0x0f000,0x00),(0x0f000,0x1fe00,0x03),
        (0x1fe00,0x30e00,0xe0),(0x30e00,0x3e200,0xe0),
        (0x3e200,0x3ee00,0x70),(0x3ee00,0x4fa00,0x70)]

def set_last_image(b, s, e):
    """给该镜像头 indicator 置 bit7，并重算尾部校验和。"""
    b = bytearray(b)
    hdr = s + struct.unpack('<H', b[s+24:s+26])[0]
    b[hdr+21] |= 0x80
    b[e-1] = 0
    b[e-1] = (256 - (sum(b[s:e-1]) & 0xFF)) & 0xFF
    assert sum(b[s:e]) & 0xFF == 0
    return bytes(b)

V = {}
V['v1_full_1M']       = bytes(d)                                  # 原样全 1 MiB
V['v2_rom_from_A00']  = bytes(d[0x0a00:0x4fa00])                  # 从 55AA 开始，去掉 NVGIO 前缀
e0 = set_last_image(d, 0x30e00, 0x3e200)
V['v3_e0end']         = e0[:0x3e200]                              # 到 Image3 末，置 last-image
V['v4_e0end_A00']     = e0[0x0a00:0x3e200]                        # 同上但去前缀

for k, v in V.items():
    open(f'/tmp/payload_{k}.bin','wb').write(v)
    print(f'  {k:16s} {len(v):8d} 字节  sha256={__import__("hashlib").sha256(v).hexdigest()[:16]}')
