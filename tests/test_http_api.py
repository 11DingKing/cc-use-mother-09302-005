"""HTTP 接口端到端测试：真实起服，用 urllib 走完整流程。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from puppet_fulfillment import FulfillmentService
from puppet_fulfillment.api import create_server


def seed() -> FulfillmentService:
    s = FulfillmentService(":memory:")
    s.register_staff("MGR", "王管理", "器材管理员")
    s.register_staff("SCH", "李签收", "学校签收人")
    s.define_kind("HEAD", "木偶头", tracking="单件")
    s.define_kind("CURTAIN", "幕布", tracking="批次")
    s.define_set("STD", "套装", {"HEAD": 2, "CURTAIN": 1})
    s.receive_batch("BH1", "HEAD", 3)
    s.receive_batch("BC1", "CURTAIN", 2)
    return s


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body: dict | None = None,
             idem: str | None = None):
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return resp.status, payload
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = seed()
        cls.httpd = create_server(cls.service, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def test_health(self) -> None:
        status, body = self.api.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["status"] == "ok")

    def test_full_journey_over_http(self) -> None:
        api = self.api
        status, body = api.call("POST", "/shows", {"show_id": "W1", "school": "Web小学"})
        self.assertEqual(status, 200, body)
        status, body = api.call("POST", "/shows/W1/reservations",
                                {"reservation_id": "RW1", "set_code": "STD", "set_units": 1})
        self.assertEqual(status, 200, body)
        status, body = api.call("POST", "/shows/W1/confirm", {"actor_id": "MGR"}, idem="lock-w1")
        self.assertEqual(status, 200, body)
        locked = body["data"]["locked_item_count"]
        self.assertEqual(locked, 3)

        # 幂等：同键重放不重复锁定
        status, body2 = api.call("POST", "/shows/W1/confirm", {"actor_id": "MGR"}, idem="lock-w1")
        self.assertEqual(body2["data"]["locked_item_count"], 3)

        codes = [c for u in body["data"]["locked_units"] for c in u["items"]]
        status, ob = api.call("POST", "/outbounds", {"outbound_id": "OW1", "show_id": "W1"})
        self.assertEqual(status, 200, ob)
        status, ship = api.call("POST", "/outbounds/OW1/ship", {"item_codes": codes})
        self.assertEqual(status, 200, ship)
        lines = [{"item_code": c, "condition": "完好"} for c in codes]
        status, rec = api.call("POST", "/outbounds/OW1/receive",
                               {"receiver_id": "SCH", "receipt_no": "C1", "lines": lines})
        self.assertEqual(status, 200, rec)
        self.assertEqual(rec["data"]["accepted_count"], 3)

        # 重复签收 → 409 duplicate_receipt
        status, dup = api.call("POST", "/outbounds/OW1/receive",
                               {"receiver_id": "SCH", "receipt_no": "C2", "lines": lines})
        self.assertEqual(status, 409)
        self.assertEqual(dup["error"]["code"], "duplicate_receipt")

        # 定位与对账
        status, loc = api.call("GET", f"/items/{codes[0]}")
        self.assertEqual(status, 200)
        self.assertEqual(loc["data"]["current_status"], "使用")
        status, recon = api.call("GET", "/reconcile")
        self.assertEqual(status, 200)
        self.assertTrue(recon["data"]["ok"], recon["data"])

    def test_insufficient_stock_returns_409(self) -> None:
        api = self.api
        api.call("POST", "/shows", {"show_id": "W2", "school": "Web二小"})
        api.call("POST", "/shows/W2/reservations",
                 {"reservation_id": "RW2", "set_code": "STD", "set_units": 1})
        status, body = api.call("POST", "/shows/W2/confirm", {"actor_id": "MGR"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "insufficient_stock")
        self.assertTrue(body["error"]["details"]["shortages"])

    def test_unknown_route_404(self) -> None:
        status, body = self.api.call("GET", "/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
