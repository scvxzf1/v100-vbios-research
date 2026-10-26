#!/bin/bash
# 卸载 V100 开机注入补丁，恢复到纯原厂行为
set -eu
[ "$(id -u)" -eq 0 ] || { echo "需要 root: sudo $0"; exit 1; }

echo "== 停止并禁用单元 =="
systemctl disable --now v100-boot-patch.service 2>&1 | sed 's/^/  /' || true

echo "== 删除文件 =="
rm -f /etc/systemd/system/v100-boot-patch.service
rm -f /usr/local/sbin/v100-boot-patch.sh
rm -rf /etc/v100
systemctl daemon-reload
echo "  已移除单元、脚本与 payload"

cat <<'TIP'

卸载完成。注意：

  * 若当前内存中已注入的覆盖仍在生效，需 rmmod 或重启才会回原厂：
      sudo rmmod nvidia_drm nvidia_modeset nvidia_uvm nvidia && sudo modprobe nvidia
  * 日志 /var/log/v100-boot-patch.log 已保留，可手动删除。
TIP
