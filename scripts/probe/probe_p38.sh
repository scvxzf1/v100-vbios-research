#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/probe_p38.log; GPU=0000:04:00.0
exec >"$LOG" 2>&1; echo "########## $(date) P38 字段测试 ##########"
ensure() {
  echo "=== 恢复最终配置 + 桌面 ==="
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
  done
  [ -e /sys/bus/pci/devices/$GPU ] || echo 1 > /sys/bus/pci/rescan
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
  modprobe nvidia; sleep 2
  { printf 'vbios='; cat $OUT/v100_final_payload.bin; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan; sleep 6
  nvidia-smi -i $GPU -pl 350 >/dev/null 2>&1
  modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm; sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null; sleep 2
  nvidia-smi --query-gpu=index,vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "########## done ##########"
}
trap ensure EXIT
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3
for V in q0_ctrl q1_1912 q2_2000 q3_1200; do
  echo ""; echo "############ $V ############"
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
  done
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
  modprobe nvidia; sleep 2
  { printf 'vbios='; cat /tmp/p38_$V.bin; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan; sleep 7
  echo "--- 上报 (max_sm, app, max_pwr) ---"
  nvidia-smi --query-gpu=clocks.max.sm,clocks.applications.graphics,power.max_limit --format=csv,noheader -i $GPU 2>&1
  modprobe nvidia_uvm 2>/dev/null
  echo "--- 真实负载 ---"
  timeout 60 python3 $OUT/verify-tools/stress_clock.py $GPU 10 2>&1 | grep -E "SM 时钟|功耗|温度"
done
