#!/bin/bash
# 注: 原脚本内嵌明文 root 密码, 开源版已改为从环境变量读取。
#     使用前请先: export ROOT_PASSWORD="你的root密码"
# 安全提示: 本文用 bash here-string 把口令喂给 sudo 的 stdin, 不产生额外子进程,
#     因此口令不会出现在 /proc/<pid>/cmdline 里(进程表)。更稳妥的做法是配置
#     NOPASSWD 的 sudoers 规则并改用 `sudo -n`，本脚本不需要为此改代码。
# V100 VBIOS 提取脚本

PASSWORD="${ROOT_PASSWORD:?请先导出 ROOT_PASSWORD 环境变量}"
PCI_ADDR="0000:04:00.0"
OUTPUT_FILE="v100_vbios.rom"

echo "正在启用 ROM 读取..."
sudo -S bash -c "echo 1 > /sys/bus/pci/devices/$PCI_ADDR/rom" <<< "$PASSWORD"

if [ $? -ne 0 ]; then
    echo "启用 ROM 失败"
    exit 1
fi

echo "正在读取 VBIOS..."
sudo -S cat /sys/bus/pci/devices/$PCI_ADDR/rom <<< "$PASSWORD" > "$OUTPUT_FILE"

if [ $? -ne 0 ]; then
    echo "读取 VBIOS 失败"
    exit 1
fi

echo "正在关闭 ROM 读取..."
sudo -S bash -c "echo 0 > /sys/bus/pci/devices/$PCI_ADDR/rom" <<< "$PASSWORD"

# 验证文件
FILE_SIZE=$(stat -c%s "$OUTPUT_FILE")
echo "VBIOS 文件大小: $FILE_SIZE 字节"

if [ $FILE_SIZE -gt 0 ]; then
    echo "✓ VBIOS 提取成功: $OUTPUT_FILE"
    file "$OUTPUT_FILE"
    echo ""
    echo "前 64 字节（十六进制）："
    hexdump -C "$OUTPUT_FILE" | head -5
else
    echo "✗ 提取失败，文件为空"
    exit 1
fi
