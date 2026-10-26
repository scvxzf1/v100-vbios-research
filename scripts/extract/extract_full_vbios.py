#!/usr/bin/env python3
# 注: 原脚本内嵌明文 root 密码, 开源版已改为从环境变量 ROOT_PASSWORD 读取。
"""
完整 V100 VBIOS 提取工具
尝试多种方法提取完整的 VBIOS ROM（包含 NPDS Image 3）
"""
import subprocess
import os

# 从环境变量读取 root 密码（开源版不含明文密码）
root_pw = os.environ.get("ROOT_PASSWORD", "")
if not root_pw:
    raise SystemExit("请先导出 ROOT_PASSWORD 环境变量")
import sys
from pathlib import Path

def run_cmd(cmd, desc):
    """执行命令并返回结果"""
    print(f"\n[*] {desc}")
    print(f"    命令: {cmd}")
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            print(f"    ✓ 成功")
            return result.stdout
        else:
            print(f"    ✗ 失败: {result.stderr}")
            return None
    except Exception as e:
        print(f"    ✗ 异常: {e}")
        return None

def main():
    pci_addr = "0000:04:00.0"
    output_dir = Path("/path/to/v100-vbios-research")
    
    print("=" * 70)
    print("V100 完整 VBIOS 提取工具")
    print("=" * 70)
    
    # 方法 1: nvidia-smi --gpu-rom-dump（推荐）
    output1 = output_dir / "v100_full_rom_method1.rom"
    run_cmd(f"echo '{root_pw}' | su - root -c 'nvidia-smi -i 1 --gpu-rom-dump={output1}'",
            "方法 1: 使用 nvidia-smi --gpu-rom-dump")
    
    # 方法 2: 从 sysfs debugfs 读取
    output2 = output_dir / "v100_full_rom_method2.rom"
    run_cmd(f"echo '{root_pw}' | su - root -c 'cat /sys/kernel/debug/dri/1/vbios.rom > {output2} 2>/dev/null || echo 未找到 debugfs 入口'",
            "方法 2: 从 kernel debugfs 读取")
    
    # 方法 3: nvidia-debugdump
    output3 = output_dir / "v100_debugdump.zip"
    run_cmd(f"echo '{root_pw}' | su - root -c 'nvidia-debugdump --dumpall --device 1 -f {output3}'",
            "方法 3: nvidia-debugdump 完整转储")
    
    # 方法 4: 通过 NvAPI 读取（需要特殊工具）
    print("\n" + "=" * 70)
    print("提取完成，检查结果：")
    print("=" * 70)
    
    for rom_file in [output1, output2]:
        if rom_file.exists():
            size = rom_file.stat().st_size
            print(f"\n{rom_file.name}:")
            print(f"  大小: {size} 字节 ({size/1024:.1f} KB)")
            if size > 200000:
                print(f"  ✓ 大小合理（>200KB），可能是完整 ROM")
            else:
                print(f"  ✗ 太小，可能不完整")
            
            # 检查是否包含 NPDS 镜像
            with open(rom_file, 'rb') as f:
                data = f.read()
                if b'NPDS' in data:
                    npds_count = data.count(b'NPDS')
                    print(f"  ✓ 包含 {npds_count} 个 NPDS 镜像")
                else:
                    print(f"  ✗ 未找到 NPDS 镜像")
    
    print("\n" + "=" * 70)
    print("建议：")
    print("1. 如果以上方法都失败，尝试在 Windows 下使用 GPU-Z 提取")
    print("2. 或使用 nvflash 工具：nvflash --save v100_backup.rom")
    print("3. 完整 V100 ROM 应该约 300-350KB，包含多个 NPDS 镜像")
    print("=" * 70)

if __name__ == '__main__':
    main()
