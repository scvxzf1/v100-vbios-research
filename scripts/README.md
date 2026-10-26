# scripts/ —— 实验脚本存档

按实验阶段分类，保留原始形态以便复现。**这些是研究过程中的一次性脚本**，
路径与设备号可能写死在当前机器上，使用前请按自身环境调整。

| 目录 | 内容 |
|---|---|
| `extract/` | 从驱动侧提取 VBIOS（debugfs / `nvidia-smi --gpu-rom-dump` / nvflash） |
| `build/` | 各轮实验的 payload 构建脚本（功耗、时钟、显存、P38、刷写镜像） |
| `probe/` | 批量变体探测脚本，每个变体走一次完整的 PCI 摘卡→注入→rescan 流程 |
| `apply/` | 应用 / 回退 payload |
| `preflight/` | 物理刷写前的非破坏性预检 |

## 重要提示

`extract_vbios.sh`、`load_vbios_safe.sh`、`extract_full_vbios.py` 三个脚本原本内嵌了
明文 root 密码，**开源版已改为从环境变量读取**：

```bash
export ROOT_PASSWORD="你的root密码"
```

`probe_*.sh` 会卸载内核驱动，**会导致图形会话中断**。原脚本通过
`systemd-run --no-block` 提交为独立单元，跑完自动恢复桌面。若在 SSH 会话中直接运行，
连接会断开且脚本可能被中断。
