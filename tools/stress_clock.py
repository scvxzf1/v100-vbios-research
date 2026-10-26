#!/usr/bin/env python3
"""持续负载 + 时钟/功耗/温度采样。

用 CUDA driver API + PTX JIT 跑寄存器常驻的 mma.sync 循环（Volta tensor core），
不依赖 torch / nvcc。压测期间后台采样 nvidia-smi，输出实际达到的最高 SM 时钟、
功耗与温度。

用法:
    python3 stress_clock.py [pci地址] [秒数]
    python3 stress_clock.py 0000:04:00.0 20
"""
import ctypes as C
import subprocess
import sys
import threading
import time

PCI = sys.argv[1] if len(sys.argv) > 1 else "0000:04:00.0"
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
SMI_INDEX = PCI          # 用 PCI 地址定位，避免索引变化

CU = C.CDLL("libcuda.so.1")
CU.cuInit.argtypes = [C.c_uint]
CU.cuDeviceGetByPCIBusId.argtypes = [C.POINTER(C.c_int), C.c_char_p]
CU.cuDevicePrimaryCtxRetain.argtypes = [C.POINTER(C.c_void_p), C.c_int]
CU.cuCtxSetCurrent.argtypes = [C.c_void_p]
CU.cuMemAlloc_v2.argtypes = [C.POINTER(C.c_uint64), C.c_size_t]
CU.cuMemcpyHtoD_v2.argtypes = [C.c_uint64, C.c_void_p, C.c_size_t]
CU.cuMemcpyDtoH_v2.argtypes = [C.c_void_p, C.c_uint64, C.c_size_t]
CU.cuModuleLoadDataEx.argtypes = [C.POINTER(C.c_void_p), C.c_char_p, C.c_uint, C.c_void_p, C.c_void_p]
CU.cuModuleGetFunction.argtypes = [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p]
CU.cuLaunchKernel.argtypes = [C.c_void_p] + [C.c_uint] * 6 + [C.c_uint, C.c_void_p, C.c_void_p, C.c_void_p]
CU.cuCtxSynchronize.argtypes = []

CHAINS = 8               # 独立依赖链数量：越多 ILP 越高、功耗越大
MMA = "mma.sync.aligned.m8n8k4.row.col.f16.f16.f16.f16"


def chain(i):
    return "{" + ",".join(f"%c{i*4+j}" for j in range(4)) + "}"


BODY = "\n  ".join(
    f"{MMA} {chain(i)}, {{%a0,%a1}}, {{%b0,%b1}}, {chain(i)};" for i in range(CHAINS)
)

PTX = f"""
.version 7.0
.target sm_70
.address_size 64

.visible .entry mma_stress(.param .u64 pD, .param .u32 N)
{{
  .reg .b32 %a<2>; .reg .b32 %b<2>; .reg .b32 %c<{CHAINS*4}>;
  .reg .b64 %rd<2>; .reg .u32 %n; .reg .pred %p;
  ld.param.u64 %rd0, [pD];
  mov.b32 %a0, 0x3c003c00; mov.b32 %a1, 0x3c003c00;
  mov.b32 %b0, 0x3c003c00; mov.b32 %b1, 0x3c003c00;
  {" ".join(f"mov.b32 %c{i}, 0;" for i in range(CHAINS*4))}
  ld.param.u32 %n, [N];
LOOP:
  {BODY}
  sub.u32 %n, %n, 1;
  setp.ne.u32 %p, %n, 0;
  @%p bra LOOP;
  st.global.b32 [%rd0], %c0;
  ret;
}}
"""


def smi(fields):
    out = subprocess.run(
        ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits", "-i", SMI_INDEX],
        capture_output=True, text=True, timeout=10,
    )
    return [x.strip() for x in out.stdout.strip().split(",")]


def main():
    CU.cuInit(0)
    dev = C.c_int()
    rc = CU.cuDeviceGetByPCIBusId(C.byref(dev), PCI.encode())
    if rc:
        sys.exit(f"cuDeviceGetByPCIBusId({PCI}) 失败 rc={rc}")
    ctx = C.c_void_p()
    CU.cuDevicePrimaryCtxRetain(C.byref(ctx), dev)
    CU.cuCtxSetCurrent(ctx)

    mod = C.c_void_p()
    log = C.create_string_buffer(16384)
    opts = (C.c_int * 3)(5, 6, 8)          # ERROR_LOG_BUFFER, SIZE, TARGET_FROM_CUCONTEXT
    vals = (C.c_void_p * 3)(C.cast(log, C.c_void_p), C.c_void_p(16384), C.c_void_p(1))
    if CU.cuModuleLoadDataEx(C.byref(mod), PTX.encode(), 3, opts, vals):
        sys.exit("PTX JIT 失败:\n" + log.value.decode(errors="replace"))

    fn = C.c_void_p()
    CU.cuModuleGetFunction(C.byref(fn), mod, b"mma_stress")
    pD = C.c_uint64()
    CU.cuMemAlloc_v2(C.byref(pD), 4096)
    zero = (C.c_uint32 * 1024)(*([0] * 1024))
    blocks, threads = 1280, 256         # 1280*256/32 = 10240 warps

    def launch(iters):
        CU.cuMemcpyHtoD_v2(pD, zero, 4096)
        n = C.c_uint32(iters)
        pp, pn = C.pointer(pD), C.pointer(n)
        args = (C.c_void_p * 2)(C.cast(pp, C.c_void_p), C.cast(pn, C.c_void_p))
        CU.cuLaunchKernel(fn, blocks, 1, 1, threads, 1, 1, 0, None,
                          C.cast(args, C.c_void_p), None)
        CU.cuCtxSynchronize()

    # --- 口径校准：先跑一次小规模，测每次迭代耗时 ---
    t0 = time.time()
    launch(200000)                      # 足够长以摊薄启动开销
    per_iter = (time.time() - t0) / 200000
    iters = max(200000, int(SECS / per_iter))
    est = iters * per_iter
    print(f"校准: 每迭代 {per_iter*1e6:.1f} µs -> 用 {iters} 次迭代, 预计 {est:.1f} s")

    samples = []
    stop = threading.Event()

    def sampler():
        while not stop.is_set():
            try:
                c, p, t = smi("clocks.sm,power.draw,temperature.gpu")
                samples.append((time.time(), c, p, t))
            except Exception:
                pass
            time.sleep(0.25)

    baseline = smi("vbios_version,power.limit,power.max_limit,clocks.max.sm")
    print(f"压测前: vbios={baseline[0]} limit={baseline[1]}W max={baseline[2]}W max_sm={baseline[3]}MHz")

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    t0 = time.time()
    launch(iters)
    dur = time.time() - t0
    stop.set()
    th.join(timeout=3)

    def nums(idx):
        out = []
        for s in samples:
            v = s[idx]
            if v not in ("N/A", "[N/A]", ""):
                try:
                    out.append(float(v))
                except ValueError:
                    pass
        return out

    clk, pwr, tmp = nums(1), nums(2), nums(3)

    def stat(name, a, unit):
        if not a:
            print(f"  {name}: 无样本")
            return
        a_sorted = sorted(a)
        med = a_sorted[len(a_sorted) // 2]
        print(f"  {name}: 最大 {max(a):.0f}{unit}  中位 {med:.0f}{unit}  最小 {min(a):.0f}{unit}  ({len(a)} 样本)")

    print(f"\n压测结束，实际耗时 {dur:.1f} s，采样 {len(samples)} 点")
    stat("SM 时钟", clk, " MHz")
    stat("功耗", pwr, " W")
    stat("温度", tmp, " °C")

    # 结论判定
    limit = float(baseline[1].split()[0]) if baseline[1][0].isdigit() else 0
    if clk:
        peak = max(clk)
        print(f"\n判定: 负载下 SM 时钟峰值 {peak:.0f} MHz"
              f"（VBIOS 上报上限 {baseline[3].split()[0]} MHz）")
        print(f"      功耗峰值 {max(pwr):.0f} W（当前功耗上限 {baseline[1]}）" if pwr else "")


if __name__ == "__main__":
    main()
