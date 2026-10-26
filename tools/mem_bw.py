#!/usr/bin/env python3
"""显存有效带宽实测（CUDA driver API，D2D 拷贝）。

用 cuMemcpyDtoD 在设备内搬运大块数据，带宽 = 2*bytes/time（读+写）。
这是判断"显存频率是否真的提高"的地面真值：频率变了带宽必须跟着变。

用法: python3 mem_bw.py [pci] [重复次数]
"""
import ctypes as C, sys, time

PCI = sys.argv[1] if len(sys.argv) > 1 else "0000:04:00.0"
REP = int(sys.argv[2]) if len(sys.argv) > 2 else 6
SIZE = 256 << 20          # 256 MiB

CU = C.CDLL("libcuda.so.1")
CU.cuInit.argtypes=[C.c_uint]
CU.cuDeviceGetByPCIBusId.argtypes=[C.POINTER(C.c_int), C.c_char_p]
CU.cuDevicePrimaryCtxRetain.argtypes=[C.POINTER(C.c_void_p), C.c_int]
CU.cuCtxSetCurrent.argtypes=[C.c_void_p]
CU.cuMemAlloc_v2.argtypes=[C.POINTER(C.c_uint64), C.c_size_t]
CU.cuMemcpyDtoD_v2.argtypes=[C.c_uint64, C.c_uint64, C.c_size_t]
CU.cuMemsetD32_v2.argtypes=[C.c_uint64, C.c_uint, C.c_size_t]
CU.cuCtxSynchronize.argtypes=[]

CU.cuInit(0)
dev=C.c_int()
if CU.cuDeviceGetByPCIBusId(C.byref(dev), PCI.encode()): sys.exit("找不到 "+PCI)
ctx=C.c_void_p(); CU.cuDevicePrimaryCtxRetain(C.byref(ctx), dev); CU.cuCtxSetCurrent(ctx)

a=C.c_uint64(); b=C.c_uint64()
CU.cuMemAlloc_v2(C.byref(a), SIZE)
CU.cuMemAlloc_v2(C.byref(b), SIZE)
CU.cuMemsetD32_v2(a, 1, SIZE//4)
CU.cuCtxSynchronize()

# 读数辅助
import subprocess
def smi(f):
    o=subprocess.run(["nvidia-smi",f"--query-gpu={f}","--format=csv,noheader,nounits","-i",PCI],
                     capture_output=True,text=True).stdout.strip()
    return o

print(f"设备 {PCI}  vbios={smi('vbios_version')}  max_sm={smi('clocks.max.sm')}  limit={smi('power.limit')}")

# 预热
for _ in range(2): CU.cuMemcpyDtoD_v2(b, a, SIZE)
CU.cuCtxSynchronize()

ts=[]
for i in range(REP):
    t0=time.perf_counter()
    CU.cuMemcpyDtoD_v2(b, a, SIZE)
    CU.cuCtxSynchronize()
    dt=time.perf_counter()-t0
    ts.append(dt)
    print(f"  第{i+1}次: {dt*1000:.2f} ms -> {2*SIZE/dt/1e9:.1f} GB/s")

best=min(ts)
print(f"\n显存带宽: 最佳 {2*SIZE/best/1e9:.1f} GB/s  中位 {2*SIZE/sorted(ts)[len(ts)//2]/1e9:.1f} GB/s")
print(f"（V100-SXM2-16GB HBM2 理论 900 GB/s）")
