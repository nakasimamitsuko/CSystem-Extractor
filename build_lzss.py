#!/usr/bin/env python3
"""
编译 lzss_fast.c 为 Python 可调用的共享库

用法:
  python build_lzss.py

会生成:
  Linux:   lzss_fast.so
  Windows: lzss_fast.dll
"""

import os
import sys
import platform
import subprocess

def build():
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lzss_fast.c')
    
    if platform.system() == 'Windows':
        out = 'lzss_fast.dll'
        # 尝试 MSVC
        try:
            subprocess.run(['cl', '/O2', '/LD', src, f'/Fe:{out}'], check=True)
            print(f'编译成功: {out} (MSVC)')
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        
        # 尝试 GCC (MinGW)
        try:
            subprocess.run(['gcc', '-O3', '-shared', '-o', out, src], check=True)
            print(f'编译成功: {out} (GCC)')
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        
        print('错误: 需要 MSVC 或 GCC (MinGW) 来编译')
        print('  MSVC: 在 Visual Studio 开发者命令行中运行')
        print('  GCC:  安装 MinGW-w64 并确保 gcc 在 PATH 中')
        sys.exit(1)
    else:
        out = 'lzss_fast.so'
        try:
            subprocess.run(['gcc', '-O3', '-shared', '-fPIC', '-o', out, src], check=True)
            print(f'编译成功: {out}')
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            print(f'编译失败: {e}')
            print('需要 gcc, 安装: sudo apt install gcc')
            sys.exit(1)

if __name__ == '__main__':
    build()
