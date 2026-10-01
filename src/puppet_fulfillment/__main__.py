"""命令行入口：python -m puppet_fulfillment [db路径] [端口]"""
from __future__ import annotations

import sys

from .api import serve
from .database import Database
from .service import FulfillmentService


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "puppet_fulfillment.db"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    service = FulfillmentService(Database(db_path))
    serve(service, port=port)


if __name__ == "__main__":
    main()
