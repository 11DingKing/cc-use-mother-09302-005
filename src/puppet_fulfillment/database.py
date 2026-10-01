"""SQLite 连接与初始化。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class _Lease(sqlite3.Connection):
    """内存共享连接：业务层的 close() 只是释放租约，不真正销毁数据库。"""

    def close(self) -> None:  # type: ignore[override]
        return None

    def really_close(self) -> None:
        super().close()


class Database:
    """库存分录库。每个工作单元使用独立连接，写入一律 BEGIN IMMEDIATE。

    :memory: 模式复用单一连接（SQLite 内存库按连接隔离），适合单线程测试；
    生产并发请使用文件库（WAL + 每连接 busy_timeout）。
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._mem_conn: sqlite3.Connection | None = None
        self._mem_lock = threading.RLock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.init()

    def init(self) -> None:
        """建表（幂等）。"""
        ddl = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = self.connect()
        try:
            conn.executescript(ddl)
        finally:
            if self.path != ":memory:":
                conn.close()

    def connect(self) -> sqlite3.Connection:
        if self.path == ":memory:":
            with self._mem_lock:
                if self._mem_conn is None:
                    conn = sqlite3.connect(":memory:", isolation_level=None,
                                           check_same_thread=False, factory=_Lease)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys = ON")
                    self._mem_conn = conn
                return self._mem_conn
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即拿写锁的事务：并发借用在数据库层串行化，失败整体回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
