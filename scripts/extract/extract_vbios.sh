#!/bin/bash
# 注: 原脚本内嵌明文 root 密码, 开源版已改为从环境变量读取。
#     使用前请先: export ROOT_PASSWORD="你的root密码"
# V100 VBIOS 提取脚本

PASSWORD="${ROOT_PASSWORD:?请先导出 ROOT_PASSWORD 环境变量}"
PCI_ADDR="0000:04:00.0"
OUTPUT_FILE="v100_vbios.rom"

echo "正在启用 ROM 读取..."
echo "$PASSWORD" | sudo -S bash -c "echo 1 > /sys/bus/pci/devices/$PCI_ADDR/rom"

if [ $? -ne 0 ]; then
    echo "启用 ROM 失败"
    exit 1
fi

echo "正在读取 VBIOS..."
echo "$PASSWORD" | sudo -S cat /sys/bus/pci/devices/$PCI_ADDR/rom > "$OUTPUT_FILE"

if [ $? -ne 0 ]; then
    echo "读取 VBIOS 失败"
    exit 1
fi

echo "正在关闭 ROM 读取..."
echo "$PASSWORD" | sudo -S bash -c "echo 0 > /sys/bus/pci/devices/$PCI_ADDR/rom"

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
