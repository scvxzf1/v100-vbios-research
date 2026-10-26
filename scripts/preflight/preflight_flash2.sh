#!/bin/bash
OUT=/path/to/v100-vbios-research; LOG=$OUT/preflight_flash2.log; GPU=0000:04:00.0
NV=$OUT/tools/nvflash; IMG=$OUT/v100_flashable_350w.rom
exec >"$LOG" 2>&1; echo "########## $(date) 刷写前静态校验 v2（不写入）##########"

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
  systemctl start lactd 2>/dev/null          # 恢复被我们停掉的服务
  sleep 4
  echo "最终状态:"; nvidia-smi --query-gpu=index,vbios_version,power.limit,clocks.max.sm,clocks.applications.graphics --format=csv 2>&1
  echo "桌面: $(systemctl is-active gdm3)  lactd: $(systemctl is-active lactd)"
  echo "########## done ##########"
}
trap restore EXIT

echo "=== ① 停服务（lactd 会握住 /dev/nvidia*）==="
systemctl stop lactd 2>/dev/null && echo "  lactd 已停" || echo "  lactd 停止失败"
systemctl stop gdm3 gdm 2>/dev/null; sleep 3
pkill -x lact 2>/dev/null; pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f 'Xorg vt'; pkill -f mutter; sleep 4

echo "=== ② 卸载驱动（最多重试 30 秒）==="
for i in $(seq 1 30); do
  lsmod | grep -qE "^nvidia" || break
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do rmmod $m 2>/dev/null; done
  sleep 1
done
if lsmod | grep -qE "^nvidia"; then
  echo "  !! 卸载失败，占用者："
  for d in /dev/nvidia0 /dev/nvidia1 /dev/nvidiactl /dev/nvidia-uvm; do
    [ -e "$d" ] && { echo "    $d:"; fuser -v "$d" 2>&1 | tail -n +2 | sed 's/^/      /'; }
  done
  echo "  跳过 nvflash 步骤"
  exit 1
fi
echo "  驱动已全部卸载 ✅"

echo ""; echo "=========== ③ --check：EEPROM 是否支持写入 ==========="
$NV -i 1 --check 2>&1
echo ""; echo "=========== ④ --compare：与当前 flash 逐字节对比 ==========="
$NV -i 1 --compare "$IMG" 2>&1
echo ""; echo "=========== ⑤ --save：读回 flash 与原厂备份比对 ==========="
$NV -i 1 --save /tmp/recheck_current.rom 2>&1 | tail -5
echo "--- 哈希 ---"; sha256sum /tmp/recheck_current.rom $OUT/v100_original.rom 2>&1
if cmp -s /tmp/recheck_current.rom $OUT/v100_original.rom 2>/dev/null; then
  echo "结论: 当前 flash 与 v100_original.rom 完全一致（flash 从未被写入）"
else
  echo "结论: 无法比对（文件缺失或内容不同）"
fi
