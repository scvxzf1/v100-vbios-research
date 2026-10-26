#!/bin/bash
# 应用覆盖并保留（不退回物理 ROM），最后拉起桌面
PAYLOAD="${1:-/path/to/v100-vbios-research/v100_350w_payload.bin}"
OUT=/path/to/v100-vbios-research
LOG=$OUT/apply_and_keep.log
GPU=0000:04:00.0
exec >"$LOG" 2>&1
echo "########## $(date) ##########"

ensure_desktop() {
  echo "=== 确保驱动完整 + 桌面起来 ==="
  for m in nvidia nvidia_uvm nvidia_modeset nvidia_drm; do modprobe $m 2>/dev/null; done
  sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  sleep 2
  echo "=== 最终状态 ==="
  nvidia-smi --query-gpu=index,name,vbios_version,power.limit,power.min_limit,power.max_limit,power.default_limit --format=csv 2>&1
  echo "########## done ##########"
}
trap ensure_desktop EXIT

echo "=== 停桌面 ==="
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui" ; sleep 3

echo "=== 卸驱动 ==="
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  for i in 1 2 3; do rmmod $m 2>/dev/null && { echo "  rmmod $m ok"; break; }; sleep 1; done
done
lsmod | grep -qE "^nvidia" && echo "  !!! 残留" || echo "  已清空"

echo "=== PCI remove V100 ==="
echo 1 > /sys/bus/pci/devices/$GPU/remove && echo "  ok"
sleep 2
modprobe nvidia && echo "  modprobe nvidia ok"
sleep 2

echo "=== 写 payload ==="
{ printf 'vbios='; cat "$PAYLOAD"; } > /proc/driver/nvidia/registry
echo "  $(cat /proc/driver/nvidia/registry)"

echo "=== rescan（V100 带覆盖回来）==="
echo 1 > /sys/bus/pci/rescan
sleep 8
nvidia-smi --query-gpu=index,vbios_version,power.min_limit,power.max_limit,power.default_limit --format=csv 2>&1

echo "=== 把当前限制也顶到 350 ==="
nvidia-smi -i 1 -pl 350 2>&1 | head -2
