# 贡献指南

感谢你愿意为 **Tesla V100 VBIOS 研究**（`v100-vbios-research`）贡献内容。本仓库是一份**硬件侧黑盒研究记录 + 可复现工具集**，因此对贡献的要求与普通软件项目略有不同：**正确性、可复现性和"不把人带进坑里"** 比功能数量更重要。

> ⚠️ **动手前请先读 [`NOTICE`](NOTICE) 与 [`docs/06-刷写尝试与教训.md`](docs/06-刷写尝试与教训.md)。** 本项目涉及卸载内核驱动、操作 PCI 总线等高风险动作，并且**已经真实发生过一次芯片被擦空的事故**。

---

## 一、可以贡献什么

| 类型 | 说明 | 需要附带的材料 |
|---|---|---|
| 新固件版本上的复现结果 | 在**别的** V100（或别的 GV100 卡）上重跑本方案 | 固件版本号、`tools/derive_tables.py` 输出、`nvidia-smi -q` 关键字段 |
| 证伪 / 修正结论 | 推翻或修正文档里的某条结论 | 原始命令、完整日志、对照组数据 |
| 工具改进 | `tools/` 下的解析/构建/压测脚本 | 改动理由 + 改前改后对比输出 |
| 文档修正 | 错别字、失效链接、口径不一致 | 说明改哪一行、改成什么 |
| 事故记录 | 新的失败模式与恢复步骤 | 现象、日志、最终是否恢复、如何恢复 |

**不接受**：无法复现的性能数字、只有结论没有命令的"实测"、来源不明的固件镜像。

---

## 二、提交规范

### 2.1 分支与提交

1. 从 `main` 切分支：`git switch -c fix/<简短说明>` 或 `feat/<简短说明>`。
2. 一个提交只做一件事；文档修正与代码改动尽量分开。
3. 提交信息格式（中文即可，前缀用英文）：

   ```
   <type>: <一句话说明>

   <可选：为什么这么做、影响范围、验证方式>
   ```

   `type` 取值：`fix`（修 bug / 修错字 / 修失效链接）、`feat`（新功能）、`docs`（仅文档）、`refactor`、`chore`。

   示例：

   ```
   docs: 把 docs 内的旧文件名引用改为 01-..99- 编号文件名

   文档改名为编号前缀后，docs 内部 22 处交叉引用失效。
   本次统一改为同目录相对链接，并补充 README 文档索引表。
   验证：全仓 Markdown 链接均可解析（见附录命令）。
   ```

### 2.2 提交前自检（必须全部通过）

```bash
# 1) 不得出现明文密码 / 个人路径 / 个人用户名
#    说明：说明性文档（本文件、SECURITY.md、docs/00-整理清单.md）为了描述规则本身会包含
#    这些词的示例，故排除；其余文件命中即需修正。
grep -rniE --binary-files=without-match \
  "password=|passwd=|/home/|~/(下载|Desktop|Documents)|PRIVATE KEY|ssh-rsa" . \
  --exclude-dir=.git --exclude=CONTRIBUTING.md --exclude=SECURITY.md --exclude=00-整理清单.md

# 2) 文档互链必须都能解析到真实文件（输出应为空）
python3 - <<'EOF'
import re, os, glob
for f in sorted(glob.glob('**/*.md', recursive=True)):
    base = os.path.dirname(f)
    infence = False
    for i, l in enumerate(open(f, encoding='utf-8').read().splitlines(), 1):
        if l.lstrip().startswith('```'):
            infence = not infence
            continue
        if infence:
            continue
        for m in re.finditer(r'\[[^\]]*\]\(([^)]+)\)', l):
            t = m.group(1)
            if t.startswith(('http://', 'https://', '#')):
                continue
            p = os.path.normpath(os.path.join(base, t.split('#')[0]))
            if not os.path.exists(p):
                print(f'{f}:{i}  BROKEN  {t}')
EOF

# 3) 不得提交固件二进制的新增/改动（firmware/ 仅保留既有存档）
git status --porcelain firmware/

# 4) 若改了 deploy/ 下被 MANIFEST 覆盖的文件，必须同步重算清单
cd deploy && sha256sum -c MANIFEST.sha256
```

### 2.3 硬性禁止事项

- **禁止提交明文密码、令牌、私钥。** 需要 root 的脚本一律从环境变量读取（`ROOT_PASSWORD`），见 `scripts/README.md`。
- **禁止提交个人路径**（`/home/<用户名>`、`~/下载`、`~/Desktop` 等）。仓库内统一用 `/path/to/v100-vbios-research` 占位符。
- **禁止新增或修改 `firmware/` 下的固件镜像。** 该目录是证据存档，改一个字节哈希就变，`NOTICE` 与 `firmware/README.md` 里的校验值全部失效。
- **禁止在脚本里把设备地址写死为可执行的默认值后不加提示**；写死可以，但必须在注释里标出"请按本机环境修改"。
- **禁止把"失败路径会阻塞开机"的改动合入** `deploy/v100-boot-patch.sh`：任何失败都必须回退原厂状态并 `exit 0`。

---

## 三、如何复现实验

### 3.1 前置条件

- Linux + NVIDIA 闭源驱动（本项目在 `580.178.04`、内核 `6.8.0-136-generic` 上验证）
- Tesla V100（本项目为 SXM2-16GB，固件 `88.00.4F.00.09`）
- `nvidia-smi` 可用；**nvflash 需自行获取**（NVIDIA 专有，本项目未收录，见 `NOTICE`）
- 压测工具需要 CUDA 驱动 + `libcuda.so`（不需要 `nvcc` / cuBLAS / PyTorch）

### 3.2 最小复现路径

```bash
# 1) 取原厂固件（需先卸载驱动；V100 无 ROM BAR，只能走 nvflash）
sudo nvflash -i 1 --save my_v100.rom

# 2) 查看结构，确认表位置（不要跳过）
python3 tools/derive_tables.py my_v100.rom

# 3) 生成 payload
python3 tools/build_payload.py my_v100.rom

# 4) 按 docs/05-注入点与初始化.md 的流程注入（摘卡 → 写 registry → rescan）

# 5) 验证
nvidia-smi --query-gpu=power.limit,clocks.max.sm --format=csv -i 1
python3 tools/stress_clock.py 0000:04:00.0 30
```

> ⚠️ **不要把 `scripts/` 下的脚本当工具直接跑。** 它们是研究过程的一次性存档，`probe_*` / `apply_*` /
> `preflight_*` 都会 `rmmod` 内核驱动、摘掉 PCI 设备，**执行即中断图形会话**。需要长期生效请用
> `deploy/` 的 systemd 单元；提交复现结果时也请说明你是按哪份文档的哪一节操作的，而不是"跑了某个脚本"。

### 3.3 提交复现结果时的模板

```markdown
- 硬件：Tesla V100-<型号>
- 固件版本：88.xx.xx.xx.xx（`nvidia-smi -q | grep VBIOS`）
- 驱动 / 内核：
- 操作：<照哪个文档的第几节>
- 结果：`power.limit=` / `clocks.max.sm=` / 实测算力与功耗
- 日志：<贴关键片段，**先删除序列号与 UUID**>
- 是否可完全回退：是/否
```

---

## 四、硬件风险提示（务必阅读）

| 操作 | 风险 | 必须的预防措施 |
|---|---|---|
| 内存态注入（本项目主方案） | 低：不写 flash，`rmmod` 或重启即回原厂 | 保留一份**已验证哈希**的原厂 ROM |
| 物理刷写 VBIOS | **高**：本项目已发生一次芯片被完全擦空（读回全 `0xFF`） | 1 MiB **完整**编程器备份 + CH341A 等编程器 + 3.3 V 供电 |
| 改功耗上限（300 → 350 W） | 中：热负荷 +17%，被动散热卡需确认风道 | 满载前确认进风温度；长期使用建议留 1530 MHz 能效档 |
| 长时间满载压测 | 中：加速硬件老化 | 监控温度与节流原因，不做无人值守的长跑 |
| 卸载内核驱动 / 摘 PCI 设备 | 中：会中断图形会话，偶发阻塞 | 用 `systemd-run --unit=... --no-block` 提交为独立单元 |

**本仓库作者不对显卡损坏、变砖、保修失效或任何连带损失承担责任。** 提交 issue / PR 时也请假设读者会照着做——请把你踩过的坑写清楚。

---

## 五、代码风格

- Python：Python 3 标准库优先，能用 `argparse` 暴露的输入就别写死；表偏移一律**动态解析**，不要新增硬编码偏移量。
- Shell：`#!/bin/bash` + `set -eu`；所有写系统状态的路径必须先检查存在性；失败分支要有明确输出。
- 文档：简体中文；命令、路径、字段名保留英文原文；跨文档引用一律使用 Markdown 相对链接（不要只写反引号文件名）。

---

## 六、许可

- 代码与文档：以 [Apache License 2.0](LICENSE) 提交，即表示你同意以该许可发布你的贡献。
- `firmware/` 下的固件镜像不适用该许可，版权归 NVIDIA，详见 [`NOTICE`](NOTICE)。
