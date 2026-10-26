#!/bin/bash
# 脱离会话：测试覆盖 payload。停桌面 -> 卸驱动 -> PCI remove -> modprobe -> 写 registry -> rescan -> 探测 -> 恢复
PAYLOAD="${1:?用法: try_payload.sh <payload.bin>}"
OUT=/path/to/v100-vbios-research
LOG=$OUT/try_payload.log
GPU=0000:04:00.0
exec >"$LOG" 2>&1
echo "########## $(date) payload=$(basename "$PAYLOAD") ##########"

restore() {
  echo "=== 恢复：回到物理 ROM + 拉起桌面 ==="
  rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null
  sleep 2
  modprobe nvidia; modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm
  sleep 3
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  echo "=== 恢复后 ==="
  nvidia-smi --query-gpu=index,name,vbios_version,power.min_limit,power.max_limit --format=csv 2>&1
  echo "########## done ##########"
}
trap restore EXIT

echo "=== 停桌面 ==="
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; sleep 3

echo "=== 卸驱动 ==="
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  for i in 1 2 3; do rmmod $m 2>/dev/null && { echo "  rmmod $m ok"; break; }; sleep 1; done
done
lsmod | grep -qE "^nvidia" && echo "  !!! 残留模块" || echo "  驱动已全部卸载"

echo "=== PCI remove V100 ==="
echo 1 > /sys/bus/pci/devices/$GPU/remove && echo "  remove ok"
sleep 2
modprobe nvidia && echo "  modprobe nvidia ok (仅 1050)"
sleep 2

echo "=== 写入 $(basename "$PAYLOAD") ($(stat -c%s "$PAYLOAD") 字节) ==="
{ printf 'vbios='; cat "$PAYLOAD"; } > /proc/driver/nvidia/registry
echo "  registry -> $(cat /proc/driver/nvidia/registry)"

echo "=== rescan ==="
echo 1 > /sys/bus/pci/rescan
sleep 8

echo "=== 结果 ==="
nvidia-smi --query-gpu=index,name,vbios_version,power.limit,power.min_limit,power.max_limit,power.default_limit \
   --format=csv 2>&1
echo "=== dmesg ==="; dmesg | tail -8
