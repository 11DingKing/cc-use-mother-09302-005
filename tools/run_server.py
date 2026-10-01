#!/usr/bin/env python3
"""启动木偶教具库存履约服务端（免安装，直接从源码目录运行）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from prop_inventory.__main__ import main

if __name__ == "__main__":
    main()
