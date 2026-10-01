"""库存履约服务端回归测试。

覆盖：批次谱系、套装组成校验、一次性锁定（全有或全无）、并发借用、
分批出库、重复签收幂等、损坏隔离、临时替换、部分归还与归还差异、
分录对账修复、单件全程追溯，以及 HTTP 层与幂等键。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain_contract.validator import load_contract
from prop_inventory import Database, InventoryService
from prop_inventory.api import create_server
from prop_inventory.models import ACTORS, ITEM_STATUSES, DomainError


def make_service() -> InventoryService:
    return InventoryService(Database(":memory:"))


def seed(service: InventoryService, kit_specs, *, template="TPL-TOUR", spare_items=()):
    """登记批次并组装套装。kit_specs: [(kit_id, (幕布id, 木偶id, 木偶id)), ...]"""
    items = []
    for _, members in kit_specs:
        for iid in members:
            category = "幕布" if iid.startswith("MB") else "木偶"
            items.append({"item_id": iid, "category": category, "label": f"物资{iid}"})
    items.extend(spare_items)
    service.register_batch(name="巡演基础批次", actor="王库管", items=items)
    service.create_template(
        template_id=template, name="木偶巡演套装",
        parts=[{"category": "幕布", "quantity": 1}, {"category": "木偶", "quantity": 2}],
    )
    for kit_id, members in kit_specs:
        service.assemble_kit(
            kit_id=kit_id, template_id=template, name=f"套装{kit_id}",
            item_ids=list(members), actor="王库管",
        )


def make_session(service: InventoryService, session_id: str, quantity: int = 1):
    service.create_session(
        session_id=session_id, school="阳光小学", teacher="李老师",
        planned_at="2026-10-10", requirements=[{"template_id": "TPL-TOUR", "quantity": quantity}],
    )


class ContractAlignmentTest(unittest.TestCase):
    """服务端常量必须与领域契约保持一致。"""

    def test_constants_align_with_contract(self):
        contract = load_contract(ROOT / "domain" / "contract.json")
        for actor in ACTORS:
            self.assertIn(actor, contract["actors"])
        for status in ITEM_STATUSES:
            self.assertIn(status, contract["states"])


class BatchAndKitTest(unittest.TestCase):
    def test_register_batch_records_entries_and_lineage(self):
        svc = make_service()
        svc.register_batch(
            batch_id="BAT-A", name="首批采购", source="采购", actor="王库管",
            items=[{"item_id": "MB-01", "category": "幕布", "label": "主幕布"}],
        )
        svc.register_batch(
            batch_id="BAT-B", name="补充批", actor="王库管", parent_batch_id="BAT-A",
            items=[{"item_id": "MB-02", "category": "幕布", "label": "备用幕布"}],
        )
        batch = svc.get_batch("BAT-B")
        self.assertEqual(batch["parent_batch_id"], "BAT-A")
        self.assertEqual([b["batch_id"] for b in batch["lineage"]], ["BAT-A"])
        self.assertEqual(batch["items"][0]["status"], "备货")
        trace = svc.trace_item("MB-01")
        self.assertEqual(trace["location"], "仓库")
        self.assertEqual(trace["entries"][0]["action"], "register")
        self.assertEqual(trace["entries"][0]["actor"], "王库管")
        self.assertEqual(trace["entries"][0]["role"], "器材管理员")

    def test_assemble_kit_validates_composition_and_exclusivity(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))],
             spare_items=[
                 {"item_id": "MB-09", "category": "幕布", "label": "幕布"},
                 {"item_id": "OU-09", "category": "木偶", "label": "木偶"},
             ])
        # 组成与模板不符（少一个木偶）
        with self.assertRaises(DomainError) as ctx:
            svc.assemble_kit(template_id="TPL-TOUR", name="坏套",
                             item_ids=["MB-09", "OU-09"], actor="王库管")
        self.assertEqual(ctx.exception.code, "composition_mismatch")
        self.assertEqual(ctx.exception.status, 422)
        # 已在其他套装中的单件不能重复入套
        with self.assertRaises(DomainError) as ctx:
            svc.assemble_kit(template_id="TPL-TOUR", name="坏套2",
                             item_ids=["MB-01", "OU-09", "OU-02"], actor="王库管")
        self.assertEqual(ctx.exception.code, "already_in_kit")


class ConfirmTest(unittest.TestCase):
    def test_confirm_is_all_or_nothing(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(svc, "S1", quantity=2)  # 只有 1 套，需求 2 套
        with self.assertRaises(DomainError) as ctx:
            svc.confirm_session("S1", actor="王库管")
        self.assertEqual(ctx.exception.code, "insufficient_stock")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.details["shortages"][0]["available"], 1)
        # 全有或全无：没有任何物资被锁定，没有预约记录，没有分录
        self.assertEqual(svc.get_stock()["by_status"].get("占用", 0), 0)
        self.assertEqual(svc.get_session("S1")["status"], "待确认")
        self.assertEqual(svc.query_ledger(action="reserve")["entries"], [])

    def test_confirm_locks_then_cancel_releases(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(svc, "S1")
        result = svc.confirm_session("S1", actor="王库管")
        self.assertEqual(result["locked_items"], 3)
        self.assertEqual(svc.get_stock()["by_status"]["占用"], 3)
        self.assertEqual(svc.get_stock()["kits_ready"], 0)
        self.assertEqual(svc.get_session("S1")["status"], "已确认")
        # 取消场次：幕布等物资必须回到可预约状态（修复“取消后幕布仍不可借”）
        svc.cancel_session("S1", actor="王库管")
        self.assertEqual(svc.get_stock()["by_status"].get("占用", 0), 0)
        self.assertEqual(svc.get_stock()["kits_ready"], 1)
        session = svc.get_session("S1")
        self.assertEqual(session["status"], "已取消")
        self.assertEqual(session["reservations"][0]["status"], "已释放")
        releases = svc.query_ledger(action="release")["entries"]
        self.assertEqual(len(releases), 3)
        # 释放后可以重新锁定
        make_session(svc, "S2")
        svc.confirm_session("S2", actor="王库管")
        self.assertEqual(svc.get_session("S2")["status"], "已确认")

    def test_confirm_twice_rejected(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(svc, "S1")
        svc.confirm_session("S1", actor="王库管")
        with self.assertRaises(DomainError) as ctx:
            svc.confirm_session("S1", actor="王库管")
        self.assertEqual(ctx.exception.code, "session_state")


class ConcurrencyTest(unittest.TestCase):
    """并发借用：多个场次同时确认，不能出现超锁或重复占用。"""

    def _race_confirm(self, svc, session_ids):
        def attempt(sid):
            try:
                return ("ok", svc.confirm_session(sid, actor="王库管"))
            except DomainError as exc:
                return ("fail", exc)

        with ThreadPoolExecutor(max_workers=len(session_ids)) as pool:
            return list(pool.map(attempt, session_ids))

    def test_concurrent_confirm_never_double_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = InventoryService(Database(os.path.join(tmp, "inv.db")))
            seed(svc, [
                ("KIT-01", ("MB-01", "OU-01", "OU-02")),
                ("KIT-02", ("MB-02", "OU-03", "OU-04")),
                ("KIT-03", ("MB-03", "OU-05", "OU-06")),
            ])
            for sid in ("S1", "S2"):
                make_session(svc, sid, quantity=2)  # 各需 2 套，共 3 套
            results = self._race_confirm(svc, ["S1", "S2"])
            ok = [r for r in results if r[0] == "ok"]
            fail = [r for r in results if r[0] == "fail"]
            # 3 套不够两个场次各锁 2 套：恰好一个成功
            self.assertEqual(len(ok), 1)
            self.assertEqual(len(fail), 1)
            self.assertEqual(fail[0][1].code, "insufficient_stock")
            # 占用中的物资全部属于获胜场次，且每件只被锁定一次
            winner = ok[0][1]["session_id"]
            bound = svc.list_items(status="占用")["items"]
            self.assertEqual(len(bound), 6)
            self.assertTrue(all(item["session_id"] == winner for item in bound))
            reserves = svc.query_ledger(action="reserve")["entries"]
            self.assertEqual(len({e["item_id"] for e in reserves}), 6)

    def test_concurrent_confirm_disjoint_when_stock_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            svc = InventoryService(Database(os.path.join(tmp, "inv.db")))
            seed(svc, [
                ("KIT-01", ("MB-01", "OU-01", "OU-02")),
                ("KIT-02", ("MB-02", "OU-03", "OU-04")),
                ("KIT-03", ("MB-03", "OU-05", "OU-06")),
                ("KIT-04", ("MB-04", "OU-07", "OU-08")),
            ])
            for sid in ("S1", "S2"):
                make_session(svc, sid, quantity=2)
            results = self._race_confirm(svc, ["S1", "S2"])
            self.assertTrue(all(r[0] == "ok" for r in results))
            locked = {
                r[1]["session_id"]: {iid for kit in r[1]["locked_kits"] for iid in kit["items"]}
                for r in results
            }
            self.assertFalse(locked["S1"] & locked["S2"])  # 锁定集合互不相交
            self.assertEqual(svc.get_stock()["by_status"]["占用"], 12)


class ShipmentSignTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        seed(self.svc, [
            ("KIT-01", ("MB-01", "OU-01", "OU-02")),
            ("KIT-02", ("MB-02", "OU-03", "OU-04")),
        ])
        make_session(self.svc, "S1", quantity=2)
        self.svc.confirm_session("S1", actor="王库管")

    def test_partial_shipments_and_duplicate_signoff(self):
        svc = self.svc
        shp1 = svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        self.assertEqual(shp1["seq"], 1)
        # 分批出库：KIT-02 拆成两批
        shp2 = svc.create_shipment("S1", actor="王库管", item_ids=["MB-02", "OU-03"])
        shp3 = svc.create_shipment("S1", actor="王库管", item_ids=["OU-04"])
        self.assertEqual((shp2["seq"], shp3["seq"]), (2, 3))
        self.assertEqual(svc.get_session("S1")["status"], "出库中")
        self.assertEqual(svc.trace_item("MB-01")["location"], "在途")
        # 部分签收
        part = svc.sign_shipment(shp2["shipment_id"], signed_by="周校长", items=["MB-02"])
        self.assertEqual(part["status"], "部分签收")
        # 重复签收：幂等跳过，不产生新分录
        before = svc.query_ledger(item_id="MB-02")["entries"]
        dup = svc.sign_shipment(shp2["shipment_id"], signed_by="周校长", items=["MB-02"])
        after = svc.query_ledger(item_id="MB-02")["entries"]
        self.assertEqual(len(before), len(after))
        self.assertEqual(dup["already_signed"], ["MB-02"])
        self.assertEqual(dup["signed"], [])
        # 整单重复签收
        svc.sign_shipment(shp1["shipment_id"], signed_by="周校长")
        again = svc.sign_shipment(shp1["shipment_id"], signed_by="周校长")
        self.assertTrue(again["duplicate"])
        # 签完剩余
        svc.sign_shipment(shp2["shipment_id"], signed_by="周校长")
        svc.sign_shipment(shp3["shipment_id"], signed_by="周校长")
        self.assertEqual(svc.get_session("S1")["status"], "使用中")
        self.assertEqual(svc.trace_item("OU-04")["location"], "到校")
        # 签收人落在分录上（责任环节）
        signs = [e for e in svc.query_ledger(action="sign")["entries"]]
        self.assertEqual(len(signs), 6)
        self.assertTrue(all(e["actor"] == "周校长" and e["role"] == "学校签收人" for e in signs))

    def test_damaged_sign_goes_quarantine(self):
        svc = self.svc
        shp = svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        svc.sign_shipment(
            shp["shipment_id"], signed_by="周校长",
            items=[{"item_id": "OU-01", "condition": "损坏"}],
        )
        trace = svc.trace_item("OU-01")
        self.assertEqual(trace["status"], "隔离")
        self.assertEqual(trace["location"], "隔离区")
        self.assertEqual(trace["entries"][-1]["action"], "sign_damage")


class DamageReplaceTest(unittest.TestCase):
    def test_damage_isolation_and_replacement_before_shipment(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))],
             spare_items=[{"item_id": "OU-09", "category": "木偶", "label": "备用木偶"}])
        make_session(svc, "S1")
        svc.confirm_session("S1", actor="王库管")
        # 活动老师报损：占用中的木偶被隔离并解除绑定
        result = svc.report_damage("OU-01", reason="木偶手臂断裂", actor="李老师", role="活动老师")
        self.assertEqual(result["affected_session"], "S1")
        self.assertEqual(svc.trace_item("OU-01")["status"], "隔离")
        # 重复报损幂等
        again = svc.report_damage("OU-01", reason="木偶手臂断裂", actor="李老师", role="活动老师")
        self.assertTrue(again["duplicate"])
        # 场次视图暴露缺件
        session = svc.get_session("S1")
        self.assertFalse(session["kits"][0]["health"]["healthy"])
        self.assertEqual(session["kits"][0]["health"]["shortages"][0]["category"], "木偶")
        # 缺件套装不能出库
        with self.assertRaises(DomainError) as ctx:
            svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        self.assertEqual(ctx.exception.code, "kit_not_ready")
        # 临时替换：备用木偶接管场次绑定，套装恢复齐套
        svc.replace_kit_item("KIT-01", old_item_id="OU-01", new_item_id="OU-09",
                             old_condition="损坏", reason="断臂更换", actor="王库管")
        kit = svc.get_kit("KIT-01")
        self.assertTrue(kit["health"]["healthy"])
        self.assertEqual(svc.trace_item("OU-09")["status"], "占用")
        self.assertEqual(svc.trace_item("OU-09")["current_session"]["session_id"], "S1")
        # 替换后正常出库
        shp = svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        self.assertEqual(len(shp["items"]), 3)

    def test_in_tour_damage_extra_shipment_and_membership_swap(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))],
             spare_items=[{"item_id": "OU-09", "category": "木偶", "label": "备用木偶"}])
        make_session(svc, "S1")
        svc.confirm_session("S1", actor="王库管")
        shp = svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        svc.sign_shipment(shp["shipment_id"], signed_by="周校长")
        self.assertEqual(svc.get_session("S1")["status"], "使用中")
        # 到校后发现木偶损坏：保持场次绑定，等待归还清算
        svc.report_damage("OU-01", reason="演出中操控杆断裂", actor="李老师", role="活动老师")
        self.assertEqual(svc.trace_item("OU-01")["current_session"]["session_id"], "S1")
        # 临时增补出库：备件先锁定再出库，两步分录
        extra = svc.create_shipment("S1", actor="王库管", extra_item_ids=["OU-09"])
        self.assertEqual(extra["items"][0]["item_id"], "OU-09")
        entries = svc.query_ledger(item_id="OU-09")["entries"]
        self.assertEqual([e["action"] for e in entries[-2:]], ["reserve", "ship"])
        svc.sign_shipment(extra["shipment_id"], signed_by="周校长")
        # 成员替换：坏件离套（仍绑定待归还），新件入套，套装恢复齐套
        svc.replace_kit_item("KIT-01", old_item_id="OU-01", new_item_id="OU-09",
                             old_condition="损坏", reason="现场替换", actor="王库管")
        self.assertTrue(svc.get_kit("KIT-01")["health"]["healthy"])
        # 归还：坏件随队带回入隔离，其余完好
        svc.register_return("S1", received_by="王库管", items=[
            {"item_id": "OU-01", "condition": "损坏"},
            {"item_id": "MB-01", "condition": "完好"},
            {"item_id": "OU-02", "condition": "完好"},
            {"item_id": "OU-09", "condition": "完好"},
        ])
        closed = svc.close_session("S1", actor="王库管")
        self.assertEqual(closed["status"], "已完结")
        # 隔离件修复后回库
        svc.resolve_item("OU-01", outcome="修复", actor="王库管")
        self.assertEqual(svc.trace_item("OU-01")["status"], "备货")
        # 全程分录链完整
        self.assertTrue(svc.reconcile()["chain_ok"])


class ReturnTest(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        seed(self.svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(self.svc, "S1")
        self.svc.confirm_session("S1", actor="王库管")
        shp = self.svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        self.svc.sign_shipment(shp["shipment_id"], signed_by="周校长")

    def test_partial_return_missing_and_close(self):
        svc = self.svc
        # 部分归还：先还幕布
        svc.register_return("S1", received_by="王库管", items=[{"item_id": "MB-01", "condition": "完好"}])
        session = svc.get_session("S1")
        self.assertEqual(session["status"], "归还中")
        self.assertEqual(svc.trace_item("MB-01")["location"], "仓库")
        # 还有物资在校，不能结案
        with self.assertRaises(DomainError) as ctx:
            svc.close_session("S1", actor="王库管")
        self.assertEqual(ctx.exception.code, "close_blocked")
        self.assertEqual(len(ctx.exception.details["outstanding"]), 2)
        # 木偶 OU-01 丢失：登记归还差异
        svc.register_return("S1", received_by="王库管", missing=["OU-01"])
        session = svc.get_session("S1")
        self.assertEqual(session["discrepancies"]["missing"][0]["item_id"], "OU-01")
        self.assertEqual(svc.trace_item("OU-01")["location"], "去向不明")
        # 最后一件损坏归还，入隔离
        svc.register_return("S1", received_by="王库管", items=[{"item_id": "OU-02", "condition": "损坏"}])
        self.assertEqual(svc.trace_item("OU-02")["status"], "隔离")
        # 差异全部清算，结案
        svc.close_session("S1", actor="王库管")
        session = svc.get_session("S1")
        self.assertEqual(session["status"], "已完结")
        self.assertEqual(session["reservations"][0]["status"], "已完结")
        self.assertEqual(session["summary"]["missing"], 1)
        self.assertEqual(session["summary"]["damaged"], 1)
        self.assertEqual(session["summary"]["returned_good"], 1)

    def test_close_releases_unshipped_reservation(self):
        svc = make_service()
        seed(svc, [
            ("KIT-01", ("MB-01", "OU-01", "OU-02")),
            ("KIT-02", ("MB-02", "OU-03", "OU-04")),
        ])
        make_session(svc, "S1", quantity=2)
        svc.confirm_session("S1", actor="王库管")
        shp = svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        svc.sign_shipment(shp["shipment_id"], signed_by="周校长")
        svc.register_return("S1", received_by="王库管", items=[
            {"item_id": iid, "condition": "完好"} for iid in ("MB-01", "OU-01", "OU-02")
        ])
        # KIT-02 未出库：结案时自动释放占用
        svc.close_session("S1", actor="王库管")
        session = svc.get_session("S1")
        statuses = {r["kit_id"]: r["status"] for r in session["reservations"]}
        self.assertEqual(statuses["KIT-02"], "已释放")
        self.assertEqual(svc.get_stock()["by_status"].get("占用", 0), 0)

    def test_resolve_quarantined_item(self):
        svc = self.svc
        svc.register_return("S1", received_by="王库管", items=[
            {"item_id": "OU-01", "condition": "损坏"},
            {"item_id": "MB-01", "condition": "完好"},
            {"item_id": "OU-02", "condition": "完好"},
        ])
        svc.resolve_item("OU-01", outcome="修复", actor="王库管")
        self.assertEqual(svc.trace_item("OU-01")["status"], "备货")
        svc.report_damage("OU-01", reason="再次损坏", actor="王库管", role="器材管理员")
        svc.resolve_item("OU-01", outcome="报废", actor="王库管")
        self.assertEqual(svc.trace_item("OU-01")["status"], "报废")
        with self.assertRaises(DomainError) as ctx:
            svc.resolve_item("OU-01", outcome="修复", actor="王库管")
        self.assertEqual(ctx.exception.code, "item_state")


class ReconcileTest(unittest.TestCase):
    def test_reconcile_repairs_drift_and_detects_tampering(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(svc, "S1")
        svc.confirm_session("S1", actor="王库管")
        # 模拟中途失败/异常写入：绕过服务直接改账面状态
        conn = svc.db.connection()
        conn.execute("UPDATE items SET status='丢失' WHERE item_id='MB-01'")
        report = svc.reconcile()
        self.assertTrue(report["chain_ok"])
        self.assertEqual(report["drift"], [{"item_id": "MB-01", "stored": "丢失", "derived": "占用"}])
        # 修复后账面与分录一致，修复本身也落分录
        svc.reconcile(repair=True)
        self.assertEqual(svc.trace_item("MB-01")["status"], "占用")
        follow = svc.reconcile()
        self.assertEqual(follow["drift"], [])
        self.assertEqual(svc.query_ledger(action="reconcile_repair")["entries"][0]["to_status"], "占用")
        # 篡改分录内容：哈希链断裂可检出
        conn.execute("UPDATE entries SET to_status='丢失' WHERE item_id='OU-01' AND action='reserve'")
        tampered = svc.reconcile()
        self.assertFalse(tampered["chain_ok"])
        self.assertTrue(tampered["chain_problems"])


class IdempotencyTest(unittest.TestCase):
    def test_same_key_replays_without_reapplying(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(svc, "S1")
        body = {"actor": "王库管"}
        data1, _, replayed1 = svc.run_idempotent(
            "confirm-S1", "POST /api/sessions/S1/confirm", body,
            lambda: svc.confirm_session("S1", actor="王库管"), 200)
        data2, _, replayed2 = svc.run_idempotent(
            "confirm-S1", "POST /api/sessions/S1/confirm", body,
            lambda: svc.confirm_session("S1", actor="王库管"), 200)
        self.assertFalse(replayed1)
        self.assertTrue(replayed2)
        self.assertEqual(data1, data2)
        # 只锁定一次
        self.assertEqual(len(svc.query_ledger(action="reserve")["entries"]), 3)
        # 同一键提交不同请求体：拒绝
        with self.assertRaises(DomainError) as ctx:
            svc.run_idempotent(
                "confirm-S1", "POST /api/sessions/S1/confirm", {"actor": "别人"},
                lambda: svc.confirm_session("S1", actor="别人"), 200)
        self.assertEqual(ctx.exception.code, "idempotency_mismatch")


class TraceTest(unittest.TestCase):
    def test_item_trace_restores_whereabouts_and_responsibility(self):
        svc = make_service()
        seed(svc, [("KIT-01", ("MB-01", "OU-01", "OU-02"))])
        make_session(svc, "S1")
        svc.confirm_session("S1", actor="王库管")
        shp = svc.create_shipment("S1", actor="王库管", kit_ids=["KIT-01"])
        svc.sign_shipment(shp["shipment_id"], signed_by="周校长")
        svc.register_return("S1", received_by="王库管", items=[{"item_id": "MB-01", "condition": "完好"}])
        svc.register_return("S1", received_by="王库管", missing=["OU-01"])
        svc.register_return("S1", received_by="王库管", items=[{"item_id": "OU-02", "condition": "完好"}])
        svc.close_session("S1", actor="王库管")
        trace = svc.trace_item("OU-01")
        actions = [e["action"] for e in trace["entries"]]
        self.assertEqual(actions, ["register", "assemble", "reserve", "ship", "sign", "mark_missing"])
        # 每个环节都有责任人
        actors = {(e["action"], e["actor"], e["role"]) for e in trace["entries"]}
        self.assertIn(("reserve", "王库管", "器材管理员"), actors)
        self.assertIn(("sign", "周校长", "学校签收人"), actors)
        self.assertIn(("mark_missing", "王库管", "器材管理员"), actors)
        # 分录可关联到出库单
        ship_entry = next(e for e in trace["entries"] if e["action"] == "ship")
        self.assertEqual(ship_entry["ref_id"], shp["shipment_id"])


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.service = create_server("127.0.0.1", 0, ":memory:")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method=method,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self):
        status, payload = self.call("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_full_flow_over_http_with_idempotency_key(self):
        status, payload = self.call("POST", "/api/batches", {
            "batch_id": "BAT-HTTP", "name": "HTTP 批次", "actor": "王库管",
            "items": [
                {"item_id": "H-MB-01", "category": "幕布", "label": "幕布"},
                {"item_id": "H-OU-01", "category": "木偶", "label": "木偶甲"},
                {"item_id": "H-OU-02", "category": "木偶", "label": "木偶乙"},
            ],
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/api/templates", {
            "template_id": "TPL-HTTP", "name": "巡演套装",
            "parts": [{"category": "幕布", "quantity": 1}, {"category": "木偶", "quantity": 2}],
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/api/kits", {
            "kit_id": "KIT-HTTP", "template_id": "TPL-HTTP", "name": "HTTP 套装",
            "item_ids": ["H-MB-01", "H-OU-01", "H-OU-02"], "actor": "王库管",
        })
        self.assertEqual(status, 201)
        status, _ = self.call("POST", "/api/sessions", {
            "session_id": "S-HTTP", "school": "阳光小学", "teacher": "李老师",
            "planned_at": "2026-10-10",
            "requirements": [{"template_id": "TPL-HTTP", "quantity": 1}],
        })
        self.assertEqual(status, 201)
        # 幂等键：重复提交确认请求，只锁定一次
        headers = {"Idempotency-Key": "confirm-S-HTTP"}
        status, first = self.call("POST", "/api/sessions/S-HTTP/confirm", {"actor": "王库管"}, headers)
        self.assertEqual(status, 200)
        self.assertFalse(first["idempotent_replay"])
        status, second = self.call("POST", "/api/sessions/S-HTTP/confirm", {"actor": "王库管"}, headers)
        self.assertEqual(status, 200)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["data"], second["data"])
        # 出库 → 签收 → 归还 → 结案
        status, shipment = self.call("POST", "/api/sessions/S-HTTP/shipments",
                                     {"actor": "王库管", "kit_ids": ["KIT-HTTP"]})
        self.assertEqual(status, 201)
        shipment_id = shipment["data"]["shipment_id"]
        status, signed = self.call("POST", f"/api/shipments/{shipment_id}/sign",
                                   {"signed_by": "周校长"})
        self.assertEqual(status, 200)
        self.assertEqual(signed["data"]["status"], "已签收")
        status, _ = self.call("POST", "/api/sessions/S-HTTP/returns", {
            "received_by": "王库管",
            "items": [{"item_id": iid, "condition": "完好"}
                      for iid in ("H-MB-01", "H-OU-01", "H-OU-02")],
        })
        self.assertEqual(status, 201)
        status, closed = self.call("POST", "/api/sessions/S-HTTP/close", {"actor": "王库管"})
        self.assertEqual(status, 200)
        self.assertEqual(closed["data"]["status"], "已完结")
        # 追溯与对账
        status, trace = self.call("GET", "/api/items/H-MB-01/trace")
        self.assertEqual(status, 200)
        self.assertEqual(trace["data"]["status"], "归还")
        status, report = self.call("POST", "/api/reconcile", {})
        self.assertEqual(status, 200)
        self.assertTrue(report["data"]["chain_ok"])
        self.assertEqual(report["data"]["drift"], [])

    def test_error_format(self):
        status, payload = self.call("GET", "/api/sessions/NO-SUCH")
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "not_found")
        status, payload = self.call("POST", "/api/sessions", {"school": "缺字段"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "missing_field")


if __name__ == "__main__":
    unittest.main()
