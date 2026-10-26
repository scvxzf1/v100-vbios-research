#!/bin/bash
# 脱离会话执行：停桌面 -> 卸驱动 -> nvflash 备份 -> 恢复驱动与桌面
OUT=/path/to/v100-vbios-research
LOG=$OUT/nvflash_save.log
NV=/tmp/nvflash_x/x64/nvflash
exec >"$LOG" 2>&1
echo "########## $(date) ##########"

restore() {
  echo "=== 恢复驱动与桌面 ==="
  modprobe nvidia; modprobe nvidia_uvm; modprobe nvidia_modeset; modprobe nvidia_drm
  sleep 2
  systemctl start gdm3 2>/dev/null || systemctl start gdm 2>/dev/null
  echo "=== 恢复后 ==="
  nvidia-smi --query-gpu=index,name,vbios_version --format=csv 2>&1 | head -5
  chown -R "${SUDO_USER:-$USER}" "$OUT"/v100_original.rom "$LOG" 2>/dev/null
  echo "########## done ##########"
}
trap restore EXIT

echo "=== 停桌面 ==="
systemctl stop gdm3 gdm 2>/dev/null
sleep 3
pkill -f gnome-shell; pkill -f gnome-remote-desktop; pkill -f "Xorg"; pkill -f mutter
sleep 3

echo "=== 卸驱动 ==="
for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
  for i in 1 2 3; do rmmod $m 2>/dev/null && { echo "  rmmod $m ok"; break; }; sleep 1; done
done
lsmod | grep -E "^nvidia" && echo "  !!! 仍有模块残留" || echo "  驱动已全部卸载"

echo "=== nvflash --list ==="
$NV --list 2>&1

echo "=== nvflash --save (adapter 1) ==="
$NV -i 1 --save "$OUT/v100_original.rom" 2>&1
echo "  rc=$?"
ls -l "$OUT/v100_original.rom" 2>/dev/null
