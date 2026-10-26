#!/bin/bash
# 注: 原脚本内嵌明文 root 密码, 开源版已改为从环境变量读取。
#     使用前请先: export ROOT_PASSWORD="你的root密码"
# V100 VBIOS 安全加载测试脚本
# 设备: 0000:04:00.0 (Tesla V100, 无显示输出)

set -e

VBIOS_ROM="$1"
GPU_BUS="0000:04:00.0"
GPU_INDEX=1
PASSWORD="${ROOT_PASSWORD:?请先导出 ROOT_PASSWORD 环境变量}"

if [ -z "$VBIOS_ROM" ]; then
    echo "用法: $0 <vbios.rom>"
    exit 1
fi

if [ ! -f "$VBIOS_ROM" ]; then
    echo "✗ 文件不存在: $VBIOS_ROM"
    exit 1
fi

echo "=========================================="
echo "V100 VBIOS 安全加载测试"
echo "=========================================="
echo "目标设备: $GPU_BUS (GPU $GPU_INDEX)"
echo "VBIOS 文件: $VBIOS_ROM"
echo "文件大小: $(stat -c%s "$VBIOS_ROM") 字节"
echo ""

# 阶段 0: 备份当前状态
echo "[阶段 0] 备份当前 GPU 状态"
nvidia-smi -i $GPU_INDEX -q > gpu_state_before.txt
echo "✓ 状态已保存到 gpu_state_before.txt"
echo ""

# 阶段 1: 检查 registry 接口
echo "[阶段 1] 检查驱动 registry 接口"
REGISTRY_PATH="/proc/driver/nvidia/gpus/$GPU_BUS/registry"

if [ ! -f "$REGISTRY_PATH" ]; then
    echo "✗ Registry 接口不存在: $REGISTRY_PATH"
    echo "当前驱动可能不支持 registry override"
    ls -la /proc/driver/nvidia/gpus/$GPU_BUS/
    exit 1
fi

echo "✓ Registry 接口存在: $REGISTRY_PATH"
echo "当前权限: $(ls -l $REGISTRY_PATH)"
echo ""

# 阶段 2: 读取当前 registry（如果可能）
echo "[阶段 2] 尝试读取当前 registry"
echo "$PASSWORD" | su - root -c "head -c 100 $REGISTRY_PATH 2>&1 | hexdump -C" || echo "（可能不支持读取）"
echo ""

# 阶段 3: 询问用户确认
echo "[阶段 3] 准备写入修改后的 VBIOS"
echo ""
echo "⚠️  注意事项:"
echo "  1. 这会替换 GPU $GPU_INDEX 的运行时 VBIOS"
echo "  2. GPU 可能重新初始化或进入保护模式"
echo "  3. 如果失败，重启系统即可恢复"
echo "  4. 你的显示由 GTX 1050 负责，不受影响"
echo ""
read -p "继续? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
    echo "已取消"
    exit 0
fi

# 阶段 4: 写入 VBIOS
echo ""
echo "[阶段 4] 写入 VBIOS 到 registry"
echo "$PASSWORD" | su - root -c "cat $VBIOS_ROM > $REGISTRY_PATH"

if [ $? -eq 0 ]; then
    echo "✓ VBIOS 已写入 registry"
else
    echo "✗ 写入失败"
    exit 1
fi

echo ""
echo "等待 5 秒让 GPU 稳定..."
sleep 5

# 阶段 5: 验证 GPU 状态
echo ""
echo "[阶段 5] 验证 GPU 状态"

if nvidia-smi -i $GPU_INDEX &>/dev/null; then
    echo "✓ GPU 仍然可访问"
    
    echo ""
    echo "查询当前功耗上限:"
    nvidia-smi -i $GPU_INDEX --query-gpu=power.limit --format=csv,noheader,nounits
    
    echo ""
    echo "完整状态:"
    nvidia-smi -i $GPU_INDEX -q | grep -A 10 "Power"
    
    nvidia-smi -i $GPU_INDEX -q > gpu_state_after.txt
    echo ""
    echo "✓ 新状态已保存到 gpu_state_after.txt"
    
    echo ""
    echo "对比功耗上限:"
    echo "修改前: $(grep "Power Limit" gpu_state_before.txt | head -1)"
    echo "修改后: $(grep "Power Limit" gpu_state_after.txt | head -1)"
else
    echo "✗ GPU 不可访问！"
    echo "驱动可能崩溃或 GPU 进入保护模式"
    echo "建议: 重启系统恢复"
    exit 1
fi

echo ""
echo "=========================================="
echo "测试完成"
echo "=========================================="
echo ""
echo "如果功耗上限已改变，说明修改生效！"
echo "如果需要恢复原始 VBIOS，运行:"
echo "  $0 v100_vbios.rom"
echo "或重启系统"
