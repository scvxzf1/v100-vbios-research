# V100 开机注入补丁 —— 部署备份

本目录是**系统内已部署配置的完整备份**，可在系统重装、配置丢失或迁移时一键重建。

原始记录见 [`../docs/01-总览与结论.md`](../docs/01-总览与结论.md) §六「开机自动生效」。

---

## 目录内容

| 文件 | 部署到 | 说明 |
|---|---|---|
| `v100-boot-patch.service` | `/etc/systemd/system/` | systemd 单元 |
| `v100-boot-patch.sh` | `/usr/local/sbin/` | 注入脚本 |
| `../firmware/v100_payload_350w.bin` | `/etc/v100/v100_final_payload.bin` | VBIOS 覆盖 payload，327,680 B |
| `install.sh` | — | 幂等安装（含完整性校验） |
| `uninstall.sh` | — | 卸载并回原厂行为 |
| `MANIFEST.sha256` | — | 备份文件校验清单 |

`install.sh` 会先跑 `sha256sum -c MANIFEST.sha256`，**任一文件被改动就拒绝安装**。若你确实修改了某个文件，重算清单：

```bash
cd deploy && sha256sum v100-boot-patch.service v100-boot-patch.sh \
  install.sh uninstall.sh ../firmware/v100_payload_350w.bin > MANIFEST.sha256
```

---

## 安装

```bash
cd /path/to/v100-vbios-research/deploy
sudo ./install.sh
```

## 卸载

```bash
sudo ./uninstall.sh
# 若当前内存覆盖仍在生效，需回原厂：
sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia && sudo modprobe nvidia
```

---

## 它做什么

目标配置：**功耗 350 W / XBAR 1912 MHz / Applications 1800 MHz**

> 功耗上限由环境变量 **`V100_EXPECT_W`** 决定，**缺省 350**（与参数化之前的写死行为逐字节等价）。
> TUI 为其他功耗的档案安装持久化时，会写入 drop-in
> `/etc/systemd/system/v100-boot-patch.service.d/10-v100-tui.conf` 覆盖此值，单元本身仍保持 350 缺省。
> 若 `deploy/v100-boot-patch.sh` 未包含 `V100_EXPECT_W`（旧版副本），则只支持 350 W 档案（TUI 会报 `E0504` 拒绝安装）。

```
等 V100 上线（最多 3 秒）
  → 若 lactd 在跑则停掉（它握住 /dev/nvidia* 会让驱动卸不掉）
  → 卸载 nvidia 驱动
  → 检查 /dev/nvidia* 无进程占用（否则放弃，见下）
  → 把 V100 从 PCI 总线摘下
  → modprobe nvidia（此时只 probe 显示卡）
  → 写入 vbios 覆盖到 /proc/driver/nvidia/registry
  → rescan 让 V100 上线并 probe → 读取覆盖
  → 验证 power.max_limit 是否为 350.00
  → 按需拉起 lactd
```

物理 flash **一个字节都不写**。覆盖只活在 `nvidia.ko` 的模块级 registry 表里，`rmmod` 或重启即消失。

---

## 安全设计

**任何环节失败都必须让系统以原厂状态正常启动，绝不阻塞开机。**

| 机制 | 作用 |
|---|---|
| `ExecCondition=/bin/test -e /sys/bus/pci/devices/0000:04:00.0` | **卡不存在时 systemd 直接跳过**，脚本不执行，实测 47–63 ms |
| `Before=display-manager.service gdm.service lactd.service` | 保证在任何实体抢住 GPU 之前完成注入 |
| 摘卡前 `fuser` 检查 `/dev/nvidia*` | 有占用则放弃注入。**这是唯一可能拖慢开机的路径**：进程持有设备时内核 `nv_pci_remove` 会阻塞在 `os_delay` 数分钟，且 D 状态无法被 `TimeoutStartSec` 中断 |
| `fail()` 统一兜底 | rescan 恢复设备 → 按需拉起 lactd → `exit 0` |
| `TimeoutStartSec=120` / `TimeoutStopSec=15` | 最坏情况限时 |
| 失败即回原厂 | 保持 300 W / 1530 MHz，功能不受影响 |

各场景实测行为：

| 场景 | 结果 | 开机影响 |
|---|---|---|
| 卡不存在 | `Result=exec-condition` | **47–63 ms，无影响** |
| 注入成功 | 350 W + 1912 | 约 +12–15 秒 |
| 某步失败 | 回原厂 | 不阻塞 |
| 设备被占用 | 放弃注入 | 不阻塞 |
| 脚本卡住 | 强杀 | 最多 120 秒 |

---

## 验证

```bash
# 开机后看日志（成功会有一行「成功: ...」）
cat /var/log/v100-boot-patch.log

# 直接查上报值
nvidia-smi --query-gpu=power.limit,power.max_limit,clocks.max.sm,clocks.applications.graphics \
  --format=csv -i 1
# 期望: 350.00 W, 350.00 W, 1912 MHz, 1800 MHz
```

---

## 注意事项

1. **不要同时使用内存注入与 flash 写入的配置**。若将来刷写了修改版 VBIOS，应卸载本补丁，否则二者叠加（效果相同但徒增复杂度）。
2. **LACT 的 `power_cap` 必须保持 `350.0`**（`/etc/lact/config.yaml`）。它为 V100 自动建过条目并把 350 改回 300，是唯一会静默抹掉成果的地方。
3. 本方案**不改电压**。核心频率实际上限 1785 MHz 是出厂电压下的物理极限，与补丁无关。
