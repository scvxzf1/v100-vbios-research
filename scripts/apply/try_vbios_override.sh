#!/bin/bash
# V100 vbios registry 覆盖实验：停 gdm -> 逐变体写入+unbind/bind -> 判定 -> 恢复 gdm
# 判据: vbios_version 从 88.00.4F.00.09 变为 88.00.13.00.02，且 power.max_limit 变 350W
GPU=0000:04:00.0
DRV=/sys/bus/pci/drivers/nvidia
LOG=/path/to/v100-vbios-research/vbios_override_test.log

probe() { nvidia-smi --query-gpu=index,vbios_version,power.limit,power.max_limit,clocks.max.sm \
          --format=csv,noheader -i 1 2>&1 | sed 's/^/    /'; }

{
echo "########## $(date) ##########"
echo "=== 基线 ==="; probe

echo "=== 确保 /dev/nvidia1 空闲 ==="
systemctl is-active gdm >/dev/null 2>&1 && { echo "  gdm 仍在运行，停止它"; systemctl stop gdm; sleep 3; } || echo "  gdm 已 inactive"
echo "  仍持有 nvidia1 的进程:"; fuser -v /dev/nvidia1 2>&1 | sed 's/^/    /' || echo "    无"

for v in v0_stock v1_full_1M v2_rom_from_A00 v3_e0end v4_e0end_A00; do
  echo; echo "=== 变体 $v ($(stat -c%s /tmp/payload_$v.bin) 字节) ==="
  { printf 'vbios='; cat /tmp/payload_$v.bin; } > /proc/driver/nvidia/registry
  rc=$?; echo "  写 registry rc=$rc  ->  $(cat /proc/driver/nvidia/registry)"
  [ $rc -ne 0 ] && continue

  dmesg -C 2>/dev/null
  echo $GPU > $DRV/unbind 2>&1 && echo "  unbind ok" || echo "  unbind FAIL"
  sleep 1
  echo $GPU > $DRV/bind   2>&1 && echo "  bind ok"   || echo "  bind FAIL"
  sleep 5

  echo "  --- 结果 ---"; probe
  echo "  --- dmesg(nvidia/vbios) ---"
  dmesg 2>/dev/null | grep -iE "nvidia|vbios|override" | tail -12 | sed 's/^/    /'

  if nvidia-smi -i 1 --query-gpu=vbios_version --format=csv,noheader 2>/dev/null | grep -q "88.00.13"; then
      echo "  *** 命中：覆盖已生效 ***"
      HIT=$v; break
  fi
done

echo; echo "=== 最终状态 ==="; probe
echo "（未自动启动 gdm；需要时手动: systemctl start gdm）"
echo "HIT=${HIT:-none}"
} 2>&1 | tee "$LOG"
