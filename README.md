# Tesla V100 VBIOS 研究：突破功耗墙与频率墙

> 在**不修改物理 flash、不增加一毫伏电压**的前提下，把 Tesla V100-SXM2-16GB 的
> 功耗上限从 300 W 提到 350 W、核心频率从 1530 MHz 提到 1785 MHz（实测算力 +15.5%），
> 并系统性地验证了显存频率、A0、P38、VFE 钳位等其余所有可疑字段——**多数是惰性的**。

---

## English Summary

This repository documents a black-box investigation of the Tesla V100-SXM2-16GB
(GV100) VBIOS. Instead of flashing the EEPROM, we override the firmware image that
the proprietary NVIDIA driver consumes at adapter-init time, via the driver's own
`/proc/driver/nvidia/registry` binary-key interface.

**Results:** power limit 300 W → **350 W**, SM clock ceiling 1530 MHz → **1785 MHz**
(+15.5% measured tensor-core throughput, +84% power), with **zero bytes written to
flash** and **zero increase in voltage**. Memory clock, the "A0" offset, the "P38"
target-clock field and the 1912 MHz VFE clamp were all empirically proven to be
either inert or reporting-only.

The real ceiling of 1785 MHz is the silicon's **voltage/VID limit** — not a policy
limit — so it cannot be exceeded without overvolting. Everything here is reversible by
`rmmod`, and includes a hardened boot-time systemd unit for persistence.

---

## 一、结论速览

| 项目 | 原厂 | 修改后 | 实测效果 |
|---|---|---|---|
| 功耗上限 | 300 W | **350 W** | 高负载余量 +50 W |
| 核心频率上限（上报） | 1530 MHz | **1912 MHz** | — |
| 核心频率（实际负载） | 1530 MHz | **1785 MHz** | **算力 +15.5%** |
| Applications Clocks | 1312 MHz | 1800 MHz | 支持更高基准锁频 |
| 显存频率 | 877 MHz | 877 MHz | **不可改**（仅上报值可改） |
| 电压 | 出厂 | 出厂 | **未增加** |
| flash 写入 | — | **0 字节** | 零变砖风险 |

**能效代价**：1785 MHz 时 `0.525 TF·s⁻¹·W⁻¹`，1530 MHz 时 `0.835 TF·s⁻¹·W⁻¹`。
性能 +15.5%，功耗 +84%（139 → 255 W）。多卡或电源受限场景应优先选 1530。

---

## 二、六个关键发现

**1. `1530 MHz` 是策略上限，`1785 MHz` 才是物理天花板**

VBIOS 的 P-State 表把上报上限压到 1530，但硅片在**出厂电压**下实际能跑到 1785。
修改只涉及表格数据，不涉及任何电压设定。

**2. 真正的频率杠杆是 `XBAR` 字段，不是 `A0`**

上游研究认为 `Max Clocks = 1912 + A0`。本卡实测**证伪**：A0 加 +100/+200/+300 全部无反应，
而修改 `0x31597` 的 XBAR 字段直接改变实际频率。不同固件版本行为不同，**不能照抄偏移和结论**。

**3. 显存频率无法修改，只有上报值可以**

把数据区与代码区两处字段**同时**改成 600 MHz 后，驱动确实上报 600、`Supported Clocks`
也列出 600——但 `clocks.current.memory` 恒为 877，实测带宽恒为 805–809 GB/s。
数学反证：600 MHz 的 HBM2 理论带宽上限仅 614 GB/s，实测 809 已超出，**硬件从未改变**。
原因是 HBM2 的 PLL 在 POST 阶段就按 strap 锁死，而注入发生在 POST 之后。

**4. `1912` 这个钳位来自 VFE 表的第 14 号槽**

VFE 是一张分段函数表（`[域下界, 域上界, 值, 系数a, 系数b]`），`0x322E7` 处的
f32 `1912.0` 就是钳位本体。抬高它只能改上报，改不了实际频率。

**5. `P38` 字段是惰性的（含反向验证）**

上游称 `0x38EA9`/`0x391FB` 为 "target clock"，本卡原值恰为 1530（等于原厂 Max Clocks）。
四向测试全无效——**把它压到 1200，时钟照跑 1777，完全没被限住**。若它是上限字段必然生效，
故确认既非上限也非目标值。

**6. `Image0`（代码段）的改动会被硬件真正采纳**

曾假设"只有数据镜像有效、代码段动不得"。实测反证：把 Image0 `0x7E72` 由 877 改为 600，
`clocks.sm` 真的降到 600 MHz、带宽随之掉到 488.8 GB/s。这条对后续任何 Image0 方向的尝试都适用。

---

## 三、工作原理

驱动没有打补丁、驱动本身没有修改、flash 一个字节都没写。用的是 **RM（NVIDIA 内核态资源管理器）
自带的固件覆盖机制**——它在初始化 adapter 时先查一个名为 `vbios` 的二进制 registry 键，
若存在就用它替代从 flash 读到的 VBIOS。

```
① 驱动自带通道    RM 优先读取名为 "vbios" 的 registry 二进制键
② 写入接口        /proc/driver/nvidia/registry        格式: vbios=<裸字节>
③ payload 格式    rom[0x0A00:0x50A00]，327,680 字节（剔除 0xA00 NVGIO 前缀）
④ 生效时机        每个模块生命周期只在第一次 adapter 初始化时读一次
⑤ 镜像校验        每个镜像 additive checksum 必须归零（改了就重算）
⑥ 字段定位        从 NPDS 的 BIT 指针动态解析，不硬编码偏移
```

**④ 是这套方案的关键**：必须把 GPU 从 PCI 总线上摘掉，在它"上线之前"把数据写好。

```bash
rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia        # ① 清空模块级 registry
echo 1 > /sys/bus/pci/devices/0000:04:00.0/remove        # ② 显卡下线
modprobe nvidia                                          # ③ 只 probe 显示卡
{ printf 'vbios='; cat payload.bin; } > /proc/driver/nvidia/registry   # ④ 写入
echo 1 > /sys/bus/pci/rescan                             # ⑤ 显卡上线 → 读到覆盖
```

约 30 秒一轮，**不用重启**。覆盖只活在 `nvidia.ko` 的模块级 registry 表里，
`rmmod` 或重启即消失——所以物理 flash 始终是原厂内容。

### 改了什么

总共 **20 个字节**，全部落在 Image3（NPDS 数据镜像）：

| 偏移 | 字段 | 原值 | 新值 |
|---|---|---|---|
| `0x31523` | ENTRY0_Q | 382 | 478 |
| `0x3156E` | GPC_HDR 频率/比率 | 1312/328 | 1800/450 |
| `0x315C0` | GPC_R1 频率/比率 | 1312/328 | 1800/450 |
| `0x31597` | XBAR 频率/比率 | `0x85FA`/382 | `0x8778`/478 |
| `0x315E9` | SYS 频率/比率 | `0x85FA`/382 | `0x8778`/478 |
| `0x3348F` | 功耗 row4 rated | 300000 | 350000 |
| `0x33493` | 功耗 row4 max | 300000 | 350000 |
| `0x3E7FF` | Image3 校验和 | 126 | 108 |

---

## 四、目录结构

```
.
├── docs/                     研究文档（建议按序号阅读）
│   ├── 01-总览与结论.md         ← 从这份开始
│   ├── 02-功耗墙实测.md         注入点细节、payload 格式、8 条失败做法
│   ├── 03-时钟墙实测.md         三轮 14 变体实测、瓶颈诊断、VFE 结构解构
│   ├── 04-显存频率实测.md       两轮 9 变体、current/max 分化、POST 锁定机制
│   ├── 05-注入点与初始化.md     驱动侧静态分析证据
│   ├── 06-刷写尝试与教训.md     ⚠️ 物理刷写失败事故与四条教训
│   ├── 07-算力锁验证.md
│   ├── 99-早期研究与证伪.md     早期结论，多数已被推翻
│   └── evidence/               实测日志与现场快照
├── tools/                    核心工具（结构解析 / 构建 / 压测）
├── deploy/                   开机自动注入补丁（systemd + 安装脚本）
├── firmware/                 固件镜像存档（⚠️ 非 Apache-2.0，内含设备序列号）
└── scripts/                  实验脚本存档（按阶段分类）
```

---

## 五、快速开始

前置：Tesla V100（本卡为 SXM2-16GB，固件 `88.00.4F.00.09`）+ 较新的闭源驱动。
**请先读 `docs/01-总览与结论.md` 与 `docs/02-功耗墙实测.md`。**

```bash
# 1. 取你自己的原厂固件（需卸载驱动；V100 无 ROM BAR，只能用 nvflash）
sudo nvflash -i 1 --save my_v100.rom

# 2. 查看结构，确认表位置（不要跳过这步）
python3 tools/derive_tables.py my_v100.rom

# 3. 生成 payload
python3 tools/build_payload.py my_v100.rom

# 4. 按 docs/05-注入点与初始化.md 的流程注入
# 5. 验证
nvidia-smi --query-gpu=power.limit,clocks.max.sm --format=csv -i 1
```

**长期使用**建议装开机自动注入补丁：

```bash
cd deploy && sudo ./install.sh     # 幂等，含完整性校验
sudo ./uninstall.sh                # 随时卸载
```

---

## 六、实测数据

### 频率与算力（同一 VBIOS，仅改 `-lgc` 锁频）

| SM 锁定 | 算力 | 功耗 | 能效 |
|---|---|---|---|
| 1530 MHz | 115.9 TF/s | 138.8 W | 0.835 TF·s⁻¹·W⁻¹ |
| 1650 MHz | 124.6 TF/s | 175.6 W | 0.709 TF·s⁻¹·W⁻¹ |
| 1777 MHz | 133.9 TF/s | 255.3 W | 0.525 TF·s⁻¹·W⁻¹ |

频率 +16.1%，吞吐 +15.5%（约 96% 线性）。

### 瓶颈诊断

负载下：**263 W / 56 °C / 零节流原因 / 零 ECC 错误**。
`-lgc 1852,1852` 被接受但仍只跑到 1785（43 个采样）→ 确认是**电压/VID 限制**，
不是加速策略或散热问题。

### 显存

| 变体 | `current.memory` | `max.memory` | 实测带宽 |
|---|---|---|---|
| 原厂 | 877 | 877 | 804.7 GB/s |
| 数据区改 600 | 877 | 877 | 805.4 GB/s |
| 两处同改 600 | **877** | **600** | **809.3 GB/s** |

`current` 读硬件实测值，`max` 读表值——**只有后者会跟着改**，带宽恒定不变。

---

## 七、已知边界（做不到的事）

| 目标 | 结论 |
|---|---|
| 突破 1785 MHz | ❌ 需加压，本方案**不涉及电压**。这是出厂电压下的物理极限 |
| 修改显存频率 | ❌ 硬件 PLL 在 POST 阶段锁死，只能改上报值 |
| 软件控制电压 | ❌ `nvidia-smi -q -d VOLTAGE` 为空，NV-CONTROL 无电压属性 |
| `-lmc` 锁显存 | ❌ V100 上完全不被支持（连设成当前值都报错） |
| 平台侧优化 | ❌ 已 Gen3 x16、`iommu=pt`、单路全 NUMA0，无余量 |
| 物理刷写持久化 | ⚠️ 已尝试并失败（芯片被擦空），详见 `docs/06`。改用开机注入 |

**纯计算路径已实测达到 1777 MHz 理论峰值的 92%**，剩余空间在访存与数据管线
（张量核心利用率、cuBLAS 选型、pinned memory + 多流、batch size），属应用层。

---

## 八、安全与免责

⚠️ **本项目涉及卸载内核驱动、操作 PCI 总线等高风险动作。**

- 内存注入方案 flash 零写入，`rmmod` 或重启即回原厂
- `docs/06` 记录的**物理刷写尝试已导致一次芯片被完全擦空的真实事故**
- **执行任何步骤前，请确保已拥有完整、经过校验的原始固件备份**

作者不对显卡损坏、变砖、保修失效或任何连带损失承担责任。风险自负。

固件版权与设备标识说明见 [`NOTICE`](NOTICE)。

---

## 九、致谢

本项目起点是 [xmm2333/Tesla-v100-vbios-research](https://github.com/xmm2333/Tesla-v100-vbios-research)。
该项目提出的注入思路与 BIT 表结构分析极具价值。本仓库在其基础上：

- **证伪了部分结论**（A0 惰性、XBAR/SYS 才是真实杠杆、P38 惰性）
- 补充了显存频率、VFE 结构、功耗表 `tgp_index` 动态解析等完整实测
- 增加了开机自动注入、失败回退与事故记录

---

## 十、许可

代码与文档：[Apache License 2.0](LICENSE)。
`firmware/` 下的固件镜像**不受该许可证覆盖**，详见 [`NOTICE`](NOTICE)。
