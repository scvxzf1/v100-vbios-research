#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/probe_mem2.log; GPU=0000:04:00.0
exec >"$LOG" 2>&1; echo "########## $(date) 显存下调探测 ##########"

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
  modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm
  sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  sleep 2
  nvidia-smi --query-gpu=index,vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "########## done ##########"
}
trap ensure EXIT

systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3

for V in d0_stock d1_data600 d2_code600 d3_both600 d4_all600; do
  echo ""; echo "############ $V ############"
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
  done
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
  modprobe nvidia; sleep 2
  { printf 'vbios='; cat /tmp/md_$V.bin; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan; sleep 7
  echo "--- 覆盖是否被接受（版本号应仍为 88.00.4F.00.09）---"
  nvidia-smi --query-gpu=vbios_version --format=csv,noheader -i $GPU 2>&1
  echo "--- 显存上报 ---"
  nvidia-smi --query-gpu=clocks.current.memory,clocks.max.memory,clocks.sm --format=csv,noheader -i $GPU 2>&1
  echo "--- 支持档位 ---"
  nvidia-smi -q -d SUPPORTED_CLOCKS -i $GPU 2>/dev/null | grep -E "^\s+Memory" | head -2
  modprobe nvidia_uvm 2>/dev/null
  echo "--- 实际带宽 ---"
  timeout 90 python3 $OUT/verify-tools/mem_bw.py $GPU 3 2>&1 | grep -E "显存带宽|设备"
  echo "--- ECC ---"
  nvidia-smi -q -d ECC -i $GPU 2>/dev/null | grep -A2 "Volatile" | head -4
done
