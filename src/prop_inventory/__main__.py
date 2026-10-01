"""命令行入口：``python -m prop_inventory --port 8000 --db data/inventory.db``。"""
from __future__ import annotations

import argparse

from .api import serve


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="prop_inventory", description="木偶教具库存履约服务端")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--db", default=":memory:", help="SQLite 数据库路径（默认内存库）")
    args = parser.parse_args(argv)
    serve(args.host, args.port, args.db)


if __name__ == "__main__":
    main()
