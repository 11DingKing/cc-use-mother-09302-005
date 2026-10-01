"""数据访问与统一分录。

所有库存状态变化都必须经过这里：
  * record_event  写一条事件；
  * post_entry    在事件下追加有符号库存分录；
  * *_balance     从分录重算余额，用于对账与锁定选件。

item.current_status / allocation_units.qty_* 只是加速读模型，
ledger_entries 才是唯一事实来源，两者可随时核对。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

BUCKETS = ("备货", "占用", "出库", "使用", "归还", "损坏", "丢失", "已报废")
STAGES = ("备货", "占用", "出库", "使用", "归还")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # 基础工具 -------------------------------------------------------------
    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def get_or_404(self, table: str, key_col: str, key_val: str, what: str) -> sqlite3.Row:
        row = self.one(f"SELECT * FROM {table} WHERE {key_col} = ?", (key_val,))
        if row is None:
            from .errors import NotFound

            raise NotFound(f"{what}不存在：{key_val}")
        return row

    # 事件与分录 -----------------------------------------------------------
    def record_event(
        self,
        event_type: str,
        stage: str,
        *,
        actor_id: str | None = None,
        show_id: str | None = None,
        reservation_id: str | None = None,
        outbound_id: str | None = None,
        payload: dict | None = None,
        occurred_at: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO events (event_type, stage, occurred_at, actor_id, show_id,
                                reservation_id, outbound_id, payload_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                event_type,
                stage,
                occurred_at or now(),
                actor_id,
                show_id,
                reservation_id,
                outbound_id,
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        return int(cur.lastrowid)

    def post_entry(
        self,
        event_id: int,
        *,
        scope: str,
        batch_no: str,
        kind_code: str,
        bucket: str,
        amount_qty: int,
        item_code: str | None = None,
        show_id: str | None = None,
        reservation_id: str | None = None,
        unit_id: str | None = None,
        stage: str | None = None,
        note: str | None = None,
        created_at: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO ledger_entries (event_id, scope, batch_no, item_code, kind_code,
                                        show_id, reservation_id, unit_id, stage,
                                        bucket, amount_qty, note, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                scope,
                batch_no,
                item_code,
                kind_code,
                show_id,
                reservation_id,
                unit_id,
                stage,
                bucket,
                amount_qty,
                note,
                created_at or now(),
            ),
        )
        return int(cur.lastrowid)

    def post_item_move(
        self,
        event_id: int,
        *,
        item: sqlite3.Row,
        from_bucket: str | None,
        to_bucket: str | None,
        stage: str,
        show_id: str | None = None,
        reservation_id: str | None = None,
        unit_id: str | None = None,
        note: str | None = None,
    ) -> None:
        """单件移动：离桶 -1、入桶 +1；同时自动登记平行的批次汇总分录。

        这样“单件账”和“批次账”由同一动作一次写就，两种粒度永远可交叉核对。
        """
        ts = now()
        item_common = dict(
            scope="单件",
            batch_no=item["batch_no"],
            item_code=item["item_code"],
            kind_code=item["kind_code"],
            show_id=show_id,
            reservation_id=reservation_id,
            unit_id=unit_id,
            stage=stage,
            note=note,
            created_at=ts,
        )
        batch_common = {k: v for k, v in item_common.items() if k != "item_code"}
        batch_common["scope"] = "批次"
        batch_common["item_code"] = None
        if from_bucket:
            self.post_entry(event_id, bucket=from_bucket, amount_qty=-1, **item_common)
            self.post_entry(event_id, bucket=from_bucket, amount_qty=-1, **batch_common)
        if to_bucket:
            self.post_entry(event_id, bucket=to_bucket, amount_qty=1, **item_common)
            self.post_entry(event_id, bucket=to_bucket, amount_qty=1, **batch_common)

    # 读模型维护 -----------------------------------------------------------
    def set_item_status(
        self,
        item_code: str,
        status: str,
        holder: str | None = None,
        *,
        quarantine: int | None = None,
        clear_holder: bool = False,
    ) -> None:
        if quarantine is not None:
            self.conn.execute(
                "UPDATE items SET current_status=?, current_holder=?, quarantine=? WHERE item_code=?",
                (status, holder, quarantine, item_code),
            )
        elif clear_holder:
            self.conn.execute(
                "UPDATE items SET current_status=?, current_holder=NULL WHERE item_code=?",
                (status, item_code),
            )
        else:
            self.conn.execute(
                "UPDATE items SET current_status=?, COALESCE(?, current_holder) WHERE item_code=?",
                (status, holder, item_code),
            )

    # 可用量：以分录为准（占用/出库/使用/损坏/丢失/报废均不在库）-------------
    def available_items(self, kind_code: str) -> list[sqlite3.Row]:
        return self.all(
            """
            SELECT i.* FROM items i
            WHERE i.kind_code = ?
              AND i.current_status = '备货'
              AND i.quarantine = 0
              AND NOT EXISTS (
                  SELECT 1 FROM allocation_items ai
                  WHERE ai.item_code = i.item_code AND ai.active = 1
              )
            ORDER BY i.batch_no, i.seq
            """,
            (kind_code,),
        )

    def batch_available_qty(self, batch_no: str) -> int:
        """批次内当前可借实物数（由单件状态汇总，保证两种粒度同账）。"""
        return int(
            self.scalar(
                """
                SELECT COUNT(*) FROM items i
                WHERE i.batch_no = ?
                  AND i.current_status = '备货' AND i.quarantine = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM allocation_items ai
                      WHERE ai.item_code = i.item_code AND ai.active = 1
                  )
                """,
                (batch_no,),
            )
            or 0
        )

    # 分录余额与对账 -------------------------------------------------------
    def item_balance(self, item_code: str) -> dict[str, int]:
        rows = self.all(
            "SELECT bucket, SUM(amount_qty) q FROM ledger_entries "
            "WHERE item_code=? AND scope='单件' GROUP BY bucket",
            (item_code,),
        )
        bal = {b: 0 for b in BUCKETS}
        for r in rows:
            bal[r["bucket"]] = int(r["q"])
        return bal

    def batch_balance(self, batch_no: str) -> dict[str, int]:
        """批次粒度余额：取 scope='批次' 的汇总分录（单件分录是平行账，不重复计入）。"""
        rows = self.all(
            "SELECT bucket, SUM(amount_qty) q FROM ledger_entries "
            "WHERE batch_no=? AND scope='批次' GROUP BY bucket",
            (batch_no,),
        )
        bal = {b: 0 for b in BUCKETS}
        for r in rows:
            bal[r["bucket"]] = int(r["q"])
        return bal

    def reconcile(self) -> dict[str, Any]:
        """全库对账：
          1. 每件物资在单件账上恰好落在一个桶，桶余额非负；
          2. 单件账读模型状态与分录一致；
          3. 批次汇总账与逐件汇总账每桶一致（两种粒度同账）；
          4. 批次所有桶合计 = 入库总量（守恒）。
        """
        problems: list[dict[str, Any]] = []

        def bucket_rows(scope: str) -> list[sqlite3.Row]:
            col = "item_code" if scope == "单件" else "batch_no"
            return self.all(
                f"""
                SELECT {col} AS key, bucket, SUM(amount_qty) q
                FROM ledger_entries WHERE scope=? GROUP BY {col}, bucket HAVING q <> 0
                """,
                (scope,),
            )

        per_item: dict[str, dict[str, Any]] = {}
        for r in bucket_rows("单件"):
            rec = per_item.setdefault(r["key"], {"buckets": {}})
            rec["buckets"][r["bucket"]] = int(r["q"])

        for item_code, rec in per_item.items():
            for bucket, q in rec["buckets"].items():
                if q < 0:
                    problems.append(
                        {"item_code": item_code, "bucket": bucket, "problem": f"桶余额为负：{q}"}
                    )
            placed = sum(q for q in rec["buckets"].values() if q > 0)
            if placed != 1:
                problems.append(
                    {"item_code": item_code, "problem": f"实物落桶数应为 1，实际 {placed}"}
                )
            cur = self.one("SELECT current_status FROM items WHERE item_code=?", (item_code,))
            if cur is not None:
                positive = [b for b, q in rec["buckets"].items() if q > 0]
                ledger_status = positive[0] if positive else None
                if ledger_status and ledger_status != cur["current_status"]:
                    problems.append(
                        {
                            "item_code": item_code,
                            "problem": f"读模型状态 {cur['current_status']} 与分录 {ledger_status} 不一致",
                        }
                    )

        # 批次：逐件账按批汇总，与批次汇总账交叉核对
        item_by_batch: dict[str, dict[str, int]] = {}
        for item_code, rec in per_item.items():
            bn = self.scalar("SELECT batch_no FROM items WHERE item_code=?", (item_code,))
            slot = item_by_batch.setdefault(bn, {b: 0 for b in BUCKETS})
            for bucket, q in rec["buckets"].items():
                slot[bucket] += q

        for b in self.all("SELECT batch_no, total_qty FROM batches"):
            bn = b["batch_no"]
            from_items = item_by_batch.get(bn, {b: 0 for b in BUCKETS})
            from_batch = self.batch_balance(bn)
            for bucket in BUCKETS:
                if from_items.get(bucket, 0) != from_batch.get(bucket, 0):
                    problems.append({
                        "batch_no": bn, "bucket": bucket,
                        "problem": f"批次账 {from_batch.get(bucket, 0)} 与逐件账 "
                                   f"{from_items.get(bucket, 0)} 不一致",
                    })
            total = sum(from_items.values())
            if total != b["total_qty"]:
                problems.append(
                    {"batch_no": bn, "problem": f"批次不守恒：分录合计 {total} != 入库 {b['total_qty']}"}
                )

        return {
            "ok": not problems,
            "checked_items": len(per_item),
            "checked_batches": len(item_by_batch),
            "problems": problems,
            "checked_at": now(),
        }

    def item_trace(self, item_code: str) -> list[dict[str, Any]]:
        """沿单件分录还原完整去向与责任环节。"""
        rows = self.all(
            """
            SELECT le.entry_id, le.event_id, e.event_type, le.stage, le.bucket,
                   le.amount_qty, le.note, le.created_at, e.actor_id,
                   e.show_id, e.reservation_id, e.outbound_id, e.payload_json
            FROM ledger_entries le JOIN events e ON e.event_id = le.event_id
            WHERE le.item_code = ?
            ORDER BY le.entry_id
            """,
            (item_code,),
        )
        result = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            result.append(d)
        return result
