#!/usr/bin/env python3
"""负载下的节流原因与电压诊断。

跑寄存器常驻 mma.sync 持续负载，期间采样：
  - SM 时钟 / 功耗 / 温度
  - clocks_throttle_reasons.*（到底是哪个限制在起作用）
  - 图形电压（nvidia-smi -q -d VOLTAGE）
并在负载持续期间抓一份完整 -q 快照。

用法: python3 diag_throttle.py [pci] [秒数]
"""
import ctypes as C
import subprocess
import sys
import threading
import time
from collections import Counter

PCI = sys.argv[1] if len(sys.argv) > 1 else "0000:04:00.0"
SECS = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0
SNAP = "/path/to/v100-vbios-research/verify-tools/load_snapshot.txt"

REASON_FIELDS = [
    "clocks_throttle_reasons.active",
    "clocks_throttle_reasons.gpu_idle",
    "clocks_throttle_reasons.sw_power_cap",
    "clocks_throttle_reasons.hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown",
    "clocks_throttle_reasons.sw_thermal_slowdown",
    "clocks_throttle_reasons.hw_power_brake_slowdown",
    "clocks_throttle_reasons.sync_boost",
]

CU = C.CDLL("libcuda.so.1")
CU.cuInit.argtypes = [C.c_uint]
CU.cuDeviceGetByPCIBusId.argtypes = [C.POINTER(C.c_int), C.c_char_p]
CU.cuDevicePrimaryCtxRetain.argtypes = [C.POINTER(C.c_void_p), C.c_int]
CU.cuCtxSetCurrent.argtypes = [C.c_void_p]
CU.cuMemAlloc_v2.argtypes = [C.POINTER(C.c_uint64), C.c_size_t]
CU.cuMemcpyHtoD_v2.argtypes = [C.c_uint64, C.c_void_p, C.c_size_t]
CU.cuModuleLoadDataEx.argtypes = [C.POINTER(C.c_void_p), C.c_char_p, C.c_uint, C.c_void_p, C.c_void_p]
CU.cuModuleGetFunction.argtypes = [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p]
CU.cuLaunchKernel.argtypes = [C.c_void_p] + [C.c_uint] * 6 + [C.c_uint, C.c_void_p, C.c_void_p, C.c_void_p]
CU.cuCtxSynchronize.argtypes = []

CHAINS = 8
MMA = "mma.sync.aligned.m8n8k4.row.col.f16.f16.f16.f16"
chain = lambda i: "{" + ",".join(f"%c{i*4+j}" for j in range(4)) + "}"
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


def run(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout


def voltage():
    out = run(["nvidia-smi", "-q", "-d", "VOLTAGE", "-i", PCI])
    for line in out.splitlines():
        if "Graphics" in line and "mV" in line:
            try:
                return float(line.split(":")[1].strip().split()[0])
            except Exception:
                pass
    return None


def main():
    CU.cuInit(0)
    dev = C.c_int()
    if CU.cuDeviceGetByPCIBusId(C.byref(dev), PCI.encode()):
        sys.exit("找不到设备 " + PCI)
    ctx = C.c_void_p()
    CU.cuDevicePrimaryCtxRetain(C.byref(ctx), dev)
    CU.cuCtxSetCurrent(ctx)
    mod, log = C.c_void_p(), C.create_string_buffer(16384)
    opts = (C.c_int * 3)(5, 6, 8)
    vals = (C.c_void_p * 3)(C.cast(log, C.c_void_p), C.c_void_p(16384), C.c_void_p(1))
    if CU.cuModuleLoadDataEx(C.byref(mod), PTX.encode(), 3, opts, vals):
        sys.exit("JIT 失败:\n" + log.value.decode(errors="replace"))
    fn = C.c_void_p()
    CU.cuModuleGetFunction(C.byref(fn), mod, b"mma_stress")
    pD = C.c_uint64()
    CU.cuMemAlloc_v2(C.byref(pD), 4096)
    zero = (C.c_uint32 * 1024)(*([0] * 1024))

    def launch(iters):
        CU.cuMemcpyHtoD_v2(pD, zero, 4096)
        n = C.c_uint32(iters)
        pp, pn = C.pointer(pD), C.pointer(n)
        a = (C.c_void_p * 2)(C.cast(pp, C.c_void_p), C.cast(pn, C.c_void_p))
        CU.cuLaunchKernel(fn, 1280, 1, 1, 256, 1, 1, 0, None, C.cast(a, C.c_void_p), None)
        CU.cuCtxSynchronize()

    t0 = time.time()
    launch(200000)
    per = (time.time() - t0) / 200000
    iters = max(200000, int(SECS / per))
    print(f"校准 {per*1e6:.2f} µs/迭代 → {iters} 次")

    base = run(["nvidia-smi", "--query-gpu=vbios_version,power.limit,power.max_limit,"
                "clocks.max.sm,clocks.applications.graphics", "--format=csv,noheader", "-i", PCI]).strip()
    print(f"压测前: {base}")

    samples, stop = [], threading.Event()

    def sampler():
        first = True
        while not stop.is_set():
            try:
                o = run(["nvidia-smi", "--query-gpu=clocks.sm,power.draw,temperature.gpu,"
                         + ",".join(REASON_FIELDS), "--format=csv,noheader,nounits", "-i", PCI])
                p = [x.strip() for x in o.strip().split(",")]
                samples.append((p[0], p[1], p[2], p[3:]))
                if first:                      # 负载刚开始时抓快照
                    with open(SNAP, "w") as f:
                        f.write(run(["nvidia-smi", "-q", "-d", "POWER,CLOCK,PERFORMANCE", "-i", PCI]))
                        f.write("\n" + run(["nvidia-smi", "-q", "-d", "VOLTAGE", "-i", PCI]))
                    first = False
            except Exception:
                pass
            time.sleep(0.3)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    time.sleep(1.5)
    v_under_load_before = voltage()
    t0 = time.time()
    launch(iters)
    dur = time.time() - t0
    stop.set()
    th.join(timeout=3)

    def col(i):
        out = []
        for s in samples:
            try:
                out.append(float(s[i]))
            except ValueError:
                pass
        return out

    clk, pwr, tmp = col(0), col(1), col(2)
    print(f"\n负载 {dur:.1f}s，{len(samples)} 采样")
    for nm, a, u in (("SM 时钟", clk, "MHz"), ("功耗", pwr, "W"), ("温度", tmp, "°C")):
        if a:
            print(f"  {nm}: 峰 {max(a):.0f}{u}  中位 {sorted(a)[len(a)//2]:.0f}{u}")
    print(f"  电压（负载早期读一次）: {v_under_load_before} mV")

    print("\n节流原因统计（活跃样本数 / 总样本）:")
    cnt = Counter()
    names = [f.replace("clocks_throttle_reasons.", "") for f in REASON_FIELDS[1:]]
    active_any = 0
    for s in samples:
        reasons = s[3]
        hit = False
        for nm, v in zip(names, reasons):
            if v.lower().startswith("active"):
                cnt[nm] += 1
                hit = True
        if hit:
            active_any += 1
    if not cnt:
        print("  无任何节流原因被激活")
    for nm, c in cnt.most_common():
        print(f"  {nm:26s} {c}/{len(samples)}")
    print(f"\n快照已写入 {SNAP}")


if __name__ == "__main__":
    main()
