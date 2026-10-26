# 变更记录

本文件记录仓库层面的重要变更。研究过程的逐分钟时间线见
[`docs/01-总览与结论.md`](docs/01-总览与结论.md) §十、[`docs/02-功耗墙实测.md`](docs/02-功耗墙实测.md) §11
与 [`docs/06-刷写尝试与教训.md`](docs/06-刷写尝试与教训.md)。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

---

## [未发布]

### 计划中（v0.2）

- 档案**导入 / 导出**（外部 JSON 导入、导出为文件）；本轮只做新建 / 载入 / 改名 / 删除。
- 多卡场景的批量注入与逐卡档案（当前明确只处理单张 V100）。
- 真实 GEMM / 训练负载下的收益实测（现结论基于手写 PTX 张量核心内核）。

---

## [0.1.0] — 2026-10-26

首个公开整理版。研究结论、实测证据与可复现工具首次汇总为一个自洽的仓库。

### Added（新增）

- **研究文档（编号形式）**：`docs/01-总览与结论.md`、`02-功耗墙实测.md`、`03-时钟墙实测.md`、
  `04-显存频率实测.md`、`05-注入点与初始化.md`、`06-刷写尝试与教训.md`、`07-算力锁验证.md`、
  `99-早期研究与证伪.md`。
- **实测证据**：`docs/evidence/`（探针、预检、nvflash、`nvidia-smi -q` 现场快照）。
- **工具**：`tools/derive_tables.py`（BIT 指针动态解析）、`tools/build_payload.py`（生成 payload）、
  `tools/stress_clock.py`、`tools/mem_bw.py`、`tools/diag_throttle.py`、`tools/mma_probe.py`。
- **开机自动注入**：`deploy/`（systemd 单元 + 注入脚本 + 幂等安装/卸载 + `MANIFEST.sha256` 完整性校验）。
- **实验脚本存档**：`scripts/{extract,build,probe,apply,preflight}/`（按研究阶段分类，保留原始形态）。
- **固件镜像存档**：`firmware/`（原厂 1 MiB 备份、nvflash 镜像、修改版镜像、运行时 payload、事故读回证据），
  含完整大小与 sha256 清单。
- **仓库常规文件**：`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`（本文件）、
  `.github/ISSUE_TEMPLATE/bug_report.md`、`.github/pull_request_template.md`。
- **审计清单**：`docs/00-整理清单.md` —— 逐条列出失效引用、结构偏差、敏感信息与修复状态，
  并在 §11 给出 73 条的**最终状态与证据**（已修复 / 已核实 / 不适用 / 已移交）。
- **简体中文 TUI 快捷操作台（仅 Linux，零第三方依赖）**：
  - 入口：仓库根 `./v100-tui`、`python3 -m tui`；参数 `--rom/--readonly/--no-color/--once/--log-dir/--version/--help`。
  - **快速修改**：四路 ROM 来源 → `derive_tables` 动态定位 → 功耗 150–350 W（步进 10）与五类频率
    300–1912 MHz → payload 生成 + 回读校验（体积 / sha256 / 改动字节数 / 校验和）。
  - **快速注入**：预检清单 → 完整 10 步脚本 → 键入中文确认词 → 以 `systemd-run --no-block --collect`
    提交独立 oneshot 单元；**提交成功即退出**、不做假监控，结果由 `/run/v100-tui/last-job.json` 下次呈现。
  - **记录与持久化**：档案 CRUD（`$XDG_CONFIG_HOME/v100-tui/profiles/<slug>.json`）、JSONL 历史、
    一键安装 / 卸载开机持久化、恢复原厂；非 root 时危险入口置灰并给出中文原因。
  - 规格与限制：`tui/DESIGN.md`（架构 / 数据模型 / 安全边界 / 错误码）、`tui/README.md`（使用说明与已知限制）。
- **`tools` 两处配套改造**：`tools/build_payload.py` 抽成纯函数 `build_payload(rom, power_w, clocks)` 并加
  `argparse` CLI（`--power/--out/--xbar…`，错误路径中文 + rc=2）；`tools/derive_tables.py` 仅**新增**导出字段
  `entry_size`、`entry0_3rd_at`（既有键名语义不变），便于 TUI 写回 Performance 字段。

### Changed（变更）

- **文档改名**：`V100-<主题>-2026102X.md` 系列统一改为 `01-` … `99-` 编号前缀。
- **跨文档引用修复**：22 处仍指向旧文件名的引用（含 `deploy/README.md` 中的 1 处）全部改为
  同目录 / 跨目录的 Markdown 相对链接，现在可点击且逐条可解析。
- **`README.md` 目录结构块**：补全 `README.md`、`LICENSE`、`NOTICE`、`.gitignore`、`.gitattributes`、
  `tui/`、`docs/00-整理清单.md`，并为 `07-算力锁验证.md` 补上说明；新增「文档索引」表。
- **`firmware/README.md`**：表格改为给出**完整 64 位 sha256**（原为省略式），补充校验命令与
  「nvflash 未随仓库发布」的说明；`docs/05` 引用改为可解析的相对链接。
- **`scripts/README.md`**：核对后确认五个子目录（`extract/build/probe/apply/preflight`）与实际目录
  完全一致（3/11/7/7/3 个文件），本次未改动内容。
- **`README.md` 再次整合（本轮收尾）**：目录结构补 `CONTRIBUTING.md` / `SECURITY.md` / `CHANGELOG.md` /
  `.github/`；文档索引表补 `tui/README.md`、`tui/DESIGN.md`、`.github/`；「快速开始」拆成
  **方式 A 命令行（CLI）** 与 **方式 B 交互式操作台（TUI）** 两条并列路径；§七 新增
  **「已知限制（TUI 与工具，v0.1.0）」** 表（仅 Linux / 单卡 / 无档案导入导出 / 4 MHz 对齐 /
  `Esc` 不退出 / 注入不做实时监控 / 非 350 W 依赖 `V100_EXPECT_W` 等）。
- **`deploy/` 最小参数化**：`v100-boot-patch.sh` 的期望功耗改为 `EXPECT_W="${V100_EXPECT_W:-350}"`
  （默认取值与原实现逐字节等价），单元可用 `Environment=V100_EXPECT_W=` 覆盖；`MANIFEST.sha256`
  已同步重算，`install.sh` 的完整性校验仍通过。`deploy/README.md` 的 payload 行改为真实路径
  `../firmware/v100_payload_350w.bin`，重算清单命令同步修正。

### Fixed（修复）

- 修正 `docs/01` 文档索引表中的重复条目与旧文件名（`07-算力锁验证.md` 曾被列两次、
  `99-早期研究与证伪.md` 曾被写成已废弃的旧名）。
- 修正 `deploy/README.md` 指向已改名文档的失效链接（并补上正确的相对层级 `../docs/`）。
- 修正 `firmware/README.md` 中不存在的 `./tools/nvflash` 路径（与 `NOTICE` §一「本项目未包含 nvflash」对齐）。

### Security（安全）

- 三个历史脚本中内嵌的明文 root 口令彻底移除，改为从 `ROOT_PASSWORD` 环境变量读取
  （`scripts/extract/extract_vbios.sh`、`scripts/apply/load_vbios_safe.sh`、`scripts/extract/extract_full_vbios.py`）。
- 向 `sudo -S` / `su` 传递口令的写法由 `echo "$PASSWORD" | sudo -S …` 改为 bash here-string
  （`sudo -S … <<< "$PASSWORD"`），避免口令出现在 `/proc/<pid>/cmdline`（进程表）。
- 仓库内不再包含个人绝对路径；文档中的路径统一为 `/path/to/v100-vbios-research` 占位符。
- `NOTICE` §二补全设备标识声明：`docs/evidence/gpu_state_dump.txt` 同时含**序列号与 GPU UUID**
  （此前只声明了 UUID）。

---

## 研究时间线（2026-10-25 → 2026-10-26）

以下为构成此次发布的研究节点，按发生时间排列。

### 2026-10-25

| 时间 | 节点 |
|---|---|
| 上午 | 一度误把 GTX 1050 的 ROM 当作 V100 分析，相关功耗结论作废 |
| 14:08 | 拿到第三方 1 MiB SPI dump（固件 `88.00.13.00.02`），据此推导出全部表结构 |
| 15:2x | 确认 registry 二进制注入点；跨版本覆盖生效但功能表退化 |
| 16:38 | 用 nvflash 取得本卡真实固件 `88.00.4F.00.09`（330,240 B） |
| 16:4x | 定位 `tgp_index=4` / row4，**功耗 300 W → 350 W 实测成功** |
| 17:0x | 时钟三轮 14 变体：**1530 → 1785 MHz 实测成功**；`A0` / `P38` 字段被证伪 |
| 18:11 | 瓶颈诊断：263 W / 56 °C / 零节流 → 结论为电压（VID）限制 |
| 18:46 | 锁定最终配置（`-lgc 135,1912`）并保留生效 |
| 18:56–19:47 | 显存两轮 9 变体：**硬件频率不可改**，只有上报值可改 |
| 19:5x | `P38` 四向验证：确认惰性（设 1200 仍跑到 1777） |
| 22:0x | VFE 结构解构完成，确认 slot14 的 `1912.0` 即钳位本体 |
| 23:20 | 用编程器取得完整 1 MiB flash 备份；生成可刷写修改版并完成静态校验 |

### 2026-10-26

| 时间 | 节点 |
|---|---|
| 00:16 | ⚠️ **编程器写入失败，读回全 `0xFF`（芯片空白）** —— 见 [`docs/06-刷写尝试与教训.md`](docs/06-刷写尝试与教训.md) |
| 01:56 | 改用开机内存注入：`v100-boot-patch.service` 安装并启用，归档至 `deploy/` |
| 02:2x | 仓库开源整理：文档改名为编号形式、固件与证据归档、部署备份入库 |
| 19:2x– | 开源收尾第二轮：审计清单、引用修复、补齐开源常规文件、新增 TUI 工具（进行中） |

---

## 说明

- 本项目**不承诺向后兼容**：`scripts/` 是研究过程的一次性脚本存档，接口可能随环境变化而调整。
- `firmware/` 下的镜像一旦入库便不再改动（改动会使 `NOTICE`、`firmware/README.md`、
  `deploy/MANIFEST.sha256` 中的校验值全部失效）。如需自己复现，请用
  `tools/build_payload.py` 从你自己的显卡现场生成 payload。
