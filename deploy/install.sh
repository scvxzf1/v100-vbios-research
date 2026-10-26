#!/bin/bash
# 把 V100 开机注入补丁安装到系统
# 幂等：可重复执行；先校验 MANIFEST.sha256，文件被改动则拒绝安装
set -eu
[ "$(id -u)" -eq 0 ] || { echo "需要 root: sudo $0"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)

echo "== 1. 校验备份文件完整性 =="
cd "$HERE"
if ! sha256sum -c MANIFEST.sha256 --quiet --ignore-missing; then
  echo "  ❌ 校验失败，备份文件已被改动，拒绝安装"; exit 1
fi
echo "  ✅ MANIFEST 校验通过"

echo "== 2. 安装文件 =="
install -d -m 755 /etc/v100
install -m 644 "$HERE/../firmware/v100_payload_350w.bin" /etc/v100/v100_final_payload.bin
install -m 755 "$HERE/v100-boot-patch.sh"    /usr/local/sbin/v100-boot-patch.sh
install -m 644 "$HERE/v100-boot-patch.service" /etc/systemd/system/v100-boot-patch.service
echo "  payload  $(stat -c%s /etc/v100/v100_final_payload.bin) 字节"

echo "== 3. 语法与单元校验 =="
bash -n /usr/local/sbin/v100-boot-patch.sh && echo "  脚本语法 ✅"
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/v100-boot-patch.service && echo "  单元定义 ✅"

echo "== 4. 启用 =="
systemctl enable v100-boot-patch.service 2>&1 | sed 's/^/  /'
echo "  状态: $(systemctl is-enabled v100-boot-patch.service)"

cat <<'TIP'

安装完成。补丁将在下次开机、显示管理器启动前自动注入。

  # 卡已装回但不想重启时，可手动应用一次
  sudo /usr/local/sbin/v100-boot-patch.sh && tail -5 /var/log/v100-boot-patch.log

  # 开机后确认
  grep 成功 /var/log/v100-boot-patch.log

  # 卸载
  sudo ./uninstall.sh
TIP
