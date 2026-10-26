#!/bin/bash
OUT=/path/to/v100-vbios-research
LOG=$OUT/probe_clocks2.log
GPU=0000:04:00.0
exec >"$LOG" 2>&1
echo "########## $(date) 第二轮：探测时钟上限 ##########"

restore() {
  echo "=== 恢复原厂 + 桌面 ==="
  rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null
  sleep 2
  [ -e /sys/bus/pci/devices/$GPU ] || echo 1 > /sys/bus/pci/rescan
  modprobe nvidia; modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm
  sleep 3
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  nvidia-smi --query-gpu=index,vbios_version,clocks.max.sm,clocks.applications.graphics,power.max_limit --format=csv 2>&1
  echo "########## done ##########"
}
trap restore EXIT

echo "=== 停桌面 ==="
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3

for V in w1_x1700 w2_x1800 w3_x1912 w4_x1912g1912 w5_x2000; do
  echo ""
  echo "############ $V ############"
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
  done
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null
  sleep 1
  modprobe nvidia
  sleep 2
  { printf 'vbios='; cat /tmp/clk2_$V.bin; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan
  sleep 6
  echo "--- 上报值 (vbios, max_sm, app, default_app, max_pwr) ---"
  nvidia-smi --query-gpu=vbios_version,clocks.max.sm,clocks.applications.graphics,clocks.default_applications.graphics,power.max_limit --format=csv,noheader -i $GPU 2>&1
  modprobe nvidia_uvm 2>/dev/null
  echo "--- 真实负载 ---"
  timeout 60 python3 $OUT/verify-tools/stress_clock.py $GPU 10 2>&1 | grep -E "SM 时钟|功耗|温度|判定|压测前"
  dmesg | tail -3 | sed 's/^/    dmesg: /'
done
