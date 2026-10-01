"""木偶教具库存履约领域服务。

每个公开方法都是一个事务（BEGIN IMMEDIATE）：
  * 场次确认必须“一次性”锁定全部需求，任一种类不足则整体回滚；
  * 所有动作写 events + ledger_entries，并同步单件读模型；
  * idem_key 支持中途失败后原样重试（幂等回放）；
  * 实物活动占用由数据库唯一索引保证，并发借用不会重复锁定同一件。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Sequence

from .database import Database, transaction
from .errors import (
    Conflict,
    DuplicateReceipt,
    IllegalTransition,
    InsufficientStock,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from .repository import Repository, now

VALID_ROLES = {"器材管理员", "活动老师", "学校签收人"}
DAMAGE_BUCKETS = {"损坏", "丢失"}


class FulfillmentService:
    def __init__(self, db: Database | str = ":memory:") -> None:
        self.db = db if isinstance(db, Database) else Database(db)
        if self.db.path == ":memory:":
            self.db.init()

    # 连接与幂等 -----------------------------------------------------------
    def _repo(self) -> tuple[sqlite3.Connection, Repository]:
        conn = self.db.connect()
        return conn, Repository(conn)

    def _idem_replay(self, repo: Repository, key: str | None, request: dict) -> Any:
        if not key:
            return None
        row = repo.one("SELECT result_json, request_hash FROM idempotency WHERE idem_key=?", (key,))
        if row is None:
            return None
        digest = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if row["request_hash"] != digest:
            raise Conflict(f"幂等键 {key} 已用于不同请求")
        return json.loads(row["result_json"])

    def _idem_save(self, conn: sqlite3.Connection, key: str | None, request: dict,
                   event_id: int | None, result: Any) -> None:
        if not key:
            return
        digest = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        conn.execute(
            "INSERT INTO idempotency (idem_key, request_hash, event_id, result_json, created_at) VALUES (?,?,?,?,?)",
            (key, digest, event_id, json.dumps(result, ensure_ascii=False, sort_keys=True), now()),
        )

    # 主数据 ---------------------------------------------------------------
    def register_staff(self, staff_id: str, name: str, role: str) -> dict:
        if role not in VALID_ROLES:
            raise ValidationError(f"角色必须是：{'、'.join(sorted(VALID_ROLES))}")
        conn, repo = self._repo()
        try:
            with transaction(conn):
                if repo.one("SELECT 1 FROM staff WHERE staff_id=?", (staff_id,)):
                    raise Conflict(f"人员已存在：{staff_id}")
                conn.execute("INSERT INTO staff (staff_id,name,role,created_at) VALUES (?,?,?,?)",
                             (staff_id, name, role, now()))
            return {"staff_id": staff_id, "name": name, "role": role}
        finally:
            conn.close()

    def define_kind(self, kind_code: str, name: str, *, unit: str = "件",
                    tracking: str = "单件") -> dict:
        if tracking not in ("批次", "单件"):
            raise ValidationError("tracking 只能是 批次 或 单件")
        conn, repo = self._repo()
        try:
            with transaction(conn):
                if repo.one("SELECT 1 FROM item_kinds WHERE kind_code=?", (kind_code,)):
                    raise Conflict(f"物资种类已存在：{kind_code}")
                conn.execute("INSERT INTO item_kinds (kind_code,name,unit,tracking) VALUES (?,?,?,?)",
                             (kind_code, name, unit, tracking))
            return {"kind_code": kind_code, "name": name, "unit": unit, "tracking": tracking}
        finally:
            conn.close()

    def define_set(self, set_code: str, name: str, components: dict[str, int]) -> dict:
        """登记套装组成。components: {种类代码: 每套数量}，批次/单件粒度可混用。"""
        if not components:
            raise ValidationError("套装至少包含一种组成物资")
        if any(not isinstance(q, int) or q <= 0 for q in components.values()):
            raise ValidationError("组成数量必须是正整数")
        conn, repo = self._repo()
        try:
            with transaction(conn):
                for kind_code in components:
                    if repo.one("SELECT 1 FROM item_kinds WHERE kind_code=?", (kind_code,)) is None:
                        raise ValidationError(f"物资种类不存在：{kind_code}")
                row = repo.one("SELECT version FROM sets WHERE set_code=?", (set_code,))
                version = 1
                if row is not None:
                    version = row["version"] + 1
                    conn.execute("UPDATE sets SET name=?, version=?, active=1 WHERE set_code=?",
                                 (name, version, set_code))
                    conn.execute("DELETE FROM set_components WHERE set_code=?", (set_code,))
                else:
                    conn.execute("INSERT INTO sets (set_code,name,version,active,created_at) VALUES (?,?,?,1,?)",
                                 (set_code, name, version, now()))
                conn.executemany(
                    "INSERT INTO set_components (set_code,kind_code,quantity) VALUES (?,?,?)",
                    [(set_code, k, q) for k, q in components.items()],
                )
            return {"set_code": set_code, "name": name, "version": version, "components": components}
        finally:
            conn.close()

    def receive_batch(self, batch_no: str, kind_code: str, total_qty: int, *,
                      supplier: str | None = None, note: str | None = None,
                      actor_id: str | None = None, idem_key: str | None = None) -> dict:
        """批次入库：一条批次汇总分录 + 逐件建立实物谱系（编码 {批次}-{序号}）。"""
        if not isinstance(total_qty, int) or total_qty <= 0:
            raise ValidationError("入库数量必须是正整数")
        request = {"op": "receive_batch", "batch_no": batch_no, "kind_code": kind_code,
                   "total_qty": total_qty}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                kind = repo.get_or_404("item_kinds", "kind_code", kind_code, "物资种类")
                if repo.one("SELECT 1 FROM batches WHERE batch_no=?", (batch_no,)):
                    raise Conflict(f"批次已存在：{batch_no}")
                conn.execute(
                    "INSERT INTO batches (batch_no,kind_code,total_qty,supplier,received_at,note) VALUES (?,?,?,?,?,?)",
                    (batch_no, kind_code, total_qty, supplier, now(), note),
                )
                event_id = repo.record_event("入库", "备货", actor_id=actor_id,
                                             payload={"batch_no": batch_no, "total_qty": total_qty})
                items = []
                width = max(4, len(str(total_qty)))
                for seq in range(1, total_qty + 1):
                    code = f"{batch_no}-{seq:0{width}d}"
                    conn.execute(
                        "INSERT INTO items (item_code,kind_code,batch_no,seq,current_status) VALUES (?,?,?,?,'备货')",
                        (code, kind_code, batch_no, seq),
                    )
                    repo.post_item_move(event_id, item={"batch_no": batch_no, "item_code": code,
                                                        "kind_code": kind_code},
                                        from_bucket=None, to_bucket="备货", stage="备货",
                                        note="批次入库")
                    items.append(code)
                result = {"batch_no": batch_no, "kind_code": kind_code,
                          "tracking": kind["tracking"], "total_qty": total_qty, "items": items,
                          "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    # 场次与预约 -----------------------------------------------------------
    def create_show(self, show_id: str, school: str, *, scheduled_at: str | None = None,
                    created_by: str | None = None, note: str | None = None) -> dict:
        conn = self.db.connect()
        try:
            with transaction(conn):
                if conn.execute("SELECT 1 FROM shows WHERE show_id=?", (show_id,)).fetchone():
                    raise Conflict(f"场次已存在：{show_id}")
                conn.execute(
                    "INSERT INTO shows (show_id,school,scheduled_at,created_by,note) VALUES (?,?,?,?,?)",
                    (show_id, school, scheduled_at, created_by, note),
                )
            return {"show_id": show_id, "school": school, "status": "草稿"}
        finally:
            conn.close()

    def add_reservation(self, reservation_id: str, show_id: str, set_code: str,
                        set_units: int = 1) -> dict:
        """为场次预约若干套套装；组成在此时快照，之后套装改版不影响本预约。"""
        if not isinstance(set_units, int) or set_units <= 0:
            raise ValidationError("套装数量必须是正整数")
        conn, repo = self._repo()
        try:
            with transaction(conn):
                show = repo.get_or_404("shows", "show_id", show_id, "场次")
                if show["status"] != "草稿":
                    raise IllegalTransition(f"场次 {show_id} 已确认，不能追加预约")
                repo.get_or_404("sets", "set_code", set_code, "套装")
                if repo.one("SELECT 1 FROM reservations WHERE reservation_id=?", (reservation_id,)):
                    raise Conflict(f"预约已存在：{reservation_id}")
                conn.execute(
                    "INSERT INTO reservations (reservation_id,show_id,set_code,set_units,status,created_at) "
                    "VALUES (?,?,?,?,'计划中',?)",
                    (reservation_id, show_id, set_code, set_units, now()),
                )
                components = repo.all(
                    "SELECT kind_code, quantity FROM set_components WHERE set_code=?", (set_code,))
                snapshot = {}
                for c in components:
                    qty = c["quantity"] * set_units
                    snapshot[c["kind_code"]] = qty
                    conn.execute(
                        "INSERT INTO reservation_components (reservation_id,kind_code,qty_required) VALUES (?,?,?)",
                        (reservation_id, c["kind_code"], qty),
                    )
                return {"reservation_id": reservation_id, "show_id": show_id,
                        "set_code": set_code, "set_units": set_units, "required": snapshot}
        finally:
            conn.close()

    def confirm_show(self, show_id: str, *, actor_id: str | None = None,
                     idem_key: str | None = None) -> dict:
        """场次确认：一次性锁定足量库存，任一组成不足则整场回滚（不产生半锁定）。"""
        request = {"op": "confirm_show", "show_id": show_id}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                show = repo.get_or_404("shows", "show_id", show_id, "场次")
                if show["status"] != "草稿":
                    raise IllegalTransition(f"场次 {show_id} 状态为 {show['status']}，无法确认")
                reservations = repo.all(
                    "SELECT * FROM reservations WHERE show_id=? AND status='计划中' ORDER BY reservation_id",
                    (show_id,))
                if not reservations:
                    raise ValidationError(f"场次 {show_id} 没有待确认的预约")

                # 1) 汇总需求并验证可借量；跨预约累计已选，避免同一件被重复挑中
                requirements: list[tuple[sqlite3.Row, list[sqlite3.Row]]] = []
                shortages = []
                claimed: set[str] = set()
                for res in reservations:
                    comps = repo.all(
                        "SELECT * FROM reservation_components WHERE reservation_id=?",
                        (res["reservation_id"],))
                    chosen_rows: list[sqlite3.Row] = []
                    for comp in comps:
                        pool = [it for it in repo.available_items(comp["kind_code"])
                                if it["item_code"] not in claimed]
                        if len(pool) < comp["qty_required"]:
                            shortages.append({
                                "kind_code": comp["kind_code"],
                                "required": comp["qty_required"],
                                "available": len(pool),
                                "short": comp["qty_required"] - len(pool),
                            })
                        else:
                            picked = pool[: comp["qty_required"]]
                            chosen_rows.extend(picked)
                            claimed.update(it["item_code"] for it in picked)
                    requirements.append((res, chosen_rows))
                if shortages:
                    raise InsufficientStock(
                        f"场次 {show_id} 库存不足，无法一次性锁定", details={"shortages": shortages})

                # 2) 足量：按 (预约,批次,种类) 切分锁定分录
                locked_units = []
                for res, chosen in requirements:
                    event_id = repo.record_event(
                        "场次锁定", "占用", actor_id=actor_id, show_id=show_id,
                        reservation_id=res["reservation_id"],
                        payload={"set_code": res["set_code"], "set_units": res["set_units"]})
                    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
                    for item in chosen:
                        grouped.setdefault((item["batch_no"], item["kind_code"]), []).append(item)
                    seq = 0
                    for (batch_no, kind_code), items in sorted(grouped.items()):
                        seq += 1
                        unit_id = f"AU-{res['reservation_id']}-{seq:02d}"
                        conn.execute(
                            "INSERT INTO allocation_units (unit_id,reservation_id,show_id,batch_no,"
                            "kind_code,qty,status,created_at) VALUES (?,?,?,?,?,?,'占用',?)",
                            (unit_id, res["reservation_id"], show_id, batch_no, kind_code,
                             len(items), now()))
                        for item in items:
                            conn.execute(
                                "INSERT INTO allocation_items (unit_id,item_code,state) VALUES (?,?,'占用')",
                                (unit_id, item["item_code"]))
                            conn.execute(
                                "UPDATE items SET current_status='占用', current_holder=? WHERE item_code=?",
                                (show_id, item["item_code"]))
                            repo.post_item_move(event_id, item=item, from_bucket="备货",
                                                to_bucket="占用", stage="占用", show_id=show_id,
                                                reservation_id=res["reservation_id"], unit_id=unit_id)
                        conn.execute(
                            "UPDATE reservation_components SET qty_locked=qty_required WHERE reservation_id=?",
                            (res["reservation_id"],))
                        conn.execute("UPDATE reservations SET status='占用中' WHERE reservation_id=?",
                                     (res["reservation_id"],))
                        locked_units.append({"unit_id": unit_id, "batch_no": batch_no,
                                             "kind_code": kind_code, "qty": len(items),
                                             "items": [i["item_code"] for i in items]})
                conn.execute("UPDATE shows SET status='已锁定', locked_at=? WHERE show_id=?",
                             (now(), show_id))
                result = {"show_id": show_id, "status": "已锁定", "locked_units": locked_units,
                          "locked_item_count": sum(u["qty"] for u in locked_units)}
                self._idem_save(conn, idem_key, request, None, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    def cancel_show(self, show_id: str, *, actor_id: str | None = None,
                    idem_key: str | None = None) -> dict:
        """取消场次：仍在“占用”的库存立即释放回库（修复取消后仍不可借的问题）。

        已有物资出库/在场时不能直接取消，须先完成归还对账。
        """
        request = {"op": "cancel_show", "show_id": show_id}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                show = repo.get_or_404("shows", "show_id", show_id, "场次")
                if show["status"] == "已取消":
                    return {"show_id": show_id, "status": "已取消", "released": 0}
                # 仅在途（出库）或在场（使用）的实物阻止取消；仍在“占用”桶的可直接释放
                outstanding = repo.all(
                    """
                    SELECT ai.item_code, ai.state FROM allocation_items ai
                    JOIN allocation_units au ON au.unit_id=ai.unit_id
                    WHERE au.show_id=? AND ai.state IN ('出库','使用')
                    """, (show_id,))
                if outstanding:
                    raise IllegalTransition(
                        f"场次 {show_id} 已有物资出库或到场，须完成归还对账后再取消",
                        details={"outstanding_items": [r["item_code"] for r in outstanding]})
                shipped_any = repo.scalar(
                    "SELECT COUNT(*) FROM outbound_orders WHERE show_id=? AND shipped_at IS NOT NULL",
                    (show_id,))
                if shipped_any:
                    raise IllegalTransition(
                        f"场次 {show_id} 已发生出库，须完成归还/差异对账后再取消")
                event_id = repo.record_event("释放占用", "占用", actor_id=actor_id, show_id=show_id,
                                             payload={"reason": "场次取消"})
                released = 0
                for unit in repo.all(
                    "SELECT * FROM allocation_units WHERE show_id=? AND status='占用'", (show_id,)):
                    conn.execute("UPDATE allocation_units SET status='已取消' WHERE unit_id=?",
                                 (unit["unit_id"],))
                    for ai in repo.all("SELECT * FROM allocation_items WHERE unit_id=?",
                                       (unit["unit_id"],)):
                        item = repo.one("SELECT * FROM items WHERE item_code=?", (ai["item_code"],))
                        conn.execute(
                            "UPDATE allocation_items SET state='已取消', active=0 WHERE id=?",
                            (ai["id"],))
                        conn.execute(
                            "UPDATE items SET current_status='备货', current_holder=NULL WHERE item_code=?",
                            (ai["item_code"],))
                        repo.post_item_move(event_id, item=item, from_bucket="占用",
                                            to_bucket="备货", stage="占用", show_id=show_id,
                                            unit_id=unit["unit_id"], note="场次取消释放")
                        released += 1
                conn.execute("UPDATE reservations SET status='已取消', released_at=? WHERE show_id=? AND status='占用中'",
                             (now(), show_id))
                conn.execute("UPDATE shows SET status='已取消', cancelled_at=? WHERE show_id=?",
                             (now(), show_id))
                result = {"show_id": show_id, "status": "已取消", "released_items": released}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    # 分批出库 -------------------------------------------------------------
    def create_outbound(self, outbound_id: str, show_id: str, *, created_by: str | None = None,
                        plan: list[dict] | None = None) -> dict:
        """建出库单。plan 可指定 [{unit_id, qty}] 做分批计划；不指定则出库时按实物自动归集。"""
        conn, repo = self._repo()
        try:
            with transaction(conn):
                repo.get_or_404("shows", "show_id", show_id, "场次")
                if repo.one("SELECT 1 FROM outbound_orders WHERE outbound_id=?", (outbound_id,)):
                    raise Conflict(f"出库单已存在：{outbound_id}")
                conn.execute(
                    "INSERT INTO outbound_orders (outbound_id,show_id,created_by,created_at,status) "
                    "VALUES (?,?,?,?,'待出库')",
                    (outbound_id, show_id, created_by, now()))
                if plan:
                    for line in plan:
                        unit = repo.one(
                            "SELECT * FROM allocation_units WHERE unit_id=? AND show_id=?",
                            (line["unit_id"], show_id))
                        if unit is None:
                            raise ValidationError(f"锁定分录不属于该场次：{line['unit_id']}")
                        qty = int(line["qty"])
                        if qty <= 0 or qty > unit["qty"] - unit["qty_out"]:
                            raise ValidationError(
                                f"分录 {line['unit_id']} 本次出库数量非法（剩余可出 "
                                f"{unit['qty'] - unit['qty_out']}）")
                        conn.execute(
                            "INSERT INTO outbound_lines (outbound_id,unit_id,qty) VALUES (?,?,?)",
                            (outbound_id, line["unit_id"], qty))
                return {"outbound_id": outbound_id, "show_id": show_id, "status": "待出库",
                        "planned": bool(plan)}
        finally:
            conn.close()

    def ship_items(self, outbound_id: str, item_codes: Sequence[str], *,
                   actor_id: str | None = None, idem_key: str | None = None) -> dict:
        """分批出库：逐件推进 占用→出库，可多次调用。"""
        request = {"op": "ship_items", "outbound_id": outbound_id, "items": list(item_codes)}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                order = repo.get_or_404("outbound_orders", "outbound_id", outbound_id, "出库单")
                if order["status"] in ("已签收", "已归还", "已关闭"):
                    raise IllegalTransition(f"出库单 {outbound_id} 状态为 {order['status']}，不能再出库")
                if not item_codes:
                    raise ValidationError("出库实物列表不能为空")
                if len(set(item_codes)) != len(item_codes):
                    raise ValidationError("出库清单存在重复实物")
                event_id = repo.record_event("出库", "出库", actor_id=actor_id,
                                             show_id=order["show_id"], outbound_id=outbound_id,
                                             payload={"items": list(item_codes)})
                shipped: dict[str, dict[str, Any]] = {}
                for code in item_codes:
                    item = repo.one("SELECT * FROM items WHERE item_code=?", (code,))
                    if item is None:
                        raise NotFound(f"实物不存在：{code}")
                    ai = repo.one(
                        """
                        SELECT ai.* FROM allocation_items ai
                        JOIN allocation_units au ON au.unit_id=ai.unit_id
                        WHERE ai.item_code=? AND au.show_id=? AND ai.active=1
                        """, (code, order["show_id"]))
                    if ai is None:
                        raise IllegalTransition(f"实物 {code} 未被该场次锁定，不能出库")
                    if ai["state"] != "占用":
                        raise IllegalTransition(f"实物 {code} 当前为「{ai['state']}」，不能出库")
                    unit = repo.one("SELECT * FROM allocation_units WHERE unit_id=?", (ai["unit_id"],))
                    line = repo.one(
                        "SELECT * FROM outbound_lines WHERE outbound_id=? AND unit_id=?",
                        (outbound_id, unit["unit_id"]))
                    if line is None:
                        conn.execute(
                            "INSERT INTO outbound_lines (outbound_id,unit_id,qty,shipped_qty) VALUES (?,?,?,?)",
                            (outbound_id, unit["unit_id"], 0, 1))
                    else:
                        if line["qty"] and line["shipped_qty"] >= line["qty"]:
                            raise IllegalTransition(
                                f"分录 {unit['unit_id']} 本批计划 {line['qty']} 件已出完")
                        conn.execute("UPDATE outbound_lines SET shipped_qty=shipped_qty+1 WHERE id=?",
                                     (line["id"],))
                    conn.execute("UPDATE allocation_items SET state='出库' WHERE id=?", (ai["id"],))
                    conn.execute("UPDATE items SET current_status='出库' WHERE item_code=?", (code,))
                    conn.execute("UPDATE allocation_units SET qty_out=qty_out+1, status='出库' WHERE unit_id=?",
                                 (unit["unit_id"],))
                    conn.execute(
                        "UPDATE reservation_components SET qty_out=qty_out+1 "
                        "WHERE reservation_id=? AND kind_code=?",
                        (unit["reservation_id"], item["kind_code"]))
                    repo.post_item_move(event_id, item=item, from_bucket="占用", to_bucket="出库",
                                        stage="出库", show_id=order["show_id"],
                                        reservation_id=unit["reservation_id"],
                                        unit_id=unit["unit_id"])
                    rec = shipped.setdefault(unit["unit_id"], {"qty": 0, "items": []})
                    rec["qty"] += 1
                    rec["items"].append(code)
                remaining_locked = repo.scalar(
                    "SELECT COUNT(*) FROM allocation_items ai JOIN allocation_units au ON au.unit_id=ai.unit_id "
                    "WHERE au.show_id=? AND ai.state='占用'", (order["show_id"],))
                conn.execute(
                    "UPDATE outbound_orders SET shipped_at=COALESCE(shipped_at,?), status='已出库' WHERE outbound_id=?",
                    (now(), outbound_id))
                if remaining_locked:
                    conn.execute(
                        "UPDATE outbound_orders SET status='部分出库' WHERE outbound_id=?",
                        (outbound_id,))
                self._refresh_show_status(conn, repo, order["show_id"])
                result = {"outbound_id": outbound_id, "shipped": shipped,
                          "shipped_count": len(item_codes),
                          "remaining_locked": remaining_locked, "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    # 现场签收 -------------------------------------------------------------
    def receive(self, outbound_id: str, receiver_id: str, lines: Sequence[dict], *,
                receipt_no: str, note: str | None = None, idem_key: str | None = None) -> dict:
        """学校现场签收。一张出库单仅一次有效签收（重复签收被数据库拒绝）。

        lines: [{item_code, condition: '完好'|'损坏', accepted: true}]
        到场即损坏：直接隔离并登记差异，责任环节记为「出库」（运输环节）。
        """
        cleaned = []
        for ln in lines:
            cleaned.append({
                "item_code": ln["item_code"],
                "condition": ln.get("condition", "完好"),
                "accepted": 1 if ln.get("accepted", True) else 0,
            })
        request = {"op": "receive", "outbound_id": outbound_id, "lines": cleaned}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                order = repo.get_or_404("outbound_orders", "outbound_id", outbound_id, "出库单")
                repo.get_or_404("staff", "staff_id", receiver_id, "签收人")
                if order["status"] in ("待出库",):
                    raise IllegalTransition("出库单尚未出库，不能签收")
                if repo.one(
                    "SELECT 1 FROM receipts WHERE outbound_id=? AND status='有效'", (outbound_id,)):
                    raise DuplicateReceipt(f"出库单 {outbound_id} 已完成有效签收，不能重复签收")
                codes = [ln["item_code"] for ln in cleaned]
                if len(set(codes)) != len(codes):
                    raise ValidationError("签收清单存在重复实物")
                event_id = repo.record_event("签收", "使用", actor_id=receiver_id,
                                             show_id=order["show_id"], outbound_id=outbound_id,
                                             payload={"receipt_no": receipt_no, "lines": cleaned})
                conn.execute(
                    "INSERT INTO receipts (receipt_no,outbound_id,receiver_id,signed_at,note) VALUES (?,?,?,?,?)",
                    (receipt_no, outbound_id, receiver_id, now(), note))
                accepted, damaged_arrival = [], []
                for ln in cleaned:
                    code = ln["item_code"]
                    item = repo.one("SELECT * FROM items WHERE item_code=?", (code,))
                    if item is None:
                        raise NotFound(f"实物不存在：{code}")
                    ai = repo.one(
                        """
                        SELECT ai.* FROM allocation_items ai
                        JOIN allocation_units au ON au.unit_id=ai.unit_id
                        JOIN outbound_orders oo ON oo.show_id=au.show_id
                        WHERE ai.item_code=? AND oo.outbound_id=?
                        """, (code, outbound_id))
                    if ai is None:
                        raise IllegalTransition(f"实物 {code} 不属于本出库单")
                    if ai["state"] != "出库":
                        raise IllegalTransition(f"实物 {code} 当前为「{ai['state']}」，不能签收")
                    conn.execute(
                        "INSERT INTO receipt_lines (receipt_no,unit_id,item_code,accepted,condition_on_arrival) "
                        "VALUES (?,?,?,?,?)",
                        (receipt_no, ai["unit_id"], code, ln["accepted"], ln["condition"]))
                    if not ln["accepted"]:
                        continue  # 拒收：仍在途，不计签收量
                    unit = repo.one("SELECT * FROM allocation_units WHERE unit_id=?", (ai["unit_id"],))
                    if ln["condition"] == "损坏":
                        self._isolate_damaged(conn, repo, event_id, item, ai, unit,
                                              responsible_stage="出库",
                                              note=f"签收时发现损坏（签收单 {receipt_no}）")
                        damaged_arrival.append(code)
                    else:
                        conn.execute("UPDATE allocation_items SET state='使用' WHERE id=?", (ai["id"],))
                        conn.execute("UPDATE items SET current_status='使用' WHERE item_code=?", (code,))
                        conn.execute(
                            "UPDATE allocation_units SET qty_received=qty_received+1, status='使用' WHERE unit_id=?",
                            (unit["unit_id"],))
                        conn.execute(
                            "UPDATE reservation_components SET qty_received=qty_received+1 WHERE reservation_id=? AND kind_code=?",
                            (unit["reservation_id"], item["kind_code"]))
                        repo.post_item_move(event_id, item=item, from_bucket="出库", to_bucket="使用",
                                            stage="使用", show_id=order["show_id"],
                                            reservation_id=unit["reservation_id"],
                                            unit_id=unit["unit_id"])
                        accepted.append(code)
                conn.execute(
                    "UPDATE outbound_orders SET status='已签收', received_at=COALESCE(received_at,?) WHERE outbound_id=?",
                    (now(), outbound_id))
                self._refresh_show_status(conn, repo, order["show_id"])
                result = {"receipt_no": receipt_no, "outbound_id": outbound_id,
                          "accepted": accepted, "damaged_on_arrival": damaged_arrival,
                          "accepted_count": len(accepted),
                          "damaged_arrival_count": len(damaged_arrival), "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    # 归还与差异 -----------------------------------------------------------
    def return_items(self, return_no: str, outbound_id: str, handler_id: str,
                     lines: Sequence[dict], *, note: str | None = None,
                     idem_key: str | None = None) -> dict:
        """部分或全部归还，可多次调用。

        lines: [{item_code, condition: '完好'|'损坏'|'缺失'}]
          完好：使用→归还→重新备货（同一条归还事件留下归还分录）；
          损坏：使用→损坏，隔离并登记差异，责任环节「使用」；
          缺失：使用→丢失，登记差异，责任环节「使用」。
        """
        norm = []
        for ln in lines:
            cond = ln.get("condition", "完好")
            if cond not in ("完好", "损坏", "缺失"):
                raise ValidationError("condition 只能是 完好/损坏/缺失")
            norm.append({"item_code": ln.get("item_code"), "condition": cond})
        request = {"op": "return_items", "return_no": return_no,
                   "outbound_id": outbound_id, "lines": norm}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                order = repo.get_or_404("outbound_orders", "outbound_id", outbound_id, "出库单")
                repo.get_or_404("staff", "staff_id", handler_id, "经手人")
                if repo.one("SELECT 1 FROM returns WHERE return_no=?", (return_no,)):
                    raise Conflict(f"归还单已存在：{return_no}")
                event_id = repo.record_event("归还", "归还", actor_id=handler_id,
                                             show_id=order["show_id"], outbound_id=outbound_id,
                                             payload={"return_no": return_no, "lines": norm})
                conn.execute(
                    "INSERT INTO returns (return_no,outbound_id,returned_at,handler_id,note) VALUES (?,?,?,?,?)",
                    (return_no, outbound_id, now(), handler_id, note))
                good, damaged, missing = [], [], []
                for ln in norm:
                    code = ln["item_code"]
                    item = repo.one("SELECT * FROM items WHERE item_code=?", (code,))
                    if item is None:
                        raise NotFound(f"实物不存在：{code}")
                    ai = repo.one(
                        """
                        SELECT ai.* FROM allocation_items ai
                        JOIN allocation_units au ON au.unit_id=ai.unit_id
                        WHERE ai.item_code=? AND au.show_id=?
                        """, (code, order["show_id"]))
                    if ai is None:
                        raise IllegalTransition(f"实物 {code} 不属于该场次")
                    if ai["state"] != "使用":
                        raise IllegalTransition(
                            f"实物 {code} 当前为「{ai['state']}」，不是使用中，不能归还（防止重复归还）")
                    unit = repo.one("SELECT * FROM allocation_units WHERE unit_id=?", (ai["unit_id"],))
                    conn.execute(
                        "INSERT INTO return_lines (return_no,unit_id,item_code,condition,qty) VALUES (?,?,?,?,1)",
                        (return_no, unit["unit_id"], code, ln["condition"]))
                    if ln["condition"] == "完好":
                        # 使用→归还（留下归还轨迹），随后验收重新入库 归还→备货
                        repo.post_item_move(event_id, item=item, from_bucket="使用", to_bucket="归还",
                                            stage="归还", show_id=order["show_id"],
                                            reservation_id=unit["reservation_id"],
                                            unit_id=unit["unit_id"], note="现场完好归还")
                        repo.post_item_move(event_id, item=item, from_bucket="归还", to_bucket="备货",
                                            stage="备货", show_id=order["show_id"],
                                            reservation_id=unit["reservation_id"],
                                            unit_id=unit["unit_id"], note="归还验收合格重新入库")
                        conn.execute("UPDATE allocation_items SET state='归还', active=0 WHERE id=?",
                                     (ai["id"],))
                        conn.execute(
                            "UPDATE items SET current_status='备货', current_holder=NULL, quarantine=0 WHERE item_code=?",
                            (code,))
                        conn.execute(
                            "UPDATE allocation_units SET qty_returned=qty_returned+1 WHERE unit_id=?",
                            (unit["unit_id"],))
                        conn.execute(
                            "UPDATE reservation_components SET qty_returned=qty_returned+1 WHERE reservation_id=? AND kind_code=?",
                            (unit["reservation_id"], item["kind_code"]))
                        good.append(code)
                    elif ln["condition"] == "损坏":
                        self._isolate_damaged(conn, repo, event_id, item, ai, unit,
                                              responsible_stage="使用",
                                              note=f"归还时损坏（归还单 {return_no}）")
                        damaged.append(code)
                    else:
                        conn.execute("UPDATE allocation_items SET state='丢失', active=0 WHERE id=?",
                                     (ai["id"],))
                        conn.execute("UPDATE items SET current_status='丢失' WHERE item_code=?", (code,))
                        conn.execute("UPDATE allocation_units SET qty_lost=qty_lost+1 WHERE unit_id=?",
                                     (unit["unit_id"],))
                        conn.execute(
                            "UPDATE reservation_components SET qty_lost=qty_lost+1 WHERE reservation_id=? AND kind_code=?",
                            (unit["reservation_id"], item["kind_code"]))
                        repo.post_item_move(event_id, item=item, from_bucket="使用", to_bucket="丢失",
                                            stage="归还", show_id=order["show_id"],
                                            reservation_id=unit["reservation_id"],
                                            unit_id=unit["unit_id"], note="归还差异：缺失")
                        self._open_discrepancy(conn, repo, event_id, kind="丢失", item=item,
                                               unit=unit, show_id=order["show_id"],
                                               responsible_stage="使用")
                        missing.append(code)
                    self._refresh_unit(conn, repo, unit["unit_id"])
                conn.execute(
                    "UPDATE outbound_orders SET status=CASE WHEN status='已签收' THEN '部分归还' ELSE status END WHERE outbound_id=?",
                    (outbound_id,))
                # 本出库单覆盖的实物全部结清（无在途/在场）才关单；
                # 范围 = 本单各行分录的实物 + 该场次的临时替换件，其他出库单不受影响
                order_active = repo.scalar(
                    """
                    SELECT COUNT(*) FROM allocation_items ai
                    JOIN allocation_units au ON au.unit_id=ai.unit_id
                    LEFT JOIN outbound_lines ol ON ol.unit_id=au.unit_id AND ol.outbound_id=?
                    WHERE au.show_id = (SELECT show_id FROM outbound_orders WHERE outbound_id=?)
                      AND ai.active=1
                      AND (ol.id IS NOT NULL OR au.unit_id LIKE '%-SUB%')
                    """, (outbound_id, outbound_id))
                if order_active == 0:
                    conn.execute("UPDATE outbound_orders SET status='已归还', closed_at=? WHERE outbound_id=?",
                                 (now(), outbound_id))
                self._refresh_show_status(conn, repo, order["show_id"])
                result = {"return_no": return_no, "good": good, "damaged": damaged,
                          "missing": missing, "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    def _isolate_damaged(self, conn, repo, event_id, item, ai, unit, *,
                         responsible_stage: str, note: str) -> None:
        """损坏隔离：进入损坏桶、置隔离标志、登记差异分录。"""
        from_bucket = {"出库": "出库", "使用": "使用", "备货": "备货"}.get(ai["state"], ai["state"])
        conn.execute("UPDATE allocation_items SET state='损坏', active=0 WHERE id=?", (ai["id"],))
        conn.execute("UPDATE items SET current_status='损坏', quarantine=1 WHERE item_code=?",
                     (item["item_code"],))
        conn.execute("UPDATE allocation_units SET qty_damaged=qty_damaged+1 WHERE unit_id=?",
                     (unit["unit_id"],))
        conn.execute(
            "UPDATE reservation_components SET qty_damaged=qty_damaged+1 WHERE reservation_id=? AND kind_code=?",
            (unit["reservation_id"], item["kind_code"]))
        stage = "出库" if responsible_stage == "出库" else "归还"
        repo.post_item_move(event_id, item=item, from_bucket=from_bucket, to_bucket="损坏",
                            stage=stage, show_id=unit["show_id"],
                            reservation_id=unit["reservation_id"], unit_id=unit["unit_id"], note=note)
        self._open_discrepancy(conn, repo, event_id, kind="损坏", item=item, unit=unit,
                               show_id=unit["show_id"], responsible_stage=responsible_stage)
        self._refresh_unit(conn, repo, unit["unit_id"])

    # 现场报损与临时替换 ----------------------------------------------------
    def report_damage(self, item_code: str, *, actor_id: str | None = None,
                      note: str | None = None, idem_key: str | None = None) -> dict:
        """使用中或在库发现损坏，立即隔离。"""
        request = {"op": "report_damage", "item_code": item_code}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                item = repo.one("SELECT * FROM items WHERE item_code=?", (item_code,))
                if item is None:
                    raise NotFound(f"实物不存在：{item_code}")
                if item["current_status"] in DAMAGE_BUCKETS or item["quarantine"]:
                    raise IllegalTransition(f"实物 {item_code} 已处于隔离/差异状态")
                if item["current_status"] not in ("使用", "备货", "出库"):
                    raise IllegalTransition(f"实物 {item_code} 当前为「{item['current_status']}」，不能报损")
                ai = repo.one("SELECT * FROM allocation_items WHERE item_code=? AND active=1", (item_code,))
                unit = repo.one("SELECT * FROM allocation_units WHERE unit_id=?",
                                (ai["unit_id"],)) if ai else None
                show_id = unit["show_id"] if unit else None
                event_id = repo.record_event("损坏隔离", "使用" if item["current_status"] == "使用" else "备货",
                                             actor_id=actor_id, show_id=show_id,
                                             reservation_id=unit["reservation_id"] if unit else None,
                                             payload={"item_code": item_code, "note": note})
                if ai is not None and unit is not None:
                    self._isolate_damaged(conn, repo, event_id, item, ai, unit,
                                          responsible_stage="使用", note=note or "使用中报损隔离")
                    if show_id:
                        self._refresh_show_status(conn, repo, show_id)
                else:
                    conn.execute("UPDATE items SET current_status='损坏', quarantine=1 WHERE item_code=?",
                                 (item_code,))
                    repo.post_item_move(event_id, item=item, from_bucket="备货", to_bucket="损坏",
                                        stage="备货", note=note or "在库报损隔离")
                    self._open_discrepancy(conn, repo, event_id, kind="损坏", item=item,
                                           unit=None, show_id=None, responsible_stage="备货")
                result = {"item_code": item_code, "status": "损坏", "quarantine": True,
                          "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    def substitute(self, show_id: str, original_item_code: str, replacement_item_code: str,
                   *, actor_id: str | None = None, note: str | None = None,
                   idem_key: str | None = None) -> dict:
        """临时替换：原物须已隔离（损坏/丢失），备件立即沿 备货→出库→使用 补到该场次。"""
        request = {"op": "substitute", "show_id": show_id, "original": original_item_code,
                   "replacement": replacement_item_code}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                show = repo.get_or_404("shows", "show_id", show_id, "场次")
                original = repo.one("SELECT * FROM items WHERE item_code=?", (original_item_code,))
                repl = repo.one("SELECT * FROM items WHERE item_code=?", (replacement_item_code,))
                if original is None or repl is None:
                    raise NotFound("原物或替换件不存在")
                if original["kind_code"] != repl["kind_code"]:
                    raise ValidationError("替换件必须与原物是同一种类")
                if repl["current_status"] != "备货" or repl["quarantine"]:
                    raise IllegalTransition(f"替换件 {replacement_item_code} 当前不可用（{repl['current_status']}）")
                orig_ai = repo.one(
                    """
                    SELECT ai.* FROM allocation_items ai JOIN allocation_units au ON au.unit_id=ai.unit_id
                    WHERE ai.item_code=? AND au.show_id=?
                    """, (original_item_code, show_id))
                if orig_ai is None:
                    raise IllegalTransition(f"原物 {original_item_code} 不属于场次 {show_id}")
                if orig_ai["state"] not in ("损坏", "丢失"):
                    raise IllegalTransition("原物须先完成损坏隔离或丢失登记，才能临时替换")
                orig_unit = repo.one("SELECT * FROM allocation_units WHERE unit_id=?",
                                     (orig_ai["unit_id"],))
                event_id = repo.record_event("临时替换", "使用", actor_id=actor_id, show_id=show_id,
                                             reservation_id=orig_unit["reservation_id"],
                                             payload={"original": original_item_code,
                                                      "replacement": replacement_item_code,
                                                      "note": note})
                # 为替换件新建同预约的补充分录，直接补到使用环节
                sub_no = repo.scalar(
                    "SELECT COALESCE(MAX(substitution_id),0)+1 FROM substitutions")
                unit_id = f"AU-{orig_unit['reservation_id']}-SUB{sub_no}"
                conn.execute(
                    "INSERT INTO allocation_units (unit_id,reservation_id,show_id,batch_no,kind_code,qty,status,created_at) "
                    "VALUES (?,?,?,?,?,?,'使用',?)",
                    (unit_id, orig_unit["reservation_id"], show_id, repl["batch_no"],
                     repl["kind_code"], 1, now()))
                conn.execute(
                    "INSERT INTO allocation_items (unit_id,item_code,state) VALUES (?,?,'使用')",
                    (unit_id, replacement_item_code))
                conn.execute("UPDATE items SET current_status='使用', current_holder=? WHERE item_code=?",
                             (show_id, replacement_item_code))
                repo.post_item_move(event_id, item=repl, from_bucket="备货", to_bucket="出库",
                                    stage="出库", show_id=show_id,
                                    reservation_id=orig_unit["reservation_id"], unit_id=unit_id,
                                    note="临时替换：备件出库")
                repo.post_item_move(event_id, item=repl, from_bucket="出库", to_bucket="使用",
                                    stage="使用", show_id=show_id,
                                    reservation_id=orig_unit["reservation_id"], unit_id=unit_id,
                                    note="临时替换：备件到场")
                cur = conn.execute(
                    "INSERT INTO substitutions (show_id,reservation_id,original_item_code,"
                    "replacement_item_code,opened_event_id,opened_at,note) VALUES (?,?,?,?,?,?,?)",
                    (show_id, orig_unit["reservation_id"], original_item_code,
                     replacement_item_code, event_id, now(), note))
                result = {"substitution_id": cur.lastrowid, "show_id": show_id,
                          "original": original_item_code, "replacement": replacement_item_code,
                          "unit_id": unit_id, "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    def swap_back(self, substitution_id: int, *, condition: str = "完好",
                  actor_id: str | None = None, note: str | None = None,
                  idem_key: str | None = None) -> dict:
        """替换件撤回：备件按 完好/损坏/缺失 结账，替换关系结案（已换回）。"""
        if condition not in ("完好", "损坏", "缺失"):
            raise ValidationError("condition 只能是 完好/损坏/缺失")
        request = {"op": "swap_back", "substitution_id": substitution_id, "condition": condition}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                sub = repo.one("SELECT * FROM substitutions WHERE substitution_id=?",
                               (substitution_id,))
                if sub is None:
                    raise NotFound(f"替换关系不存在：{substitution_id}")
                if sub["status"] != "替换在用":
                    raise IllegalTransition(f"替换关系已结案：{sub['status']}")
                repl = repo.one("SELECT * FROM items WHERE item_code=?",
                                (sub["replacement_item_code"],))
                ai = repo.one("SELECT * FROM allocation_items WHERE item_code=? AND active=1",
                              (sub["replacement_item_code"],))
                if ai is None or ai["state"] != "使用":
                    raise IllegalTransition("替换件当前不在使用中，不能换回")
                unit = repo.one("SELECT * FROM allocation_units WHERE unit_id=?", (ai["unit_id"],))
                event_id = repo.record_event("换回", "归还", actor_id=actor_id,
                                             show_id=sub["show_id"],
                                             reservation_id=sub["reservation_id"],
                                             payload={"substitution_id": substitution_id,
                                                      "replacement": sub["replacement_item_code"],
                                                      "condition": condition, "note": note})
                if condition == "完好":
                    repo.post_item_move(event_id, item=repl, from_bucket="使用", to_bucket="归还",
                                        stage="归还", show_id=sub["show_id"],
                                        reservation_id=sub["reservation_id"],
                                        unit_id=unit["unit_id"], note="替换件完好撤回")
                    repo.post_item_move(event_id, item=repl, from_bucket="归还", to_bucket="备货",
                                        stage="备货", show_id=sub["show_id"],
                                        reservation_id=sub["reservation_id"],
                                        unit_id=unit["unit_id"], note="替换件验收入库")
                    conn.execute("UPDATE allocation_items SET state='归还', active=0 WHERE id=?",
                                 (ai["id"],))
                    conn.execute(
                        "UPDATE items SET current_status='备货', current_holder=NULL, quarantine=0 WHERE item_code=?",
                        (repl["item_code"],))
                    conn.execute("UPDATE allocation_units SET qty_returned=qty_returned+1 WHERE unit_id=?",
                                 (unit["unit_id"],))
                elif condition == "损坏":
                    self._isolate_damaged(conn, repo, event_id, repl, ai, unit,
                                          responsible_stage="使用", note="替换件撤回时损坏")
                else:
                    conn.execute("UPDATE allocation_items SET state='丢失', active=0 WHERE id=?",
                                 (ai["id"],))
                    conn.execute("UPDATE items SET current_status='丢失' WHERE item_code=?",
                                 (repl["item_code"],))
                    conn.execute("UPDATE allocation_units SET qty_lost=qty_lost+1 WHERE unit_id=?",
                                 (unit["unit_id"],))
                    repo.post_item_move(event_id, item=repl, from_bucket="使用", to_bucket="丢失",
                                        stage="归还", show_id=sub["show_id"],
                                        reservation_id=sub["reservation_id"],
                                        unit_id=unit["unit_id"], note="替换件撤回时缺失")
                    self._open_discrepancy(conn, repo, event_id, kind="丢失", item=repl, unit=unit,
                                           show_id=sub["show_id"], responsible_stage="使用")
                self._refresh_unit(conn, repo, unit["unit_id"])
                conn.execute(
                    "UPDATE substitutions SET status='已换回', closed_event_id=?, closed_at=? WHERE substitution_id=?",
                    (event_id, now(), substitution_id))
                self._refresh_show_status(conn, repo, sub["show_id"])
                result = {"substitution_id": substitution_id, "status": "已换回",
                          "replacement": sub["replacement_item_code"], "condition": condition,
                          "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    # 差异核销 -------------------------------------------------------------
    def resolve_discrepancy(self, discrepancy_id: int, resolution: str, *,
                            action: str = "核销", actor_id: str | None = None,
                            idem_key: str | None = None) -> dict:
        """差异后续对账：损坏件可「报废」或「修复入库」；丢失件按「核销」结案。"""
        if action not in ("报废", "修复入库", "核销"):
            raise ValidationError("action 只能是 报废/修复入库/核销")
        request = {"op": "resolve_discrepancy", "id": discrepancy_id, "action": action}
        conn, repo = self._repo()
        try:
            with transaction(conn):
                cached = self._idem_replay(repo, idem_key, request)
                if cached is not None:
                    return cached
                d = repo.one("SELECT * FROM discrepancies WHERE discrepancy_id=?", (discrepancy_id,))
                if d is None:
                    raise NotFound(f"差异不存在：{discrepancy_id}")
                if d["status"] == "已核销":
                    raise IllegalTransition("差异已核销")
                item = repo.one("SELECT * FROM items WHERE item_code=?", (d["item_code"],))
                event_id = repo.record_event("核销", "归还", actor_id=actor_id, show_id=d["show_id"],
                                             reservation_id=d["reservation_id"],
                                             payload={"discrepancy_id": discrepancy_id,
                                                      "action": action, "resolution": resolution})
                if item is not None and action == "修复入库" and item["current_status"] == "损坏":
                    repo.post_item_move(event_id, item=item, from_bucket="损坏", to_bucket="备货",
                                        stage="备货", note=f"差异修复入库：{resolution}")
                    conn.execute(
                        "UPDATE items SET current_status='备货', quarantine=0 WHERE item_code=?",
                        (item["item_code"],))
                elif item is not None:
                    target = "已报废"
                    repo.post_item_move(event_id, item=item,
                                        from_bucket=item["current_status"]
                                        if item["current_status"] in ("损坏", "丢失") else None,
                                        to_bucket=target, stage="归还",
                                        note=f"差异{action}：{resolution}")
                    conn.execute(
                        "UPDATE items SET current_status='已报废', quarantine=1 WHERE item_code=?",
                        (item["item_code"],))
                conn.execute(
                    "UPDATE discrepancies SET status='已核销', closed_event_id=?, closed_at=?, resolution=? WHERE discrepancy_id=?",
                    (event_id, now(), resolution, discrepancy_id))
                sub = repo.one(
                    "SELECT * FROM substitutions WHERE original_item_code=? AND status='替换在用'",
                    (d["item_code"],))
                result = {"discrepancy_id": discrepancy_id, "status": "已核销",
                          "action": action, "event_id": event_id}
                self._idem_save(conn, idem_key, request, event_id, result)
                return result
        except sqlite3.IntegrityError as exc:
            raise self._translate_integrity(exc)
        finally:
            conn.close()

    # 查询与对账 -----------------------------------------------------------
    def locate_item(self, item_code: str) -> dict:
        """仓库定位：这件东西现在到底在哪、由谁占用、处于哪个责任环节。"""
        conn, repo = self._repo()
        try:
            item = repo.one(
                """
                SELECT i.*, k.name kind_name, k.tracking, b.supplier
                FROM items i JOIN item_kinds k ON k.kind_code=i.kind_code
                JOIN batches b ON b.batch_no=i.batch_no
                WHERE i.item_code=?
                """, (item_code,))
            if item is None:
                raise NotFound(f"实物不存在：{item_code}")
            ai = repo.one(
                """
                SELECT ai.state, au.unit_id, au.show_id, au.reservation_id, s.school
                FROM allocation_items ai JOIN allocation_units au ON au.unit_id=ai.unit_id
                JOIN shows s ON s.show_id=au.show_id
                WHERE ai.item_code=? ORDER BY ai.id DESC LIMIT 1
                """, (item_code,))
            stage_map = {"占用": "占用", "出库": "出库", "使用": "使用", "归还": "归还",
                         "损坏": "差异", "丢失": "差异", "已取消": "备货", "已报废": "归还"}
            result = {
                "item_code": item_code,
                "kind_code": item["kind_code"],
                "kind_name": item["kind_name"],
                "tracking": item["tracking"],
                "batch_no": item["batch_no"],
                "seq": item["seq"],
                "supplier": item["supplier"],
                "current_status": item["current_status"],
                "quarantine": bool(item["quarantine"]),
                "holder_show": item["current_holder"],
                "school": ai["school"] if ai else None,
                "unit_id": ai["unit_id"] if ai else None,
                "reservation_id": ai["reservation_id"] if ai else None,
                "responsible_stage": stage_map.get(item["current_status"], item["current_status"]),
            }
            return result
        finally:
            conn.close()

    def item_history(self, item_code: str) -> dict:
        """沿单件分录还原去向与责任环节。"""
        conn, repo = self._repo()
        try:
            if repo.one("SELECT 1 FROM items WHERE item_code=?", (item_code,)) is None:
                raise NotFound(f"实物不存在：{item_code}")
            entries = repo.item_trace(item_code)
            return {"item_code": item_code, "location": self.locate_item(item_code),
                    "timeline": entries}
        finally:
            conn.close()

    def show_overview(self, show_id: str) -> dict:
        conn, repo = self._repo()
        try:
            show = repo.get_or_404("shows", "show_id", show_id, "场次")
            comps = repo.all(
                """
                SELECT rc.kind_code, SUM(rc.qty_required) required, SUM(rc.qty_locked) locked,
                       SUM(rc.qty_out) out_qty, SUM(rc.qty_received) received,
                       SUM(rc.qty_returned) returned, SUM(rc.qty_damaged) damaged,
                       SUM(rc.qty_lost) lost
                FROM reservation_components rc
                JOIN reservations r ON r.reservation_id=rc.reservation_id
                WHERE r.show_id=?
                GROUP BY rc.kind_code
                """, (show_id,))
            units = repo.all(
                "SELECT unit_id,batch_no,kind_code,qty,qty_out,qty_received,qty_returned,qty_damaged,qty_lost,status "
                "FROM allocation_units WHERE show_id=? ORDER BY unit_id", (show_id,))
            outstanding = repo.all(
                "SELECT ai.item_code, ai.state FROM allocation_items ai "
                "JOIN allocation_units au ON au.unit_id=ai.unit_id "
                "WHERE au.show_id=? AND ai.active=1 ORDER BY ai.item_code", (show_id,))
            return {"show_id": show_id, "school": show["school"], "status": show["status"],
                    "components": [dict(c) for c in comps],
                    "allocation_units": [dict(u) for u in units],
                    "outstanding_items": [dict(r) for r in outstanding]}
        finally:
            conn.close()

    def list_discrepancies(self, *, status: str | None = None) -> list[dict]:
        conn, repo = self._repo()
        try:
            sql = ("SELECT d.*, s.school FROM discrepancies d LEFT JOIN shows s ON s.show_id=d.show_id")
            params: tuple = ()
            if status:
                sql += " WHERE d.status=?"
                params = (status,)
            sql += " ORDER BY d.discrepancy_id"
            return [dict(r) for r in repo.all(sql, params)]
        finally:
            conn.close()

    def reconcile(self) -> dict:
        """全库统一分录对账；任何一次中途失败后都可以重跑到一致。"""
        conn, repo = self._repo()
        try:
            with transaction(conn):
                result = repo.reconcile()
            return result
        finally:
            conn.close()

    # 内部工具 -------------------------------------------------------------
    def _open_discrepancy(self, conn, repo, event_id, *, kind, item, unit, show_id,
                          responsible_stage: str) -> int:
        cur = conn.execute(
            """
            INSERT INTO discrepancies (kind,status,scope,item_code,batch_no,kind_code,qty,
                                       show_id,reservation_id,unit_id,responsible_stage,
                                       opened_event_id,opened_at)
            VALUES (?, '待处理','单件',?,?,?,1,?,?,?,?,?,?)
            """,
            (kind, item["item_code"], item["batch_no"], item["kind_code"], show_id,
             unit["reservation_id"] if unit else None, unit["unit_id"] if unit else None,
             responsible_stage, event_id, now()))
        return int(cur.lastrowid)

    def _refresh_unit(self, conn: sqlite3.Connection, repo: Repository, unit_id: str) -> None:
        u = repo.one("SELECT * FROM allocation_units WHERE unit_id=?", (unit_id,))
        accounted = u["qty_returned"] + u["qty_damaged"] + u["qty_lost"]
        if accounted >= u["qty"]:
            conn.execute("UPDATE allocation_units SET status='归还' WHERE unit_id=?", (unit_id,))
            conn.execute(
                "UPDATE reservations SET status='已归还' WHERE reservation_id=? AND NOT EXISTS ("
                "SELECT 1 FROM allocation_units WHERE reservation_id=? AND status!='归还')",
                (u["reservation_id"], u["reservation_id"]))
        elif u["qty_received"] > 0 or u["qty_damaged"] + u["qty_lost"] > 0:
            conn.execute("UPDATE allocation_units SET status='使用' WHERE unit_id=?", (unit_id,))

    def _refresh_show_status(self, conn: sqlite3.Connection, repo: Repository, show_id: str) -> None:
        show = repo.one("SELECT status FROM shows WHERE show_id=?", (show_id,))
        if show is None or show["status"] == "已取消":
            return
        units = repo.all("SELECT * FROM allocation_units WHERE show_id=?", (show_id,))
        out = sum(u["qty_out"] for u in units)
        accounted = sum(u["qty_returned"] + u["qty_damaged"] + u["qty_lost"] for u in units)
        total = sum(u["qty"] for u in units)
        has_valid_receipt = repo.scalar(
            "SELECT COUNT(*) FROM receipts r JOIN outbound_orders o ON o.outbound_id=r.outbound_id "
            "WHERE o.show_id=? AND r.status='有效'", (show_id,))
        has_return = repo.scalar(
            "SELECT COUNT(*) FROM returns r JOIN outbound_orders o ON o.outbound_id=r.outbound_id "
            "WHERE o.show_id=?", (show_id,))
        if total > 0 and accounted >= total:
            status = "已完成"
        elif has_return:
            status = "部分归还"
        elif has_valid_receipt:
            status = "已签收"
        elif total > 0 and out >= total:
            status = "已出库"
        elif out > 0:
            status = "部分出库"
        else:
            status = "已锁定"
        conn.execute("UPDATE shows SET status=? WHERE show_id=?", (status, show_id))

    @staticmethod
    def _translate_integrity(exc: sqlite3.IntegrityError) -> Conflict:
        msg = str(exc)
        if "ux_active_item_allocation" in msg:
            return Conflict("该实物刚被其他并发业务锁定，请重试（库存分录已保持一致）")
        if "ux_one_valid_receipt" in msg:
            return DuplicateReceipt("出库单已存在有效签收，不能重复签收")
        return Conflict(f"数据约束冲突：{msg}")
