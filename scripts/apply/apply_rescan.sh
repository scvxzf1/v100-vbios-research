#!/bin/bash
# 覆盖应用（无需重启）：rmmod -> PCI remove V100 -> modprobe nvidia -> 写 registry -> rescan
PAYLOAD="${1:?用法: apply_rescan.sh <payload.bin>}"
GPU=0000:04:00.0
echo "  payload: $(basename "$PAYLOAD") ($(stat -c%s "$PAYLOAD") 字节)"
systemctl stop lact 2>/dev/null; pkill -f '^lact' 2>/dev/null
rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null
sleep 1
echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null
sleep 1
modprobe nvidia
sleep 2
{ printf 'vbios='; cat "$PAYLOAD"; } > /proc/driver/nvidia/registry
echo 1 > /sys/bus/pci/rescan
sleep 8
nvidia-smi --query-gpu=index,vbios_version,power.limit,power.min_limit,power.max_limit,power.default_limit \
   --format=csv,noheader -i 1
