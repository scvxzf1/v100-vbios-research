# V100 快捷操作台（TUI）设计与规格

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0（定稿待实现） |
| 状态 | 设计冻结，供 t4 / t5 / t6 / t7 实现，t8 按 §11 验收 |
| 编写 | tui-architect（任务 t2） |
| 适用平台 | **仅 Linux**（x86_64），Python 3 标准库 |
| 依赖 | 零第三方依赖：只用 `curses`(ncursesw)、`json`、`subprocess`、`pathlib` 等标准库 |
| 仓库 | `v100-vbios-research`（本文所有路径均相对仓库根目录） |

### 已实测的环境事实（本机验证，2026-10-26）

```text
Python 3.12.3
curses.version = b'2.2'       ncurses 6.4 (ncursesw)
import textual  -> ModuleNotFoundError
/proc/driver/nvidia/registry  存在（驱动已加载）
python3 tools/derive_tables.py firmware/v100_original_nvflash.rom  正常输出全部表定位结果
```

结论：**只能用标准库 `curses` 手写界面**，不假定任何 TUI 框架存在。

---

## 1. 目标与非目标

### 1.1 目标

把仓库已有的研究成果（动态字段推导 + 内存覆盖注入 + 开机持久化）包装成一个
**简体中文、键盘驱动、零依赖的终端操作台**，让一次「改功耗/改频率 → 生成覆盖镜像 →
注入生效 → 存档复现」的操作从「翻脚本、改路径、手敲五条命令」压缩到
「上下键选择 + 回车 + 输入确认词」。

三条硬性要求：

1. **不硬编码任何 VBIOS 字段偏移**。所有偏移必须来自 `tools/derive_tables.py` 的
   BIT 指针链动态推导，换固件版本能自动重定位；推导失败就拒绝写入，绝不猜。
2. **不做任何破坏性操作**。不写 flash、不改电压、不写 GPU 寄存器；唯一改动是
   `nvidia.ko` 模块级 registry 里的内存镜像，`rmmod` 或重启即消失。
3. **任何失败都必须回到原厂状态**，且不能让显卡停在「半摘卡」状态（详见 §9）。

### 1.2 非目标（明确不做，避免范围膨胀）

| 非目标 | 原因 |
|---|---|
| 完整 IDE / 十六进制编辑器 / 任意字节编辑 | 本工具是「快捷操作台」，任意字节编辑无法保证校验和与语义正确 |
| 物理刷写 flash（`nvflash` 写入） | 仓库已记录一次刷写变砖事故（`docs/06-刷写尝试与教训.md`），本工具禁止提供入口 |
| 电压修改、显存频率修改、算力锁开关 | 实测已证伪或不可改（`docs/03` `docs/04` `docs/07`），列出会误导 |
| 多卡批量注入 / 集群编排 | 覆盖是模块级全局状态，多卡场景收益与风险都不明确 |
| 压测与带宽测量面板 | `tools/stress_clock.py`、`tools/mem_bw.py` 已有 CLI，TUI 最多在结果页展示其输出摘要，不复刻 |
| 远程/Web/HTTP 接口、守护进程、自动定时注入 | 攻击面与权限模型都会失控；持久化只用 systemd 单元 |
| 引入 `textual`/`rich`/`prompt_toolkit` 等任何第三方包 | 目标环境没有，且要求零依赖 |
| 保存任何凭据（root 密码、token） | 团队硬约束：**禁止明文密码**；需要 root 时只提示用 `sudo` 重新启动 |
| 保存任何个人绝对路径 | 团队硬约束：**禁止个人路径**；配置与缓存走 XDG 变量与相对路径 |

---

## 2. 总体架构

### 2.1 分层

```text
┌──────────────────────────────────────────────────────────────┐
│ 表现层  tui/ui/            curses 界面、中文文案、确认框          │
│          （只依赖"应用服务层"的返回结构，不直接跑命令）             │
├──────────────────────────────────────────────────────────────┤
│ 应用层  tui/ui/screens/*   职责编排（不新增中间层，见 §4）          │
│         参数校验 → 调用数据层 → 二次确认 → 调用执行层               │
├──────────────────────────────────────────────────────────────┤
│ 数据层  env_probe / gpu_state / rom_source / vbios_derive /     │
│         payload / profile / history         （只读或纯计算）      │
├──────────────────────────────────────────────────────────────┤
│ 执行层  inject / persist  （唯一允许改变系统状态的两处）            │
├──────────────────────────────────────────────────────────────┤
│ 既有资产 tools/derive_tables.py  tools/build_payload.py        │
│          deploy/{install.sh,uninstall.sh,v100-boot-patch.*}    │
└──────────────────────────────────────────────────────────────┘
```

分层纪律（t8 会检查）：

* `tui/ui/**` 不得出现 `subprocess`、`os.system`、`open("/proc/...")`。
* `tui/payload.py`、`tui/profile.py`、`tui/history.py` 不得出现 `subprocess`。
* 只有 `tui/env_probe.py`、`tui/gpu_state.py`、`tui/rom_source.py`、`tui/inject.py`、
  `tui/persist.py` 允许调用外部命令；且一律通过统一的 `run()` 包装（捕获退出码、
  超时、把 stderr 收集进日志）。
* **只有 `tui/inject.py` 与 `tui/persist.py` 允许改变系统状态**（卸/装模块、摘卡、
  写 registry、启停服务、写 `/etc`）；`env_probe`/`gpu_state`/`rom_source` 只做只读查询
  （唯一的例外是 `/sys/.../rom` 使能位，由 `rom_source.load_rom(allow_sys_enable=True)`
  在 root 下短暂置 1 再置 0，且不改变显卡工作状态）。

### 2.2 主数据流

```text
  ┌─ ROM 来源 ─────────────────────┐
  │ ① /sys/bus/pci/devices/<bdf>/rom │
  │ ② nvidia-smi --gpu-rom-dump      │──► rom_source.load_rom()
  │ ③ 指定文件 --rom <path>          │        │
  │ ④ 仓库 firmware/*.rom 回退       │        ▼
  └──────────────────────────────────┘   derive_layout()  ← tools/derive_tables.py
                                              │  （失败即中止，不猜）
                                              ▼
   Profile(JSON) ──┐                    payload.build()  ← tools/build_payload.py
                   ├──► 编辑意图 ──────►   │  写 20 字节级别的字段 + 重算镜像校验和
   GPU 现值(只读) ──┘                      ▼
                                    覆盖镜像（327,680 字节, sha256）
                                              │
                     ┌────────────────────────┴──────────────────┐
                     ▼                                           ▼
            inject.submit()                             persist.install()
            systemd-run --no-block 独立单元               写 /etc/v100 + systemd drop-in
                     │                                           │
                     ▼                                           ▼
              /var/log/v100-tui/*.log                 开机时由既有单元自动注入
              /run/v100-tui/last-job.json
```

### 2.3 界面骨架（80×24 起）

```text
┌ V100 快捷操作台 v1.0 ───────────────────── 只读模式 ─────────────┐
│ 显卡 0000:04:00.0  V100-SXM2-16GB   VBIOS 96.00.74.00.05        │
│ 功耗 300 W / 上限 300 W   核心上报 1530 MHz   实际 1530 MHz     │
│ 驱动 已加载   注入契约 /proc/driver/nvidia/registry 可用          │
│ 开机持久化 未安装   图形会话 gdm3 运行中   lactd 运行中            │
├─ 功能 ──────────┬─ 详情 ────────────────────────────────────────┤
│ ▸ 快速修改       │  字段            现值      目标               │
│   快速注入       │  功耗上限        300 W     350 W              │
│   配置档案       │  核心上报上限    1530 MHz  1912 MHz           │
│   开机持久化     │  Applications    1312 MHz  1800 MHz           │
│   恢复原厂       │  Performance     1528 MHz  1912 MHz           │
│   操作历史       │                                             │
├─────────────────┴───────────────────────────────────────────────┤
│ ↑↓ 选择  Enter 进入  F5 刷新  F10 执行  ? 帮助  q 退出           │
└─────────────────────────────────────────────────────────────────┘
```

* 顶部固定横幅：`只读模式（未使用 sudo）` 或 `未检测到 V100`。
* 底部固定状态行：最近一条 `完成 / 提示 / 错误` 消息（单行截断，`l` 打开完整日志）。
* 终端小于 80×24 时，切到单列布局并在状态行提示「终端过小，部分信息已省略」。

---

## 3. 三个核心能力与交互流程

### 3.1 能力一：快速修改（只算不写硬件）

**入口**：主菜单「快速修改」。界面上永远先展示**现值**，再展示**目标值**，两侧对照。

流程：

1. **选择对象**：默认自动选中带 `1db1`（GV100 / V100）设备 ID 的卡；多卡时先选 BDF。
   若没有 V100，则提示「未检测到 V100；可对 ROM 文件做离线修改」，进入离线模式。
2. **取得基准 ROM**：按 §2.2 的 ①②③④ 顺序取，命中即用，并在屏幕标注来源与 sha256：
   * ① `/sys/bus/pci/devices/<bdf>/rom`（需 root 且先写 `1` 使能）；
   * ② `nvidia-smi --gpu-rom-dump` 到临时文件；
   * ③ 用户 `--rom <相对或绝对路径>`；
   * ④ 仓库内 `firmware/v100_original_nvflash.rom`（明确提示「使用仓库自带原厂样本」）。
3. **动态推导字段表**：调用 `derive_layout()`，一次性拿到功耗行、P-State 四字段、
   Performance ENTRY0 字段的**绝对偏移**与现值。任一必需表定位失败 → 立即中止并报
   `错误[E0202]`，**不进入编辑界面**（因为无法安全写入）。
4. **编辑**（表单，逐字段校验，非法值不允许离开输入框）：

   | 字段 | 单位 | 允许范围 | 额外规则 |
   |---|---|---|---|
   | 功耗上限 | W（0.1 精度） | **150 – 350** | 必须 > 该行 `low`（下限）；≤ 该行原 `max` 的 350 上限硬顶 |
   | 核心上报上限（XBAR） | MHz（整数） | 300 – 1912 | 写值 + 比率字段一致性校验（见 §10 附录 A） |
   | SYS（随 XBAR 联动） | MHz | 300 – 1912 | 默认与 XBAR 同值，可解耦 |
   | Applications（GPC_HDR） | MHz | 300 – 1912 | 原厂 1312 |
   | Applications（GPC_R1） | MHz | 300 – 1912 | 默认与 GPC_HDR 同值 |
   | Performance ENTRY0 | MHz | 300 – 1912 | 原厂 1528，默认与 XBAR 同值 |

   超过 1785 MHz 时在字段下方给出**黄色提示**（`提示[W0310] 1785 MHz 以上为出厂电压下的
   物理上限，超出部分仅影响上报值，实际频率不会提升`）——这是仓库实测结论，必须如实告知，
   不能承诺效果。
5. **生成覆盖镜像**：`payload.build()` → 写出到 `$XDG_CACHE_HOME/v100-tui/payloads/`，
   回读校验（重新推导一次，逐字段比对；越界改动数必须为 0），展示结果摘要：
   * 体积（期望 327,680 字节）、sha256；
   * 改动字节数与所在镜像（期望落在唯一一个 `code==0xE0` 数据镜像内）；
   * 校验和重算结果（`old → new` 末字节）。
6. **下一步**：`[保存为档案]`、`[立即注入]`、`[导出路径]`（只复制到剪贴板替代方案：
   在状态行显示可复制的相对路径）。

> 约束重申：整个「快速修改」**不写 `/proc/driver/nvidia/registry`、不摘卡、不动驱动**，
> 纯本地文件运算，任何权限下都可执行。

### 3.2 能力二：快速注入（会中断图形会话）

**入口**：主菜单「快速注入」。前置：`env_probe` 判定为 root + Linux + 驱动契约存在 +
V100 在总线上；否则入口置灰并说明原因（§6）。

交互流程：

1. 选定覆盖镜像（来自 §3.1 生成的 payload 或档案里记录的 payload）。
2. **预检页**（只读，全部显示为 ✅/❌）：
   * 目标 BDF 目录存在；
   * `/proc/driver/nvidia/registry` 可写；
   * `/dev/nvidia*` 无进程占用（`fuser` 为空）——有占用则**直接禁止提交**并列出 PID；
     理由见 `deploy/README.md`：持有设备的进程会让内核 `nv_pci_remove` 阻塞数分钟；
   * payload 尺寸与 sha256 与预期一致；
   * 磁盘上 `/run/v100-tui`、`/var/log/v100-tui` 可写；
   * 探测显示管理器单元名（`gdm3` / `gdm` / `sddm` / `lightdm` 中实际 active 的那个）
     与 `lactd` 是否 active。
3. **二次确认页**（危险操作，见 §6.3）：显示「即将中断图形会话」的完整后果清单、
   目标值 diff、预计耗时 20–60 秒、失败自动回原厂，并要求键入确认词 `注入`。
4. **提交**（关键设计点）：TUI 自身若运行在图形终端里，**一旦停掉显示管理器就会连带
   被杀掉**。因此：

   * 注入逻辑整体写入独立脚本 `/run/v100-tui/inject-<时间戳>.sh`；
   * 用 `systemd-run --no-block --collect --unit=v100-tui-inject-<时间戳> \
     --property=Type=oneshot --property=TimeoutStartSec=180 \
     /bin/bash /run/v100-tui/inject-<时间戳>.sh` 提交为**独立 systemd 单元**，
     TUI 的进程树不再是它的父进程；
   * 提交成功后 TUI 立刻恢复终端（`curses.endwin()`）、打印中文交接提示并退出：
     「注入任务已提交为独立单元 `v100-tui-inject-<时间戳>.service`。图形会话将在数秒后
     中断，本界面会随之关闭。请在 TTY 或 SSH 会话查看进度：
     `journalctl -u v100-tui-inject-<时间戳> -f`，或查看
     `/var/log/v100-tui/inject-<时间戳>.log`。」
   * TUI **不做**「提交后继续刷新进度」——因为多数情况下它自己会被一起杀掉，
     假装能监控是欺骗性设计。下次启动时由 `/run/v100-tui/last-job.json` 呈现上次结果。
   * 若 TUI 检测到当前终端不是图形会话（`/proc/self/stat` 的前台 tty 属于 tty[1-6]
     或 SSH），提示可以保留轮询：进入「等待页」读取日志尾部直至任务结束。

5. **单元脚本内的 PCI 窗口流程**（与 `README.md` §三、`deploy/v100-boot-patch.sh` 完全
   同构，不允许自创顺序）：

   ```text
   ① 停 lactd（若 active，记 STOPPED_LACT=1）        —— 它握住 /dev/nvidia* 会卸不掉驱动
   ② 停显示管理器（探测到的实际单元名）
   ③ 卸驱动：nvidia_drm / nvidia_modeset / nvidia_uvm / nvidia，每个模块 3 次重试
   ④ fuser 检查 /dev/nvidia{0,1,ctl,uvm}，有占用 → fail "设备被占用"
   ⑤ echo 1 > /sys/bus/pci/devices/<bdf>/remove   —— 摘卡
   ⑥ modprobe nvidia                              —— 此时只 probe 显示卡
   ⑦ { printf 'vbios='; cat <payload>; } > /proc/driver/nvidia/registry  —— 写 registry
   ⑧ echo 1 > /sys/bus/pci/rescan                 —— 卡带覆盖上线
   ⑨ 验证：nvidia-smi --query-gpu=power.max_limit,clocks.max.sm → 与目标比对
   ⑩ 恢复：start lactd（若 ① 停过）→ start 显示管理器
   trap EXIT 兜底：无论从哪一步退出，都执行 ⑤ 的反向（rescan）+ modprobe 全套 + 恢复服务
   ```

   每一阶段先写状态文件（write-ahead，见 §5.4）再执行，用于崩溃后识别「半摘卡」。

### 3.3 能力三：快速记录与持久化

**A. 配置档案（Profile）**

* 列表页：名称、功耗、核心上报、Applications、目标固件版本、更新时间、来源（本机生成 /
  导入）。
* 操作：`n` 新建（从当前编辑态抓取）、`Enter` 载入到编辑界面、`e` 改名/备注、
  `x` 删除（二次确认，键入 `删除`）、`i` 从文件导入、`o` 导出到文件（导出的 JSON 与
  内部格式一致）。
* 载入档案时若记录的 `rom_sha256` 与当前基准 ROM 不一致，给出 `提示[W0602]
  该档案基于另一份固件（sha256 …），字段偏移将重新推导`；**偏移永不从档案读取，
  一定重新推导**（档案里只存语义值：W/MHz）。

**B. 操作历史**

* 追加式 JSONL，界面按时间倒序显示：时间、动作（注入/生成/存档/装持久化/卸载持久化/
  恢复原厂）、结果、关键数值、payload sha256 前 8 位。
* 只读派生视图 + `d` 导出为文本；不做编辑。

**C. 开机持久化（复用 `deploy/` 资产）**

* 状态页显示：单元 `v100-boot-patch.service` 的 `is-enabled` / `is-active`、
  已安装 payload 的 sha256、当前 drop-in 的期望功耗、`/var/log/v100-boot-patch.log`
  的最后一行的成功/失败摘要。
* 安装：调用 `bash deploy/install.sh`（其自带 `MANIFEST.sha256` 完整性校验，**不绕过**），
  成功后按需写 drop-in 与替换 payload（见 §9.3 的参数化要求）。
* 卸载：调用 `bash deploy/uninstall.sh`，随后提示「当前内存中的覆盖仍在生效，
  需要恢复原厂请使用主菜单『恢复原厂』或重启」。
* 二次确认：安装键入 `安装`，卸载键入 `卸载`。
* 互斥提示：若检测到 flash 已被刷写为修改版（`nvidia-smi` 上报的 `power.max_limit`
  在原厂驱动下即为非 300 W），提示「检测到 flash 可能已被修改，建议不要叠加持久化」
  （来自 `deploy/README.md` 注意事项 1）。
* 冲突提示：读取 `/etc/lact/config.yaml` 中 V100 条目的 `power_cap`，若非目标值则提示
  「LACT 的 power_cap 会把功耗改回，建议在 LACT 中固定为 <目标>」（注意事项 2）。
  **只提示，不代改用户配置。**

---

## 4. 模块划分与文件清单

全部位于 `tui/`。`负责` 列对应团队任务：t4=tui-core-dev（数据层）、t5=tui-ui-dev（界面层）、
t6=tui-inject-dev（执行层）、t7=tui-core-dev（集成与入口）。

| 文件 | 负责 | 职责 | 对外接口（签名以行为契约表述） |
|---|---|---|---|
| `tui/__init__.py` | t7 | 包标识 | `__version__: str = "1.0"` |
| `tui/__main__.py` | t7 | 入口：`python3 -m tui`；解析参数、平台/权限闸门、启动 App、统一异常出口 | `main(argv: list[str]) -> int`；参数：`--rom PATH`、`--readonly`、`--version`、`--help`、`--log-dir PATH` |
| `tui/constants.py` | t4 | **唯一**允许出现十六进制常量的地方：驱动接口协议常量（payload 前缀 `0xA00`、长度 `327680`）、数值范围（150–350 W、300–1912 MHz）、路径模板（`/proc/driver/nvidia/registry`、`/etc/v100`、`/run/v100-tui`、`/var/log/v100-tui`）、超时值。每条常量必须带来源注释 | 模块级常量 + `WARN_CLOCK_MHZ = 1785` |
| `tui/env_probe.py` | t4 | 平台与能力探测：OS、是否 root、`curses` 可用性、驱动契约、`systemd-run` 是否存在、显示管理器与 `lactd` 的单元名与状态、终端尺寸 | `probe() -> EnvInfo`（dataclass，字段：`platform_ok`、`is_root`、`dm_unit`、`dm_active`、`lactd_active`、`registry_ok`、`systemd_run_ok`、`readonly`、`reasons: list[str]`） |
| `tui/gpu_state.py` | t4 | 通过 `nvidia-smi` 按 **BDF**（不是 `-i` 索引）读取现值，解析 CSV；多卡枚举 | `list_gpus() -> list[GpuState]`；`read_gpu_state(bdf: str) -> GpuState`（字段：`bdf/name/vbios_version/power_limit_w/power_max_w/power_default_w/sm_max_mhz/apps_graphics_mhz/sm_current_mhz/temp_c`，全部 `Optional`，缺失为 `None` 而非 0） |
| `tui/rom_source.py` | t4 | 按 ①②③④ 顺序取基准 ROM，返回内容 + 来源说明 + sha256；只读（`/sys/.../rom` 使能属执行层，见下） | `load_rom(bdf: str, explicit_path: str\|None, allow_sys_enable: bool) -> RomCandidate(bytes, source: str, sha256: str)` |
| `tui/vbios_derive.py` | t4 | 以 `importlib.util.spec_from_file_location` 载入 `tools/derive_tables.py`，调用 `derive()`，把结果规整成 TUI 用的 `Layout`（**所有偏移均来自该模块**）；提供字段级「可否安全写入」判定 | `derive_layout(rom: bytes) -> Layout`；`Layout.power_row_at/rows/xbar/gpc_hdr/gpc_r1/sys/performance_entry0_at/data_image_range` 等；失败抛 `DeriveError(代码, 原因)` |
| `tui/payload.py` | t4 | 编辑意图 → 覆盖镜像；调用 `build_payload.build_payload()`；回读校验；差分摘要；写盘与 sha256 | `build(rom: bytes, intent: EditIntent) -> Payload(bytes, sha256, diff_summary)`；`write(payload: Payload, cache_dir: Path) -> Path`；`verify_roundtrip(rom: bytes, payload: Payload) -> VerifyReport` |
| `tui/profile.py` | t4 | 档案 CRUD、校验、版本迁移、原子写（`tmp` + `os.replace`） | `list_profiles() -> list[ProfileMeta]`；`load(name) -> Profile`；`save(profile) -> Path`；`delete(name)`；`validate(dict) -> list[str]` |
| `tui/history.py` | t4 | 追加 JSONL、尾部读取、导出 | `append(event: dict) -> None`；`tail(n: int) -> list[dict]`；`export_text(path)` |
| `tui/inject.py` | t6 | 预检、生成单元脚本、`systemd-run` 提交、状态读取、崩溃后「半摘卡」检测与恢复建议 | `precheck(job: InjectJob) -> list[Check]`；`submit(job) -> JobHandle(unit, script, log)`；`read_job_state() -> JobState`；`resume_hint() -> Optional[Hint]`；`restore_factory_order() -> JobHandle` |
| `tui/persist.py` | t6 | 持久化安装/卸载/状态；调用 `deploy/install.sh`、`deploy/uninstall.sh`；写/删 drop-in；校验 payload sha256 | `state() -> PersistState`；`install(profile\|payload) -> Result`；`uninstall() -> Result` |
| `tui/ui/text_zh.py` | t5 | 全部中文文案常量、错误码表、宽度计算（`unicodedata.east_asian_width`）、`fmt_error(code, cause, advice)` | `T`（常量字典）、`fmt_error/fmt_warn/fmt_ok`、`dwidth(s) -> int`、`wrap(s, width) -> list[str]` |
| `tui/ui/widgets.py` | t5 | 通用组件：列表、表单（输入/数值步进/单选）、确认框、日志面板、状态栏、帮助浮层、横幅 | 纯绘图与键处理，不碰业务 |
| `tui/ui/screens/edit.py` | t5 | 3.1 的界面与校验反馈 | `EditScreen(app).run() -> ScreenResult` |
| `tui/ui/screens/inject.py` | t5 | 3.2 的预检页/确认页/等待页 | 同上 |
| `tui/ui/screens/profiles.py` | t5 | 档案列表、编辑、导入导出、历史视图 | 同上 |
| `tui/ui/screens/system.py` | t5 | 持久化状态、恢复原厂、环境诊断页 | 同上 |
| `tui/ui/app.py` | t5 | `curses.wrapper` 初始化、调色板（无颜色终端自动降级）、路由、全局键 | `App(env, deps).run() -> int` |
| `tui/tests/test_payload.py` | t7 | 离线回归：对 `firmware/v100_original_nvflash.rom` 生成 350 W/1912/1800，**必须与 `firmware/v100_payload_350w.bin` 逐字节相同** | `unittest` |
| `tui/tests/test_profile.py` | t7 | 档案序列化、未知版本、损坏文件、迁移 | `unittest` |
| `tui/tests/test_text_zh.py` | t7 | 术语表覆盖：源码中不得出现裸英文用户可见串（白名单：技术标识符） | `unittest` |
| `tui/README.md` | t9 | 使用说明与截图（纯文本界面示意） | — |

> `tui/tests/` 一律**不需要 root、不触碰 `/proc`、不调 `nvidia-smi`**；所有外部命令在测试里
> 用注入的假 `run()` 替身。

---

## 5. 数据模型

### 5.1 存放路径

一律 XDG/系统路径，**不得出现任何个人绝对路径**：

| 内容 | 路径 | 说明 |
|---|---|---|
| 档案 | `$XDG_CONFIG_HOME/v100-tui/profiles/<slug>.json`（缺省 `~/.config/…`） | 用户级 |
| 历史 | `$XDG_CONFIG_HOME/v100-tui/history.jsonl` | 追加式 |
| 覆盖镜像缓存 | `$XDG_CACHE_HOME/v100-tui/payloads/<slug>-<sha8>.bin` | 可随时删除 |
| 运行期状态 | `/run/v100-tui/{state.json,last-job.json,lock}`, `/run/v100-tui/inject-*.sh` | root |
| 注入日志 | `/var/log/v100-tui/inject-<时间戳>.log` | root |
| 持久化资产 | `/etc/v100/`、`/usr/local/sbin/`、`/etc/systemd/system/` | 由 `deploy/` 负责 |

**以 `sudo` 运行时的归属修正**：若 `SUDO_USER` 存在，则档案/历史/payload 缓存以
`SUDO_USER` 的 home 为基准（通过 `pwd.getpwnam` 解析），并用 `os.chown` 归还属主，
避免把文件写进 `/root`；解析失败则退化为当前用户并给出 `提示[W0102]`。

**文件名策略**：`slug` 只允许 `[a-zA-Z0-9_-]{1,32}`（由中文名转写或用户输入，冲突自动加
`-2`）；中文名称保存在 `display_name` 字段，作为界面展示名——避免中文文件名在
`systemd-run --unit=`、日志路径等处引发兼容问题。

### 5.2 Profile JSON（version = 1）

```json
{
  "kind": "v100-tui.profile",
  "version": 1,
  "slug": "pow350-clk1912",
  "display_name": "功耗 350 W + 核心 1912",
  "note": "长时间训练用，注意电源余量",
  "created_at": "2026-10-26T19:30:00+08:00",
  "updated_at": "2026-10-26T19:30:00+08:00",
  "target": {
    "bdf": "0000:04:00.0",
    "vbios_version": "96.00.74.00.05",
    "rom_sha256": "<64 位十六进制>",
    "rom_length": 330240
  },
  "power": { "limit_w": 350.0 },
  "clocks": {
    "pstate_xbar_mhz": 1912,
    "pstate_sys_mhz": 1912,
    "pstate_gpc_hdr_mhz": 1800,
    "pstate_gpc_r1_mhz": 1800,
    "performance_entry0_mhz": 1912
  },
  "payload": {
    "size": 327680,
    "sha256": "<64 位十六进制>",
    "cache_name": "pow350-clk1912-8163a67d.bin"
  },
  "extra": {}
}
```

约定：

* **只存语义值（W / MHz），绝不存字节偏移**。偏移每次重新推导——这是换固件版本仍能工作的前提。
* `payload.cache_name` 只是**相对文件名**（相对 payload 缓存目录），不是绝对路径。
* 不存任何凭据、不存 `SUDO_USER`、不存主机名。
* `note` / `display_name` 允许中文；其余字段用 ASCII，便于脚本处理。
* 单位精度：功耗保留 1 位小数；频率为整数 MHz；时间用带时区的 ISO 8601（本地时区偏移）。

### 5.3 版本号与向后兼容策略

| 情形 | 行为 |
|---|---|
| `version == 1` | 正常读写 |
| 缺 `version` | 视为 `1`，写回时补上 |
| `version > 1`（更高版本创建） | **只读**：可展示、可"导出"，禁止「载入并注入」与「设为开机目标」，提示 `错误[E0603] 该档案由更高版本创建…` |
| `version < 当前` | 依次执行迁移函数链 `_migrate_v1_to_v2(d)`，迁移是纯函数，失败则该档案只读 |
| 缺字段 | 用默认值补齐后在内存中标记 `filled=[]`，界面上以「默认」标注 |
| 未知字段 | 原样保留在 `extra`（顶层未知键也并入 `extra`），写回时携带，保证前向兼容 |
| 文件损坏（JSON 解析失败） | 不覆盖原文件；提示并提供「另存为 `*.corrupt-<时间戳>.json`」与「忽略」两个选项 |

新增字段的兼容原则：**只增不改语义、不改类型**；重命名必须走版本升级 + 迁移函数。
`kind` 字段用于识别误放的其他 JSON，不匹配则拒绝载入。

### 5.4 运行期状态（write-ahead）

`/run/v100-tui/state.json`：

```json
{
  "unit": "v100-tui-inject-20261026T193000",
  "bdf": "0000:04:00.0",
  "phase": "card_removed",
  "started_at": "2026-10-26T19:30:00+08:00",
  "updated_at": "2026-10-26T19:30:22+08:00",
  "payload_sha256": "…",
  "expected": { "power_max_w": 350.0, "sm_max_mhz": 1912 },
  "stop_lactd": true,
  "dm_unit": "gdm3"
}
```

`phase ∈ {preparing, driver_unloaded, card_removed, payload_written, rescanning,
rescan_failed, verifying, restoring, done, failed}`。**每次进入下一阶段前先落盘当前 phase**，
崩溃/断电后 `inject.read_job_state()` 能据此判断危险状态（§9.4）。

`/run/v100-tui/last-job.json`：任务结束后的最终结论（`result: ok|verify_failed|failed`、
`detail`、`log_path`、`finished_at`），用于 TUI 下次启动时呈现"上次注入结果"。

---

## 6. 安全边界

### 6.1 平台闸门（启动即判定）

| 条件 | 行为 | 退出码 |
|---|---|---|
| `sys.platform != "linux"` | 直接打印：「本工具仅支持 Linux（当前系统 `<platform>`）。VBIOS 内存覆盖依赖 Linux 驱动的 `/proc/driver/nvidia/registry` 接口，其他系统无法使用。」然后**退出** | `2` |
| `curses` 导入失败或 `setupterm` 失败 | 提示改用 `--once`（文本模式输出同样信息），不进入界面 | `2` |
| `--once` / 非 TTY | 不进 curses，走纯文本报告模式 | `0/1` |

### 6.2 权限降级矩阵

| 条件 | 展示 | 快速修改 | 快速注入 | 档案/历史 | 开机持久化 | 恢复原厂 |
|---|---|---|---|---|---|---|
| root + Linux + 契约可用 + 有 V100 | 全功能 | ✅ | ✅（需二次确认） | ✅ | ✅（需二次确认） | ✅ |
| **非 root** | 顶部横幅「只读模式（未使用 sudo）」+ 提示按 §6.4 提权 | ✅ 生成 payload 到 `$XDG_CACHE_HOME`（不触硬件） | ❌ 置灰，提示 `错误[E0103]` | ✅ 用户级写入（`--readonly` 可关闭） | ❌ 置灰 | ❌ 置灰 |
| root 但 `/proc/driver/nvidia/registry` 不存在 | 横幅「驱动未加载或版本不支持」 | ✅ | ❌ 置灰 | ✅ | ✅（仍可装，但提示注入可能无效） | ❌ |
| root 但 `systemd-run` 不存在 | 横幅「缺少 systemd-run，无法安全提交独立任务」 | ✅ | ❌ 置灰（**绝不在 TUI 进程内直接卸驱动**） | ✅ | ✅ | ❌ |
| 无 V100（仅其他卡） | 横幅「未检测到 V100」 | ✅ 仅离线（需 `--rom` 或仓库样本） | ❌ | ✅ | ❌ | ❌ |
| 检测到上次任务停在危险 phase | 首次进入即弹「未完成任务」页 | ✅ | ❌ | ✅ | ❌ | ✅（唯一可用入口） |

原则：**任何降级都只减少能力，不隐藏信息**——所有被禁用的入口必须显示"为什么禁用"和
"如何恢复"，不允许出现「按了没反应」。

### 6.3 危险操作二次确认（三类，统一组件）

| 操作 | 确认词 | 确认页必须展示 |
|---|---|---|
| 快速注入 | `注入` | 目标 BDF 与固件版本、功耗/频率 diff（现值 → 目标）、**「图形会话将中断，本界面会被关闭」**、预计 20–60 秒、失败自动回原厂、payload sha256 |
| 开机持久化安装/卸载 | `安装` / `卸载` | 将写入/删除的具体路径清单、期望功耗、`MANIFEST` 校验会执行、卸载后需恢复原厂才能立即生效 |
| 恢复原厂 | `恢复` | 当前覆盖值 → 原厂值、会 `rmmod`+`modprobe`、图形会话中断、若装了持久化则「重启后仍会注入」 |

确认词方案：不使用"按 y 确认"（易误触、且中文环境里 `y` 有歧义），要求**键入完整中文词
后回车**；输入框内回显 `*`。`Esc` 一律取消，取消后不得留下副作用。

### 6.4 提权方式

不实现任何自行提权（不读密码、不把密码通过标准输入喂给 `sudo`、不写 sudoers）。只提示：

```
提示[W0101] 注入与开机持久化需要 root 权限。
建议：退出本工具后执行 `sudo python3 -m tui`（在仓库根目录）。
```

配合团队硬约束：**代码与文档中不得出现明文密码，也不得出现借助标准输入传密码的提权写法**
（`scripts/extract/extract_vbios.sh` 等历史脚本已改为读环境变量，TUI 不复制该模式）。

### 6.5 输入校验（前端 + 数据层双份）

* 数值字段只接受 ASCII 数字与小数点；非法输入不得离开输入框，回车时在框下显示
  `错误[E0302] 功耗必须是 150–350 之间的数字`。
* 范围硬顶：功耗 150–350 W；频率 300–1912 MHz；功耗还必须 `> 该行 low`。
* 目标值与原值相同时禁止提交（`提示[W0303] 目标值与现值相同，无需生成覆盖镜像`）。
* 路径输入（`--rom`）只做 `Path.resolve()` 与存在性/可读性检查；不把它写进档案。

---

## 7. 中文文案规范

### 7.1 术语统一表（全项目唯一口径）

| 英文/原词 | 统一中文用词 | 使用规则 |
|---|---|---|
| payload | **覆盖镜像** | 首次出现写「覆盖镜像（payload）」，之后只用「覆盖镜像」 |
| override / RAM override | **覆盖** | 「内存覆盖」指 registry 覆盖机制本身 |
| inject / injection | **注入** | 「快速注入」「注入任务」「注入失败」 |
| persistence / boot patch | **开机持久化** | 不写「持久化补丁」「开机补丁」 |
| profile | **配置档案** | 首次出现写「配置档案（profile）」，界面列名用「档案」 |
| flash / flashing | **刷写** | 一律出现在否定句或风险提示中：「本工具不写 flash，也不提供刷写入口」 |
| PCI remove / rescan | **摘卡 / 重新扫描总线** | 不写「下电」「热拔」 |
| restore factory | **恢复原厂** | 不写「回滚」 |
| dry run | **试算** | 「试算不落盘」 |
| registry | **驱动 registry 接口** | 路径 `/proc/driver/nvidia/registry` 原样保留 |
| driver / module | **驱动 / 内核模块** | 模块名（`nvidia.ko`）原样保留 |
| display manager | **显示管理器** | 后接实际单元名，如「显示管理器（gdm3）」 |
| display session | **图形会话** | 「会中断图形会话」 |
| power limit | **功耗上限** | 不写「功耗墙」（该词只出现在研究文档标题里） |
| Applications Clocks | **Applications 时钟** | 保留英文专名 + 中文量词 |
| Max SM clock | **核心上报上限** | 强调"上报"，因为实际频率另受物理限制 |
| checksum | **校验和** | — |
| image | **镜像**（`Image0`/`Image3` 保留原名） | 「数据镜像」「代码镜像」 |

### 7.2 错误与提示格式

```text
错误[E<四位编号>] <一句话结论，句号结尾>
  原因：<可观测事实，不带猜测>
  建议：<一个可执行动作>
```

```text
提示[W<四位编号>] <一句话说明>          ← 黄色，不阻断操作
完成：<做了什么 + 关键数值>              ← 绿色
· <进度行>                              ← 灰色，可多行
```

编号分区：`E01xx` 环境与权限、`E02xx` ROM 读取与解析、`E03xx` 覆盖镜像生成、
`E04xx` 注入、`E05xx` 开机持久化、`E06xx` 档案与历史、`E09xx` 内部错误（附 traceback 摘要）。
编号一旦分配不得复用；`text_zh.py` 中集中登记全部编号与文案，界面不允许拼字符串。

### 7.3 排版规则

* 全角标点（，。；：！？（）「」），数字与英文标识符内部半角。
* 数字与单位之间留一个半角空格：`350 W`、`1912 MHz`、`20–60 秒`、`327,680 字节`。
* 中文与英文/数字相邻时留一个半角空格：`写入 registry 失败`、`V100 不在总线上`。
* 不使用「我 / 你 / 您」，用「本工具」或省略主语（`无法读取当前 VBIOS。`）。
* 命令行片段用反引号包裹，路径原样（如 `/proc/driver/nvidia/registry`）。
* 界面文案按 `dwidth()` 计算显示宽度（CJK 计 2 列），超宽必须换行而非截断；
  只有状态行允许 `…` 截断。
* 禁止出现"研究结论"式的武断承诺：涉及效果一律引用实测口径（如「实测 +15.5%」），
  并在超出 1785 MHz 时给出 §3.1 的提示。

---

## 8. 界面与按键规范

| 键 | 作用 |
|---|---|
| `↑` `↓` / `j` `k` | 列表/表单内移动 |
| `Tab` / `Shift-Tab` | 表单内切换字段 |
| `←` `→` | 数值字段 ±1；配合 `Shift` 为 ±10 |
| `Enter` | 进入 / 确认输入 / 表单提交 |
| `Esc` | 返回上一级；在确认框中等同取消 |
| `F5` | 重新读取（只读探测，绝不触发注入） |
| `F10` | 执行当前页的主操作（仍需二次确认） |
| `n` `e` `x` | 档案：新建 / 编辑 / 删除 |
| `l` | 打开日志面板（只读滚动） |
| `?` | 帮助浮层（含术语表与错误码速查） |
| `q` | 退出（输入态下输入 `q`，不退出） |

* 颜色：用 `curses` 的 8 色；`has_colors()` 为假时用「前缀符号 + 亮度」表达
  成功 `完成：` / 提示 `提示[W…]` / 错误 `错误[E…]`，保证单色终端语义不丢。
* 所有耗时操作（`nvidia-smi`、sha256、`systemd-run`）必须显示进度行并可 `Esc` 放弃
  （放弃只终止本地等待，已提交的 systemd 单元不受影响，需明确提示这一点）。

---

## 9. 异常与回退策略

### 9.1 注入阶段失败矩阵

| 阶段 | 失败情形 | 处理动作 | 硬件状态 |
|---|---|---|---|
| 预检 | BDF 不存在 / registry 不可写 / `/dev/nvidia*` 有占用 / payload 尺寸或 sha256 不符 | **不提交**，报 `E04xx`，系统零改动 | 未改变 |
| 卸驱动 | 3 次重试后仍有 `nvidia*` 模块驻留 | `fail()`：rescan（若曾摘卡）+ `modprobe nvidia` 全套 + 拉起 lactd 与显示管理器 → 记录 → `exit 0` | 原厂 ROM，覆盖写不进去 |
| 摘卡 | 写 `remove` 失败 | 同上；**不做任何重试性摘卡**（避免半摘卡） | 设备仍在总线 |
| 摘卡后 | `modprobe nvidia` 失败 | 必须执行 `echo 1 > /sys/bus/pci/rescan` 把卡放回，再 `modprobe` 全套 + 恢复服务 | 卡回到总线，原厂 ROM |
| 写 registry | 写入失败/长度不足 | rescan 让卡以 flash 原厂 ROM 上线 → `modprobe` 全部模块 → 恢复服务 | 原厂 |
| rescan | 失败 | 重试 3 次（每次间隔 1 s）；仍失败 → `phase=rescan_failed` + `result=failed`，提示「显卡可能不在总线上，请重启；本工具下次启动会提示恢复」 | **危险态**，见 §9.4 |
| 验证 | `power.max_limit` 与目标不符 | 判 `verify_failed`：覆盖未生效但系统可用；**不自动重试第二轮摘卡**（避免循环摘卡）；提示可手动重试或恢复原厂 | 通常仍是原厂 ROM |
| 恢复 | 拉起 lactd / 显示管理器失败 | 记录并在日志与 `last-job.json` 中明示；界面下次启动提示「显示管理器未恢复，请手动 `systemctl start gdm3`」 | 覆盖可能已生效 |

### 9.2 「恢复原厂」的确定性定义

1. `rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia`（各自重试 3 次）；
2. `modprobe nvidia`（及 `nvidia_uvm/nvidia_modeset/nvidia_drm` 若原本在用）；
3. 可选：`nvidia-smi -i <bdf> -pl <原厂上限>`（原厂值来自档案 `target` 之外的
   `power.default_limit` 探测结果，取不到则跳过并提示）；
4. 恢复 lactd 与显示管理器；
5. 验证：`power.max_limit` 回到原厂值（300 W）；
6. **不触碰** payload 文件与 `/proc/driver/nvidia/registry`（覆盖随模块卸载自然消失）。

若检测到已安装开机持久化，在同一步内提示：「开机持久化仍启用，重启后会再次注入。
要彻底回原厂，请先在『开机持久化』页卸载。」

### 9.3 与既有资产的接口契约（实现必须遵守）

**(1) `tools/derive_tables.py`** —— 只读复用，禁止 fork 一份推导逻辑。
以 `importlib.util.spec_from_file_location("v100_derive_tables", <仓库>/tools/derive_tables.py)`
载入，使用 `derive()` 与 `checksums()`。`tui/` 内不得复制任何 BIT 解析代码。

> **发现的能力缺口（t4 必须处理）**：`derive()` 的 `performance` 目前只暴露
> `entry0_3rd_u16`（字段**值**）与 `last_row`、`stride`，**没有暴露该 u16 的绝对偏移**
> （其偏移 = `last_row + entry_size + 4`，而 `entry_size` 也未被导出）。
> 因此 t4 需对 `derive_tables.py` 做**仅新增字段**的扩展：在 `performance` 中加入
> `entry_size` 与 `entry0_3rd_at`；不得改动任何既有键的名称与含义，`report()` 输出
> 保持逐字节可用。若不做此扩展，`tui/vbios_derive.py` 就不得不在 TUI 内重算公式，
> 形成第二处实现——不允许。

**(2) `tools/build_payload.py`** —— 当前是**一次性脚本**：`SRC='v100_original.rom'`、
`IMG=(0x031400,0x03e800)`、`A0/XBAR/SYS/PERF3/ROW4/GPC_*` 全部硬编码，
`XB=1912 / GP=1800 / PW=350` 也写死，文件路径与输出名固定。TUI 无法直接复用。
t4 必须（在保持 CLI 行为不变的前提下）改造为可导入模块：

```text
build_payload(rom: bytes, power_w: float | None = None, clocks: dict | None = None) -> bytes
```

* 所有偏移一律来自 `derive_tables.derive()`（禁止字面量偏移）；
* `clocks` 的键与 §5.2 的 `clocks` 字段同名；
* **回归门禁**（已实测可复现）：对 `firmware/v100_original_nvflash.rom` 以
  350 W / 1912 / 1912 / 1800 / 1800 / 1912 生成，结果必须与
  `firmware/v100_payload_350w.bin` **逐字节相同**，sha256 =
  `8163a67d23ce4de1a2feca5e72ef5b38039d1fa848a7dc08518116f6c26c8096`，
  且改动字节数恰为 **20**（与 `README.md` §三「总共 20 个字节」一致）。
* 覆盖镜像切分：`payload = rom[0xA00 : 0xA00 + 327680]`。这是**驱动接口协议常量**
  （`README.md` §三「payload 格式」），不是字段偏移，允许以字面量集中定义在
  `tui/constants.py`；生成时必须校验 `len(payload) == 327680`，且
  `derive()` 给出的所有数据镜像（`code == 0xE0`）都落在该区间内，否则拒绝生成。

**(3) `deploy/`** —— 复用，不复制。但 `v100-boot-patch.sh` 把目标值写死为 350
（`nvidia-smi -i "$GPU" -pl 350`、`[ "$MX" = "350.00" ]`），单元里 payload 路径固定
`/etc/v100/v100_final_payload.bin`，因此**任意档案的开机持久化**需要以下最小参数化
（t6 与 t3 协同，且必须保持默认行为逐字节等价）：

```text
脚本：EXPECT_W="${V100_EXPECT_W:-350}"
      · 验证改为 [ "$MX" = "$(printf '%.2f' "${EXPECT_W}")" ]
      · nvidia-smi -pl "${EXPECT_W}"
单元：在 [Service] 增加 Environment=V100_EXPECT_W=350     ← 缺省即现状
```

TUI 侧通过 drop-in `/etc/systemd/system/v100-boot-patch.service.d/10-v100-tui.conf`
写入 `Environment=V100_EXPECT_W=<目标>`，并把档案 payload 覆盖到
`/etc/v100/v100_final_payload.bin`（顺序：**先跑 `install.sh` 完整校验通过，再覆盖 payload**），
同时在 `/etc/v100/v100-tui-source.json` 记录来源档案 slug 与 sha256。
`MANIFEST.sha256` 需按 `deploy/README.md` 给出的命令重算。

若上述参数化未落地，`persist.py` **必须退化为「仅允许标准 350 W 档案安装」**并在界面
明确提示 `错误[E0504]`，**禁止**在 TUI 内另写一套注入脚本。

### 9.4 「半摘卡」状态的识别与兜底（本工具最关键的可靠性设计）

三条原则：

1. **单一失败出口**：单元脚本里只有一个 `fail()`（与 `deploy/v100-boot-patch.sh` 同构），
   所有失败分支都走它；`fail()` 内容 = 尝试 rescan → `modprobe` 全套 → 恢复 lactd 与
   显示管理器 → 写 `last-job.json` → `exit 0`。
2. **`trap EXIT` 兜底**：脚本首行即注册 `trap restore EXIT`，无论正常结束、被信号杀死、
   被 `TimeoutStartSec` 强杀，都会执行恢复序列；`restore` 必须幂等
   （重复 `rescan`/`modprobe` 是无害的）。
3. **状态先行（write-ahead）**：每个阶段先写 `state.json.phase` 再做动作。因此任何时刻
   崩溃，`state.json` 只会"超前"于现实，不会落后。

**TUI 启动时的危险态判定**（`inject.resume_hint()`）：

| `state.json.phase` | 该 BDF 是否在 `/sys/bus/pci/devices/` 下 | 判定 | 界面 |
|---|---|---|---|
| `done` / `restoring` | — | 无残留（`restoring` 表示恢复序列正在跑） | 正常进入，展示上次结果 |
| `failed`（例如卸驱动失败、验证不符） | 是 | 无残留，覆盖未生效 | 正常进入，展示「上次注入失败」摘要 |
| `failed` | 否 | **危险态** | 同上危险态页 |
| `rescan_failed` | 任意 | **危险态**（卡可能未被放回总线） | 危险态页 + 「建议重启」 |
| `card_removed` / `payload_written` / `rescanning` / `driver_unloaded` | 否 | **半摘卡危险态** | 首次进入即弹「检测到上次注入未完成，显卡可能不在总线上」页，只提供 `[恢复原厂]` 与 `[查看日志]`，并给出 `echo 1 > /sys/bus/pci/rescan` 的中文解释 |
| 同上 | 是 | 卡已回总线 | 提示「上次注入未完成但显卡已回到总线」，允许正常操作 |
| 任意 phase 但 `started_at` 早于本次开机时间（对比 `/proc/uptime` 换算的开机时刻） | — | 陈旧状态 | 视为无残留，仅提示并归档旧状态 |

`resume` 不做「自动重试注入」——危险态下唯一安全动作是**把卡放回总线并恢复原厂**，
不是再来一次。

### 9.5 并发与幂等

* `/run/v100-tui/lock`（`fcntl.flock`）：已存在运行中的注入单元时，拒绝提交第二个任务，
  提示 `错误[E0404] 已有注入任务在运行（单元 <name>），请等待其结束`。
* 重复注入同一 payload 是安全的（覆盖幂等）；重复安装持久化由 `install.sh` 保证幂等。
* 卸载持久化时若注入单元正在运行：先等待其结束（最多 30 s）再卸载，避免边注入边删文件。

---

## 10. 附录 A：字段映射表（实现必须照此取值，不得写字面量偏移）

`r` 为 `tools/derive_tables.derive(bytearray(rom))` 的返回值。

| 界面字段 | 现值读法 | 写入位置 | 编码 | 范围/规则 |
|---|---|---|---|---|
| 功耗上限 | `r["power"]["row"]["rated"] / 1000` | `r["power"]["row"]["at"] + 6` 与 `+ 10`（两个 u32 都要写） | `int(round(W*1000))` 小端 u32 | 150–350 W，且 `> r["power"]["row"]["low"]` |
| 功耗下限（可选） | `low` | `r["power"]["row"]["at"] + 2` | 同上 | 默认不改 |
| 核心上报上限 XBAR | `r["pstate"]["xbar"]["mhz"]` | `r["pstate"]["xbar"]["at"]`，`+2` 写比率 | 值：`0x8000 \| MHz`（u16）；比率：`MHz // 4`（u16） | 300–1912 MHz |
| SYS | `r["pstate"]["sys"]["mhz"]` | `r["pstate"]["sys"]["at"]`，`+2` | 同上 | 同上 |
| Applications（GPC_HDR） | `r["pstate"]["gpc_hdr"]["mhz"]` | `r["pstate"]["gpc_hdr"]["at"]`，`+2` | 值：裸 u16 `MHz`（**无 0x8000 标志**）；比率：`MHz // 4` | 300–1912 MHz |
| Applications（GPC_R1） | `r["pstate"]["gpc_r1"]["mhz"]` | `r["pstate"]["gpc_r1"]["at"]`，`+2` | 同上 | 同上 |
| Performance ENTRY0 | `r["performance"]["entry0_3rd_u16"] * 4` | `r["performance"]["entry0_3rd_at"]`（需按 §9.3(1) 扩展） | u16 `MHz // 4` | 300–1912 MHz |
| 校验和 | `checksums(d, imgs, only_end=offset)` 的返回值 | 被改动数据镜像的末字节 | 见 `derive_tables.checksums()` | 只对**唯一**包含改动字节的 `code==0xE0` 镜像重算 |

**比率一致性闸门**：写入前比较原固件的 `ratio` 与 `MHz // 4`（实测三例一致：
1530→382、1312→328、1912→478，均为向下取整）。若不一致（异常固件），默认拒绝写入并报
`错误[E0305]`，仅在显式开关 `--allow-ratio-mismatch` 下继续，且把该事实写入档案 `extra`。

**越界校验**：生成后必须复现 `derive_tables` 的严格差异检查——所有改动字节必须落在唯一
数据镜像区间内，`越界改动数 == 0`，否则报 `错误[E0306]` 并删除输出文件。

## 11. 附录 B：实现顺序与验收检查清单（t8 使用）

实现顺序：t4（数据层 + 两个 tools 的小幅扩展）→ t5（界面，可在 t4 的假数据上并行）→
t6（执行层 + 持久化参数化）→ t7（入口与联调）→ t8 全量验收。

| # | 检查项 | 命令/方法 | 期望 |
|---|---|---|---|
| 1 | 入口可用且中文 | `python3 -m tui --version`、`python3 -m tui --help` | 输出中文说明与版本，退出码 0 |
| 2 | 语法/导入 | `python3 -m compileall -q tui` | 通过，无警告级错误 |
| 3 | 单元测试 | `python3 -m unittest discover -s tui/tests -v` | 全通过，且**不需要 root** |
| 4 | payload 逐字节回归 | `test_payload.py`：从 `firmware/v100_original_nvflash.rom` 生成 350/1912/1800 | sha256 = `8163a67d…c8096`，与 `firmware/v100_payload_350w.bin` 相同，改动 20 字节 |
| 5 | 无硬编码偏移 | `grep -rnE '0x[0-9a-fA-F]{4,}' tui --include='*.py'` | 仅 `tui/constants.py` 内出现，且带来源注释 |
| 6 | 无明文密码 | `grep -rniE 'passw[o]rd|sudo[[:space:]]+-[S]' tui` | 空 |
| 7 | 无个人路径 | `grep -rnE '/ho[m]e/|/ro[o]t/|/Use[r]s/' tui` | 空（含 `DESIGN.md` 本身） |
| 8 | 非 Linux 退出 | 单测里把 `sys.platform` 打补丁为 `"win32"` 调 `main([])` | 返回 2，且 stdout 含「仅支持 Linux」 |
| 9 | 非 root 降级 | 以普通用户 `python3 -m tui` | 横幅含「只读模式」，注入与持久化入口不可用且给出原因 |
| 10 | 无 V100 降级 | 断开/隐藏 V100 时启动 | 横幅「未检测到 V100」，离线修改仍可用 |
| 11 | 注入中断图形会话的交接 | 代码审查 + 手工：注入确认后界面立即退出并打印 `journalctl` 提示 | 无「提交后仍在刷新」的假监控逻辑 |
| 12 | 半摘卡兜底 | 手工构造 `state.json` 的 `card_removed` + 摘卡状态，启动 TUI | 弹危险态页，仅「恢复原厂」「查看日志」可用 |
| 13 | 持久化复用 deploy | 代码审查：`persist.py` 只调用 `deploy/install.sh`、`deploy/uninstall.sh`，不复制注入流程 | 一致；参数化未落地时降级为「仅标准 350 W」 |
| 14 | 术语一致性 | `test_text_zh.py` + 人工抽查 | 界面与文档用词一致（§7.1） |
| 15 | 无第三方依赖 | `grep -rnE '^(import\|from) (textual\|rich\|prompt_toolkit\|urwid)' tui` | 空 |
