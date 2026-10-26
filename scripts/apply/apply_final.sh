#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/apply_final.log; GPU=0000:04:00.0
PAY=$OUT/v100_final_payload.bin
exec >"$LOG" 2>&1; echo "########## $(date) 最终配置应用 ##########"

ensure() {
  echo "=== 确保驱动完整 + 桌面 ==="
  for m in nvidia nvidia_uvm nvidia_modeset nvidia_drm; do modprobe $m 2>/dev/null; done
  sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  sleep 2
  echo "=== 最终状态（不重载驱动，覆盖保留）==="
  nvidia-smi --query-gpu=index,name,vbios_version,power.limit,power.min_limit,power.max_limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "########## done ##########"
}
trap ensure EXIT

systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3

for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
done
echo "=== 应用最终 payload ==="
echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
modprobe nvidia; sleep 2
{ printf 'vbios='; cat "$PAY"; } > /proc/driver/nvidia/registry
echo 1 > /sys/bus/pci/rescan; sleep 6
nvidia-smi --query-gpu=vbios_version,power.min_limit,power.max_limit,clocks.max.sm,clocks.applications.graphics --format=csv -i $GPU 2>&1
nvidia-smi -i $GPU -pl 350 2>&1 | head -1
modprobe nvidia_uvm 2>/dev/null

echo ""
echo "=== 30 秒验收压测 ==="
timeout 120 python3 $OUT/verify-tools/stress_clock.py $GPU 30 2>&1 | grep -E "压测前|SM 时钟|功耗|温度|判定"
