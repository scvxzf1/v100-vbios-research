# tools/ —— 核心工具

| 工具 | 作用 |
|---|---|
| `derive_tables.py` | **核心**。解析 VBIOS 的 BIT 指针链，动态定位功耗表、P-State 表、VFE 表等所有表与字段偏移。不依赖硬编码偏移，换固件版本可自动重定位 |
| `build_payload.py` | 在原始镜像上生成修改后的 payload（改功耗与频率字段，并重算镜像校验和） |
| `stress_clock.py` | 持续张量核心负载，采样 SM 时钟 / 功耗 / 温度 |
| `mem_bw.py` | 显存带宽实测（`cuMemcpyDtoD`）。**判断显存时钟改动真伪的唯一可靠判据** |
| `diag_throttle.py` | 负载 + 节流原因 + 电压诊断 |
| `mma_probe.py` | 张量核心算力（TF/s）。注意已改为按 PCI 地址定位设备，避免多卡时取错设备 |

## 使用

```bash
# 查看固件结构（推荐第一步）
python3 tools/derive_tables.py your_vbios.rom

# 生成 payload
python3 tools/build_payload.py your_vbios.rom

# 压测：参数为 PCI 地址与时长（秒）
python3 tools/stress_clock.py 0000:04:00.0 30

# 显存带宽
python3 tools/mem_bw.py 0000:04:00.0
```

## 依赖

* Python 3（标准库即可，`derive_tables.py` / `build_payload.py` 无需额外包）
* CUDA 驱动 + `libcuda.so`（压测与带宽工具）
* `nvidia-smi`

压测工具用 **CUDA Driver API + 内联 PTX**，不需要 nvcc、cuBLAS 或 PyTorch。
