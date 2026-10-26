#!/bin/bash
# 单次覆盖应用：写 registry -> unbind/bind V100 -> 报告
# 注意：每个 nvidia 模块生命周期内只有一次机会，所以每次试验前需 reboot
PAYLOAD="${1:?用法: apply_once.sh <payload.bin>}"
GPU=0000:04:00.0
DRV=/sys/bus/pci/drivers/nvidia

echo "=== 应用前 ==="
nvidia-smi --query-gpu=index,vbios_version,power.min_limit,power.max_limit --format=csv,noheader -i 1

echo "=== 释放占用 ==="
systemctl stop lact 2>/dev/null
systemctl stop gdm 2>/dev/null
pkill -f '^lact' 2>/dev/null
sleep 2
fuser -v /dev/nvidia1 2>&1 | sed 's/^/  /' || echo "  /dev/nvidia1 空闲"

echo "=== 写入 $(basename "$PAYLOAD") ($(stat -c%s "$PAYLOAD") 字节) ==="
{ printf 'vbios='; cat "$PAYLOAD"; } > /proc/driver/nvidia/registry
echo "  registry -> $(cat /proc/driver/nvidia/registry)"

echo "=== 重新初始化 V100 ==="
echo $GPU > $DRV/unbind && echo "  unbind ok"
sleep 1
echo $GPU > $DRV/bind && echo "  bind ok"
sleep 6

echo "=== 应用后 ==="
nvidia-smi --query-gpu=index,vbios_version,power.limit,power.min_limit,power.max_limit,power.default_limit \
    --format=csv,noheader -i 1
echo "=== dmesg ==="; dmesg | tail -6
