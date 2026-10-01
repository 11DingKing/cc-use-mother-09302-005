"""木偶教具库存履约服务端。"""
from .db import Database
from .service import InventoryService

__all__ = ["Database", "InventoryService"]
__version__ = "0.2.0"
