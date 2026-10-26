#!/usr/bin/env python3
"""Register-resident Volta tensor-core issue-rate probe (CUDA driver API, PTX JIT).

mma.sync.aligned.m8n8k4 = the Volta (GV100) tensor-core instruction.
Operands are held in registers; no memory traffic inside the timed loop.
Reported rate is instruction throughput, not a GEMM or application benchmark.
"""
import ctypes as C, sys, time

CU = C.CDLL("libcuda.so.1")
CU.cuInit.argtypes=[C.c_uint]
CU.cuDeviceGet.argtypes=[C.POINTER(C.c_int),C.c_int]
CU.cuDevicePrimaryCtxRetain.argtypes=[C.POINTER(C.c_void_p),C.c_int]
CU.cuCtxSetCurrent.argtypes=[C.c_void_p]
CU.cuMemAlloc_v2.argtypes=[C.POINTER(C.c_uint64),C.c_size_t]
CU.cuMemcpyHtoD_v2.argtypes=[C.c_uint64,C.c_void_p,C.c_size_t]
CU.cuMemcpyDtoH_v2.argtypes=[C.c_void_p,C.c_uint64,C.c_size_t]
CU.cuModuleLoadDataEx.argtypes=[C.POINTER(C.c_void_p),C.c_char_p,C.c_uint,C.c_void_p,C.c_void_p]
CU.cuModuleGetFunction.argtypes=[C.POINTER(C.c_void_p),C.c_void_p,C.c_char_p]
CU.cuLaunchKernel.argtypes=[C.c_void_p]+[C.c_uint]*6+[C.c_uint,C.c_void_p,C.c_void_p,C.c_void_p]
CU.cuCtxSynchronize.argtypes=[]

CHAINS = 4
def chain(i): return "{"+",".join(f"%c{i*4+j}" for j in range(4))+"}"
MMA = "mma.sync.aligned.m8n8k4.row.col.f16.f16.f16.f16"
PTX = f"""
.version 7.0
.target sm_70
.address_size 64

.visible .entry mma_f16acc(.param .u64 pD, .param .u32 N)
{{
  .reg .b32 %a<2>; .reg .b32 %b<2>; .reg .b32 %c<{CHAINS*4}>;
  .reg .b64 %rd<1>; .reg .u32 %n; .reg .pred %p;
  ld.param.u64 %rd0, [pD];
  mov.b32 %a0, 0x3c003c00; mov.b32 %a1, 0x3c003c00;
  mov.b32 %b0, 0x3c003c00; mov.b32 %b1, 0x3c003c00;
  {" ".join(f"mov.b32 %c{i}, 0;" for i in range(CHAINS*4))}
  ld.param.u32 %n, [N];
LOOP:
  {"".join(f"{MMA} {chain(i)}, {{%a0,%a1}}, {{%b0,%b1}}, {chain(i)};" + chr(10) + "  " for i in range(CHAINS))}
  sub.u32 %n, %n, 1;
  setp.ne.u32 %p, %n, 0;
  @%p bra LOOP;
  st.global.b32 [%rd0], %c0;
  ret;
}}
"""

def main():
    pci = sys.argv[1] if len(sys.argv) > 1 else "0000:04:00.0"
    CU.cuInit(0)
    CU.cuDeviceGetByPCIBusId.argtypes=[C.POINTER(C.c_int), C.c_char_p]
    dev = C.c_int()
    rc = CU.cuDeviceGetByPCIBusId(C.byref(dev), pci.encode())
    if rc: sys.exit(f"device {pci} not found rc={rc}")
    ctx = C.c_void_p(); CU.cuDevicePrimaryCtxRetain(C.byref(ctx), dev); CU.cuCtxSetCurrent(ctx)
    mod = C.c_void_p(); log = C.create_string_buffer(16384)
    opts = (C.c_int*3)(5,6,8)   # ERROR_LOG_BUFFER, SIZE, TARGET_FROM_CUCONTEXT
    vals = (C.c_void_p*3)(C.cast(log, C.c_void_p), C.c_void_p(16384), C.c_void_p(1))
    rc = CU.cuModuleLoadDataEx(C.byref(mod), PTX.encode(), 3, opts, vals)
    if rc:
        print("JIT failed:\n" + log.value.decode(errors="replace")); sys.exit(1)
    fn = C.c_void_p(); CU.cuModuleGetFunction(C.byref(fn), mod, b"mma_f16acc")

    pD = C.c_uint64(); CU.cuMemAlloc_v2(C.byref(pD), 4096)
    zero = (C.c_uint32*1024)(*([0]*1024))
    blocks, threads = 1280, 256
    warps = blocks*threads//32
    OPS = 8*8*4*2            # one m8n8k4 mma = 256 MACs = 512 ops per warp

    def launch(iters):
        CU.cuMemcpyHtoD_v2(pD, zero, 4096)
        n = C.c_uint32(iters)
        pp = C.pointer(pD); pn = C.pointer(n)
        args = (C.c_void_p*2)(C.cast(pp, C.c_void_p), C.cast(pn, C.c_void_p))
        CU.cuLaunchKernel(fn, blocks,1,1, threads,1,1, 0, None, C.cast(args, C.c_void_p), None)
        CU.cuCtxSynchronize()

    launch(1)
    out = (C.c_uint32*1)()
    CU.cuMemcpyDtoH_v2(out, pD, 4)
    import struct
    val = struct.unpack("<e", struct.pack("<H", out[0] & 0xffff))[0]
    print(f"numeric check (a=b=1, 1 mma): element = {val}  expected 4.0")

    import subprocess, threading
    samples = []
    stop = threading.Event()
    def sampler():
        while not stop.is_set():
            r = subprocess.run(["nvidia-smi","-i","1","--query-gpu=clocks.sm,power.draw,temperature.gpu,utilization.gpu",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True)
            try:
                c,pw,t,u = [x.strip() for x in r.stdout.strip().split(",")]
                samples.append((float(c), float(pw), float(t), float(u)))
            except Exception:
                pass
            stop.wait(0.15)

    ITERS = 400000
    launch(5000)                      # warm-up / clock ramp
    th = threading.Thread(target=sampler, daemon=True); th.start()
    best = None
    for _ in range(10):
        t0 = time.perf_counter(); launch(ITERS); dt = time.perf_counter()-t0
        best = dt if best is None else min(best, dt)
    stop.set(); th.join(timeout=1)

    tflops = warps*ITERS*CHAINS*OPS/best/1e12
    clocks = [s[0] for s in samples if s[3] > 90]
    pw = [s[1] for s in samples if s[3] > 90]
    print(f"iters/launch={ITERS}  best={best*1e3:.1f} ms  ->  {tflops:.1f} TF/s (FP16 tensor, f16 accumulate)")
    if clocks:
        clk = max(clocks)
        print(f"busy samples={len(clocks)}  SM clock max={clk:.0f} MHz  power max={max(pw):.1f} W")
        sms = 80
        print(f"derived ops/clk/SM = {tflops*1e12/(sms*clk*1e6):.0f}   (V100 spec ~1024)")
    else:
        print("no busy telemetry samples collected")

if __name__ == "__main__":
    main()
