#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/preflight3.log; GPU=0000:04:00.0
NV=$OUT/tools/nvflash; IMG=$OUT/v100_FLASH_350w_FULL.bin
exec >"$LOG" 2>&1; echo "########## $(date) 阶段1：非破坏性刷写预检 ##########"

restore() {
  echo ""; echo "=== 恢复 ==="
  [ -e /sys/bus/pci/devices/$GPU ] || echo 1 > /sys/bus/pci/rescan
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
  modprobe nvidia; sleep 2
  { printf 'vbios='; cat $OUT/v100_final_payload.bin; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan; sleep 6
  nvidia-smi -i $GPU -pl 350 >/dev/null 2>&1
  modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm; sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  systemctl start lactd 2>/dev/null
  sleep 4
  echo "最终状态:"; nvidia-smi --query-gpu=index,vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "桌面: $(systemctl is-active gdm3)   lactd: $(systemctl is-active lactd)"
  echo "########## done ##########"
}
trap restore EXIT

echo "=== ① 停服（lactd 会握住 /dev/nvidia*）==="
systemctl stop lactd 2>/dev/null && echo "  lactd 已停"
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -x lact 2>/dev/null; pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f mutter; sleep 4

echo "=== ② 卸载驱动 ==="
for i in $(seq 1 30); do
  lsmod | grep -qE "^nvidia" || break
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do rmmod $m 2>/dev/null; done
  sleep 1
done
if lsmod | grep -qE "^nvidia"; then
  echo "  !! 卸载失败"; for d in /dev/nvidia0 /dev/nvidia1 /dev/nvidiactl /dev/nvidia-uvm; do
    [ -e "$d" ] && { echo "    $d:"; fuser -v "$d" 2>&1 | tail -n +2 | sed 's/^/      /'; }; done
  exit 1
fi
echo "  驱动已卸载 ✅"

echo ""; echo "=========== ③ 适配器列表 ==========="
$NV --list 2>&1 | grep -vE "^$|Copyright|Firmware Update"
echo ""; echo "=========== ④ --protectoff（只改写保护位，不动固件）==========="
$NV -i 1 --protectoff 2>&1
echo ""; echo "=========== ⑤ --check：写保护是否解除 ==========="
$NV -i 1 --check 2>&1
echo ""; echo "=========== ⑥ --compare：与当前 flash 对比 ==========="
$NV -i 1 --compare "$IMG" 2>&1
