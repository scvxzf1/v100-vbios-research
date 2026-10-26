#!/bin/bash
# V100 VBIOS RAM 覆盖 —— 开机早期注入
# 目标配置: 功耗 350 W / XBAR 1912 / GPC(Applications) 1800
# 设计原则: 任何环节失败都必须让系统以"原厂状态"正常启动, 绝不阻塞开机。
set -u
STOPPED_LACT=0
GPU=0000:04:00.0
DEV=/sys/bus/pci/devices/$GPU
PAYLOAD=/etc/v100/v100_final_payload.bin
LOG=/var/log/v100-boot-patch.log

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG" 2>/dev/null; }
fail(){ echo 1 > /sys/bus/pci/rescan 2>/dev/null; log "失败: $* -- 已回退原厂"; [ "${STOPPED_LACT:-0}" = "1" ] && systemctl start lactd 2>/dev/null; exit 0; }

log "=== 开始 ==="
[ -r "$PAYLOAD" ] || fail "payload 缺失 ($PAYLOAD)"

for i in $(seq 1 3); do [ -e "$DEV" ] && break; sleep 1; done
[ -e "$DEV" ] || { log "V100 不在总线上, 跳过 (保持原厂)"; exit 0; }
log "V100 已发现"

STOPPED_LACT=0
if systemctl is-active --quiet lactd 2>/dev/null; then
  systemctl stop lactd 2>/dev/null && { STOPPED_LACT=1; log "lactd 已停"; }
fi

if lsmod | grep -qE '^nvidia '; then
  for m in nvidia_drm nvidia_modeset nvidia_uvm nvidia; do
    for i in 1 2 3; do lsmod | grep -qE "^$m " || break; rmmod $m 2>/dev/null; sleep 1; done
  done
  lsmod | grep -qE '^nvidia ' && fail "驱动无法完全卸载"
  log "驱动已卸载"
fi

# 前置检查: 若有进程仍持有 /dev/nvidia*, 内核 nv_pci_remove 会阻塞在内核态
# (实测可阻塞数分钟), 此时宁可放弃注入也不能拖住开机
for dv in /dev/nvidia0 /dev/nvidia1 /dev/nvidiactl /dev/nvidia-uvm; do
  [ -e "$dv" ] || continue
  HOLDERS=$(fuser "$dv" 2>/dev/null | tr -s ' ')
  [ -n "$HOLDERS" ] && fail "设备 $dv 被占用 (pid:$HOLDERS)"
done
log "设备无占用, 准备摘卡"

echo 1 > "$DEV/remove" 2>/dev/null || fail "无法将 V100 摘下总线"
sleep 1
modprobe nvidia 2>/dev/null || fail "modprobe nvidia 失败"
sleep 1
[ -e /proc/driver/nvidia/registry ] || fail "registry 接口不存在"

{ printf 'vbios='; cat "$PAYLOAD"; } > /proc/driver/nvidia/registry 2>/dev/null || fail "写入 registry 失败"
log "覆盖已写入 ($(stat -c%s "$PAYLOAD") 字节)"

echo 1 > /sys/bus/pci/rescan 2>/dev/null || fail "rescan 失败"
sleep 5

MX=$(nvidia-smi -i "$GPU" --query-gpu=power.max_limit --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
SM=$(nvidia-smi -i "$GPU" --query-gpu=clocks.max.sm   --format=csv,noheader,nounits 2>/dev/null | tr -d ' ')
VB=$(nvidia-smi -i "$GPU" --query-gpu=vbios_version   --format=csv,noheader 2>/dev/null | tr -d ' ')

if [ "$MX" = "350.00" ]; then
  nvidia-smi -i "$GPU" -pl 350 >/dev/null 2>&1
  log "成功: vbios=$VB  max_limit=${MX}W  max_sm=${SM}MHz"
else
  log "验证不符: max_limit='${MX}' (期望 350.00), max_sm='${SM}' -- 保持原厂"
fi

[ "${STOPPED_LACT:-0}" = "1" ] && systemctl start lactd 2>/dev/null
log "=== 结束 ==="
exit 0
