"""SQLite 存储层：连接管理、建表、写事务串行化。

- 每线程一个连接；写事务统一走 ``BEGIN IMMEDIATE`` 并由进程内写锁串行化，
  与 SQLite 单写者语义一致，保证并发借用时不会出现超锁；
- 写事务支持嵌套（幂等包装层 + 业务操作合并为同一事务提交）；
- 文件库启用 WAL，读写互不阻塞。
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT '',
    parent_batch_id TEXT REFERENCES batches(batch_id),
    note            TEXT NOT NULL DEFAULT '',
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    item_id    TEXT PRIMARY KEY,
    batch_id   TEXT NOT NULL REFERENCES batches(batch_id),
    category   TEXT NOT NULL,
    label      TEXT NOT NULL,
    status     TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_status   ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_session  ON items(session_id);
CREATE INDEX IF NOT EXISTS idx_items_category ON items(category);

CREATE TABLE IF NOT EXISTS set_templates (
    template_id TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS set_template_parts (
    template_id TEXT NOT NULL REFERENCES set_templates(template_id),
    category    TEXT NOT NULL,
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (template_id, category)
);

CREATE TABLE IF NOT EXISTS kits (
    kit_id      TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES set_templates(template_id),
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kit_members (
    kit_id   TEXT NOT NULL REFERENCES kits(kit_id),
    item_id  TEXT NOT NULL REFERENCES items(item_id),
    category TEXT NOT NULL,
    PRIMARY KEY (kit_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_kit_members_item ON kit_members(item_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    school     TEXT NOT NULL,
    teacher    TEXT NOT NULL,
    planned_at TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_requirements (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    template_id TEXT NOT NULL REFERENCES set_templates(template_id),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (session_id, template_id)
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    kit_id         TEXT NOT NULL REFERENCES kits(kit_id),
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_session ON reservations(session_id);
CREATE INDEX IF NOT EXISTS idx_reservations_kit     ON reservations(kit_id);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    seq         INTEGER NOT NULL,
    status      TEXT NOT NULL,
    shipped_by  TEXT NOT NULL,
    shipped_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipment_items (
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    item_id     TEXT NOT NULL REFERENCES items(item_id),
    signed      INTEGER NOT NULL DEFAULT 0,
    signed_by   TEXT,
    signed_at   TEXT,
    PRIMARY KEY (shipment_id, item_id)
);

CREATE TABLE IF NOT EXISTS returns (
    return_id   TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id),
    received_by TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS return_items (
    return_id TEXT NOT NULL REFERENCES returns(return_id),
    item_id   TEXT NOT NULL REFERENCES items(item_id),
    condition TEXT NOT NULL,
    PRIMARY KEY (return_id, item_id)
);

CREATE TABLE IF NOT EXISTS entries (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT NOT NULL UNIQUE,
    item_id     TEXT NOT NULL,
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    actor       TEXT NOT NULL,
    role        TEXT NOT NULL,
    session_id  TEXT,
    ref_type    TEXT,
    ref_id      TEXT,
    detail      TEXT NOT NULL DEFAULT '{}',
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_item    ON entries(item_id);
CREATE INDEX IF NOT EXISTS idx_entries_session ON entries(session_id);

CREATE TABLE IF NOT EXISTS operations (
    idempotency_key TEXT PRIMARY KEY,
    endpoint        TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    status_code     INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
"""


class Database:
    """SQLite 连接与事务管理。"""

    def __init__(self, path: str = ":memory:"):
        self._uri = False
        if path == ":memory:":
            # 共享缓存内存库，每个实例独立命名，避免测试间串数据
            path = f"file:prop-inventory-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
        self._path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._keeper = self._connect()  # 保活共享内存库并完成建表
        self._migrate(self._keeper)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, uri=self._uri, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        if not self._uri:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def write_tx(self):
        """写事务（BEGIN IMMEDIATE）。进程内串行化，可嵌套复用同一事务。"""
        with self._write_lock:
            conn = self.connection()
            depth = getattr(self._local, "tx_depth", 0)
            self._local.tx_depth = depth + 1
            try:
                if depth == 0:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
                if depth == 0:
                    if getattr(self._local, "tx_doomed", False):
                        conn.execute("ROLLBACK")
                        raise RuntimeError("事务内层已失败，整体回滚")
                    conn.execute("COMMIT")
            except Exception:
                if depth == 0:
                    conn.execute("ROLLBACK")
                else:
                    self._local.tx_doomed = True
                raise
            finally:
                self._local.tx_depth = depth
                if depth == 0:
                    self._local.tx_doomed = False

    @contextmanager
    def read_tx(self):
        """读事务；若当前线程已在写事务内则直接复用同一连接。"""
        conn = self.connection()
        if getattr(self._local, "tx_depth", 0):
            yield conn
            return
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._keeper.close()
