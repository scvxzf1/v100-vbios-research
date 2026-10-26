"""TUI 数据层共享常量。

**本文件是全项目唯一允许出现十六进制字面量的 Python 模块**（tui/DESIGN.md §11 第 5 项
检查项）。其它模块一律引用这里的名字，禁止在别处写字段偏移或协议数值。

每条常量后都注明来源；来源无法追溯的数值不得加入本文件。
"""
from __future__ import annotations

from pathlib import Path

# ── 仓库位置（相对本文件推导，不含任何个人绝对路径）─────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
FIRMWARE_DIR = REPO_ROOT / "firmware"

# ── 驱动接口协议（来源: README.md §三「payload 格式」）────────────────────────
REGISTRY_PATH = "/proc/driver/nvidia/registry"     # 驱动 registry 二进制键接口
PAYLOAD_WINDOW_START = 0x0A00                      # 覆盖镜像在 ROM 内的起始偏移
PAYLOAD_WINDOW_LENGTH = 0x50000                    # 覆盖镜像固定长度 327,680 字节

# ── P-State 字段编码（来源: tools/derive_tables.py 的 flag = raw & 0x8000；
#                     docs/03-时钟墙实测.md 的 XBAR 写入方式）─────────────────
PSTATE_FLAG = 0x8000
PSTATE_VALUE_MASK = 0x7FFF
CLOCK_RATIO_DIVISOR = 4          # 比率字段 = MHz // 4（实测 1530→382、1312→328、1912→478）

# ── PCI 标识（来源: nvidia-smi -q 与 sysfs；Tesla V100-SXM2-16GB = 10de:1db1）──
VENDOR_ID_NVIDIA = 0x10DE
DEVICE_ID_V100_SXM2 = 0x1DB1
SYSFS_PCI_DEVICES = "/sys/bus/pci/devices"

# ── 数值范围（来源: tui/DESIGN.md §3.1 字段表与 §6.5 输入校验）────────────────
POWER_MIN_W = 150.0
POWER_MAX_W = 350.0
CLOCK_MIN_MHZ = 300
CLOCK_MAX_MHZ = 1912
WARN_CLOCK_MHZ = 1785            # 出厂电压下的物理上限（docs/03 实测结论）
POWER_DECIMALS = 1

# ── 系统路径（来源: tui/DESIGN.md §5.1、§9.3；deploy/ 既有资产）───────────────
RUN_DIR = "/run/v100-tui"
LOG_DIR = "/var/log/v100-tui"
ETC_V100_DIR = "/etc/v100"
BOOT_UNIT = "v100-boot-patch.service"
BOOT_PAYLOAD_PATH = "/etc/v100/v100_final_payload.bin"
BOOT_LOG_PATH = "/var/log/v100-boot-patch.log"
BOOT_DROPIN_DIR = "/etc/systemd/system/v100-boot-patch.service.d"
BOOT_EXPECT_ENV_KEY = "V100_EXPECT_W"      # deploy 参数化键名（见 DESIGN §9.3(3)）
LACTD_UNIT = "lactd"
DISPLAY_MANAGER_UNITS = ("gdm3", "gdm", "sddm", "lightdm")

# ── 外部命令超时（秒），避免 TUI 卡死在半截 ─────────────────────────────────
TIMEOUT_SMI_S = 10
TIMEOUT_SYSTEMCTL_S = 5
TIMEOUT_ROM_DUMP_S = 90

# ── XDG 布局（来源: tui/DESIGN.md §5.1；禁止个人绝对路径）─────────────────────
XDG_CONFIG_HOME = "XDG_CONFIG_HOME"
XDG_CACHE_HOME = "XDG_CACHE_HOME"
APP_DIR_NAME = "v100-tui"
PROFILES_SUBDIR = "profiles"
PAYLOADS_SUBDIR = "payloads"
HISTORY_FILENAME = "history.jsonl"
LEGACY_PROFILES_FILENAME = "profiles.json"   # 旧版单文件布局，仅用于导入迁移

# ── 配置档案 schema（来源: tui/DESIGN.md §5.2、§5.3）─────────────────────────
PROFILE_KIND = "v100-tui.profile"
PROFILE_VERSION = 1
SLUG_MAX_LEN = 32
SLUG_DEFAULT_PREFIX = "profile"

# ── 错误码（来源: tui/DESIGN.md §7.2 的编号分区；文案由 t5 的 text_zh.py 负责）
#    E01xx 环境与权限 / E02xx ROM / E03xx 覆盖镜像 / E04xx 注入 / E05xx 持久化 / E06xx 档案
ERR_PLATFORM = "E0101"
ERR_NOT_ROOT = "E0102"
ERR_INJECT_DISABLED = "E0103"
ERR_REGISTRY_MISSING = "E0104"
ERR_ROM_READ = "E0201"
ERR_DERIVE = "E0202"
ERR_ROM_TOO_SHORT = "E0203"
ERR_RANGE = "E0302"
ERR_RATIO = "E0305"
ERR_OUTSIDE = "E0306"
ERR_BUILD = "E0307"
ERR_PROFILE_CORRUPT = "E0601"
ERR_PROFILE_VERSION = "E0603"
ERR_PROFILE_INVALID = "E0604"
ERR_PROFILE_IO = "E0605"
ERR_HISTORY_IO = "E0606"

# ── 提示码（不阻断操作，来源: tui/DESIGN.md §3.1、§6.5、§7.2）
WARN_SAME_AS_CURRENT = "W0303"      # 目标值与现值相同
WARN_CLOCK_PHYSICAL = "W0310"       # 高于 1785 MHz 物理上限
WARN_PERF_ALIGN = "W0311"           # ENTRY0 按 4 MHz 对齐
WARN_ROM_MISMATCH = "W0602"         # 档案与当前基准 ROM 不一致

# 已知「加和校验和非零」的尾段镜像类型（本固件 0x3FA00..0x50A00 段基线即非零，
# 来源: tools/derive_tables.py 的 report/回读输出），比对校验和时排除
IMG_TYPE_NO_ADDITIVE_CKSUM = 0x70

# ── 语义字段名 ───────────────────────────────────────────────────────────────
FIELD_POWER = "power_limit_w"
FIELD_XBAR = "pstate_xbar_mhz"
FIELD_SYS = "pstate_sys_mhz"
FIELD_GPC_HDR = "pstate_gpc_hdr_mhz"
FIELD_GPC_R1 = "pstate_gpc_r1_mhz"
FIELD_PERF0 = "performance_entry0_mhz"

CLOCK_FIELDS = (FIELD_XBAR, FIELD_SYS, FIELD_GPC_HDR, FIELD_GPC_R1, FIELD_PERF0)
