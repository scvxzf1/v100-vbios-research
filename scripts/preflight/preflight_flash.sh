#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/preflight_flash.log; GPU=0000:04:00.0
NV=$OUT/tools/nvflash; IMG=$OUT/v100_flashable_350w.rom
exec >"$LOG" 2>&1; echo "########## $(date) 刷写前静态校验（不写入）##########"

restore() {
  echo ""; echo "=== 恢复最终配置 + 桌面 ==="
  [ -e /sys/bus/pci/devices/$GPU ] || echo 1 > /sys/bus/pci/rescan
  echo 1 > /sys/bus/pci/devices/$GPU/remove 2>/dev/null; sleep 1
  modprobe nvidia; sleep 2
  { printf 'vbios='; cat $OUT/v100_final_payload.bin; } > /proc/driver/nvidia/registry
  echo 1 > /sys/bus/pci/rescan; sleep 6
  nvidia-smi -i $GPU -pl 350 >/dev/null 2>&1
  modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm; sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null; sleep 3
  echo "最终状态:"
  nvidia-smi --query-gpu=index,vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "########## done ##########"
}
trap restore EXIT

systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f Xorg; pkill -f mutter; pkill -f "lact gui"; sleep 3
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  for i in 1 2 3; do rmmod $m 2>/dev/null && break; sleep 1; done
done
lsmod | grep -qE "^nvidia" && echo "!! 仍有模块残留" || echo "驱动已卸载"

echo ""; echo "=========== 1) --check：EEPROM 是否支持写入 ==========="
$NV -i 1 --check 2>&1

echo ""; echo "=========== 2) --compare：与当前 flash 逐字节对比 ==========="
$NV -i 1 --compare "$IMG" 2>&1

echo ""; echo "=========== 3) --save：重新读回 flash 并比对原厂备份 ==========="
$NV -i 1 --save /tmp/recheck_current.rom 2>&1 | tail -6
echo "--- 哈希 ---"
sha256sum /tmp/recheck_current.rom $OUT/v100_original.rom
if cmp -s /tmp/recheck_current.rom $OUT/v100_original.rom; then
  echo "结论: 当前 flash 内容与 v100_original.rom 完全一致（flash 从未被改动）"
else
  echo "警告: 当前 flash 与备份不一致！"
fi
