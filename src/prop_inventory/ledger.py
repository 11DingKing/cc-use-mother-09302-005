"""统一分录：所有库存变动以追加式分录落账，哈希链保证可核对。

每条分录记录单件物资的一次状态推进（或入套/出套等事件），
``hash`` 由前一条分录的哈希与本次内容共同计算，任何篡改或
漏记都会在 ``verify_chain`` 重放时暴露；``derive_statuses``
可按分录重放推导每件物资的应有状态，用于对账。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

GENESIS_HASH = "0" * 64


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def entry_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


_HASH_FIELDS = (
    "entry_id", "item_id", "action", "from_status", "to_status", "actor", "role",
    "session_id", "ref_type", "ref_id", "detail", "prev_hash", "created_at",
)


def append_entry(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    action: str,
    from_status: str | None,
    to_status: str,
    actor: str,
    role: str,
    session_id: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    detail: dict | None = None,
    at: str | None = None,
) -> dict:
    """在当前事务内追加一条分录（与状态变更同事务提交）。"""
    row = conn.execute("SELECT hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
    prev_hash = row["hash"] if row else GENESIS_HASH
    payload = {
        "entry_id": new_id("ENT"),
        "item_id": item_id,
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "actor": actor,
        "role": role,
        "session_id": session_id,
        "ref_type": ref_type,
        "ref_id": ref_id,
        "detail": json.dumps(detail or {}, ensure_ascii=False, sort_keys=True),
        "prev_hash": prev_hash,
        "created_at": at or now_iso(),
    }
    digest = entry_hash(payload)
    conn.execute(
        """INSERT INTO entries(entry_id, item_id, action, from_status, to_status, actor, role,
                              session_id, ref_type, ref_id, detail, prev_hash, hash, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            payload["entry_id"], item_id, action, from_status, to_status, actor, role,
            session_id, ref_type, ref_id, payload["detail"], prev_hash, digest,
            payload["created_at"],
        ),
    )
    return {**payload, "hash": digest}


def verify_chain(conn: sqlite3.Connection) -> list[dict]:
    """重放哈希链，返回断链/篡改点列表（空列表 = 链完整）。"""
    problems = []
    prev = GENESIS_HASH
    for row in conn.execute("SELECT * FROM entries ORDER BY seq"):
        payload = {key: row[key] for key in _HASH_FIELDS}
        if row["prev_hash"] != prev:
            problems.append({"seq": row["seq"], "entry_id": row["entry_id"], "problem": "prev_hash 与链上前一条不一致"})
        if entry_hash(payload) != row["hash"]:
            problems.append({"seq": row["seq"], "entry_id": row["entry_id"], "problem": "分录内容哈希校验失败"})
        prev = row["hash"]
    return problems


def derive_statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """按分录顺序重放，推导每件物资的应有状态。"""
    derived: dict[str, str] = {}
    for row in conn.execute("SELECT item_id, to_status FROM entries ORDER BY seq"):
        derived[row["item_id"]] = row["to_status"]
    return derived
