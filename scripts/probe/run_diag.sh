#!/bin/bash
OUT=/path/to/v100-vbios-research
LOG=$OUT/run_diag.log
GPU=0000:04:00.0
exec >"$LOG" 2>&1
echo "########## $(date) 瓶颈诊断 ##########"

restore() {
  echo "=== 恢复原厂 + 桌面 ==="
  rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null
  sleep 2
  [ -e /sys/bus/pci/devices/$GPU ] || echo 1 > /sys/bus/pci/rescan
  modprobe nvidia; modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm
  sleep 3
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  nvidia-smi --query-gpu=index,vbios_version,clocks.max.sm,power.max_limit --format=csv 2>&1
  echo "########## done ##########"
}
trap restore EXIT

echo "=== 停桌面 ==="
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3

for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
done

echo "=== 应用 c2 (350W + XBAR/GPC 1912) ==="
echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null
sleep 1
modprobe nvidia; sleep 2
{ printf 'vbios='; cat /tmp/clk2_c2_350w_x1912.bin; } > /proc/driver/nvidia/registry
echo 1 > /sys/bus/pci/rescan
sleep 6
nvidia-smi --query-gpu=vbios_version,power.limit,power.max_limit,clocks.max.sm,clocks.applications.graphics --format=csv -i $GPU 2>&1
nvidia-smi -i $GPU -pl 350 2>&1 | head -1
modprobe nvidia_uvm 2>/dev/null

echo ""
echo "=== 负载诊断（25s）==="
timeout 90 python3 $OUT/verify-tools/diag_throttle.py $GPU 25 2>&1
