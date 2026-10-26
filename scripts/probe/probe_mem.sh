#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/probe_mem.log; GPU=0000:04:00.0
exec >"$LOG" 2>&1; echo "########## $(date) 显存时钟探测 ##########"

ensure() {
  echo "=== 确保驱动 + 桌面 ==="
  for m in nvidia nvidia_uvm nvidia_modeset nvidia_drm; do modprobe $m 2>/dev/null; done
  sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  sleep 2
  nvidia-smi --query-gpu=index,vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "########## done ##########"
}
trap ensure EXIT

systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3

apply() {   # $1 = payload
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
  done
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
  modprobe nvidia; sleep 2
  { printf 'vbios='; cat "$1"; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan; sleep 6
  nvidia-smi -i $GPU -pl 350 >/dev/null 2>&1
  modprobe nvidia_uvm 2>/dev/null
}

for V in m0_base m1_1000_min m2_1000_full m3_1126; do
  echo ""; echo "############ $V ############"
  apply /tmp/mem_$V.bin
  echo "--- 上报 ---"
  nvidia-smi --query-gpu=clocks.current.memory,clocks.max.memory,clocks.max.sm --format=csv,noheader -i $GPU 2>&1
  echo "--- 支持的显存档位 ---"
  nvidia-smi -q -d SUPPORTED_CLOCKS -i $GPU 2>/dev/null | grep -A1 "Memory" | head -4
  echo "--- 实际带宽 ---"
  timeout 90 python3 $OUT/verify-tools/mem_bw.py $GPU 4 2>&1 | grep -E "显存带宽|设备"
  echo "--- ECC 错误 ---"
  nvidia-smi -q -d ECC -i $GPU 2>/dev/null | grep -E "Volatile|Aggregate" -A2 | head -6
done

echo ""
echo "=== 恢复最终配置（350W + XBAR1912 + GPC1800）==="
apply $OUT/v100_final_payload.bin
nvidia-smi --query-gpu=vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv -i $GPU 2>&1
