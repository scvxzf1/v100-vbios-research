# firmware/ —— 固件镜像存档

> ⚠️ **本目录内容不属于 Apache-2.0**，版权归 NVIDIA 所有，且**内含原始显卡序列号**。
> 详见项目根目录 [`../NOTICE`](../NOTICE) §一、§二（其中说明：`docs/evidence/gpu_state_dump.txt`
> 同样含本卡序列号与 GPU UUID）。

| 文件 | 大小 | sha256（完整值） | 说明 |
|---|---|---|---|
| `evidence_chip_erased_readback.bin` | 1,048,576 B | `f5fb04aa5b882706b9309e885f19477261336ef76a150c3b4d3489dfac3953ec` | 事故证据：编程器写入失败后读回，**全 0xFF（芯片空白）** |
| `v100_image_350w_1m.bin` | 1,048,576 B | `492394eb611e6fb789101dea15004e870f2d4f745957e304a1178eaf705b01d9` | 修改版完整镜像：350 W + XBAR 1912 + GPC 1800，仅改 20 字节；**物理写入未成功** |
| `v100_original_1m.bin` | 1,048,576 B | `4dd19612fcf2685796a9c8d779fcd23af000eebd04076a28649f42324c3ca439` | 编程器读取的完整 1 MiB flash 原厂备份（**唯一完整备份**） |
| `v100_original_nvflash.rom` | 330,240 B | `88ab94ac77beb8301a49e94a183cba7d481e49d43d992712171a06b25f16361b` | nvflash `--save` 得到的原厂镜像（不含 0xA00 之后的空白区，**不完整**） |
| `v100_payload_350w.bin` | 327,680 B | `8163a67d23ce4de1a2feca5e72ef5b38039d1fa848a7dc08518116f6c26c8096` | 运行时注入用 payload：功耗 350 W + 频率 1912/1800 |
| `v100_payload_350w_only.bin` | 327,680 B | `a8483ca8f5bdbdb91cd38bc317f5e3dd239e7bf25563e2809a72f67b91794a6a` | 运行时注入用 payload：仅功耗 350 W，频率保持原厂 |

校验方式（在本目录内执行）：

```bash
sha256sum *.bin *.rom        # 与上表逐行比对
```

`deploy/MANIFEST.sha256` 另外覆盖 `v100_payload_350w.bin`（部署到 `/etc/v100/v100_final_payload.bin`），可用
`cd ../deploy && sha256sum -c MANIFEST.sha256` 一次校验。

## 可直接比对的校验块（与上表一致）

```
f5fb04aa5b882706b9309e885f19477261336ef76a150c3b4d3489dfac3953ec  evidence_chip_erased_readback.bin
492394eb611e6fb789101dea15004e870f2d4f745957e304a1178eaf705b01d9  v100_image_350w_1m.bin
4dd19612fcf2685796a9c8d779fcd23af000eebd04076a28649f42324c3ca439  v100_original_1m.bin
88ab94ac77beb8301a49e94a183cba7d481e49d43d992712171a06b25f16361b  v100_original_nvflash.rom
8163a67d23ce4de1a2feca5e72ef5b38039d1fa848a7dc08518116f6c26c8096  v100_payload_350w.bin
a8483ca8f5bdbdb91cd38bc317f5e3dd239e7bf25563e2809a72f67b91794a6a  v100_payload_350w_only.bin
```

## 如何不使用本目录的固件

本项目所有功能都可以**从你自己的显卡现场生成 payload**，无需依赖上面的镜像：

```bash
# 1. 用 nvflash 从你的卡读取原厂固件（需卸载驱动）
#    本项目未收录 nvflash（NVIDIA 专有工具），请自行从官方渠道获取并加入 PATH，见 NOTICE
sudo nvflash -i 1 --save my_v100.rom

# 2. 生成 payload（自动定位功耗表与 P-State 表，不依赖硬编码偏移）
python3 tools/build_payload.py my_v100.rom

# 3. 按 ../docs/05-注入点与初始化.md 的流程注入
```

这样既避免分发受版权保护的固件，也不会泄露任何设备标识符。
