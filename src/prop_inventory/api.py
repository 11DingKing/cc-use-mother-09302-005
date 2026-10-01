"""HTTP 接口层：JSON 路由、统一错误格式、幂等键支持。

仅依赖标准库。所有写接口支持 ``Idempotency-Key`` 请求头：
同一键重复提交（如网络重试、重复签收）直接回放首个响应，不重复落账。
"""
from __future__ import annotations

import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .db import Database
from .models import DomainError, ROLE_ADMIN, ROLE_SIGNER, bad_request
from .service import InventoryService


def _require(body: dict, *fields: str):
    """取必填字段，缺失时抛 400。"""
    values = []
    for field in fields:
        value = body.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise bad_request("missing_field", f"缺少必填字段：{field}")
        values.append(value)
    return values[0] if len(values) == 1 else values


def _q1(query: dict, name: str, default=None):
    values = query.get(name)
    return values[0] if values else default


class Api:
    """路由分发：把 HTTP 请求映射到 InventoryService。"""

    def __init__(self, service: InventoryService):
        self.service = service
        self._routes = [
            (method, re.compile(pattern), handler, status)
            for method, pattern, handler, status in _ROUTE_TABLE
        ]

    def dispatch(self, method: str, path: str, query: dict, body: dict, headers) -> tuple[int, dict]:
        for route_method, pattern, handler_name, status in self._routes:
            if route_method != method:
                continue
            match = pattern.fullmatch(path)
            if not match:
                continue
            handler = getattr(self, handler_name)
            if method == "POST":
                key = headers.get("Idempotency-Key")
                if key:
                    data, code, replayed = self.service.run_idempotent(
                        key, f"{method} {path}", body,
                        lambda: handler(match, body, query), status,
                    )
                    return code, {"ok": True, "data": data, "idempotent_replay": replayed}
            return status, {"ok": True, "data": handler(match, body, query)}
        raise DomainError("route_not_found", f"路由不存在：{method} {path}", 404)

    # ---- 健康与库存 ----

    def _health(self, match, body, query):
        return {"service": "木偶教具库存履约", "status": "up"}

    def _stock(self, match, body, query):
        return self.service.get_stock()

    # ---- 批次 ----

    def _create_batch(self, match, body, query):
        return self.service.register_batch(
            name=_require(body, "name"),
            source=body.get("source", ""),
            parent_batch_id=body.get("parent_batch_id"),
            note=body.get("note", ""),
            batch_id=body.get("batch_id"),
            items=_require(body, "items"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
        )

    def _get_batch(self, match, body, query):
        return self.service.get_batch(match.group("batch_id"))

    # ---- 套装模板与实例 ----

    def _create_template(self, match, body, query):
        return self.service.create_template(
            name=_require(body, "name"),
            parts=_require(body, "parts"),
            template_id=body.get("template_id"),
        )

    def _get_template(self, match, body, query):
        return self.service.get_template(match.group("template_id"))

    def _create_kit(self, match, body, query):
        return self.service.assemble_kit(
            template_id=_require(body, "template_id"),
            name=_require(body, "name"),
            item_ids=_require(body, "item_ids"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
            kit_id=body.get("kit_id"),
        )

    def _get_kit(self, match, body, query):
        return self.service.get_kit(match.group("kit_id"))

    def _replace_kit_item(self, match, body, query):
        return self.service.replace_kit_item(
            match.group("kit_id"),
            old_item_id=_require(body, "old_item_id"),
            new_item_id=_require(body, "new_item_id"),
            old_condition=body.get("old_condition", "损坏"),
            reason=body.get("reason", ""),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
        )

    # ---- 场次 ----

    def _create_session(self, match, body, query):
        return self.service.create_session(
            school=_require(body, "school"),
            teacher=_require(body, "teacher"),
            planned_at=_require(body, "planned_at"),
            requirements=_require(body, "requirements"),
            session_id=body.get("session_id"),
        )

    def _list_sessions(self, match, body, query):
        return self.service.list_sessions()

    def _get_session(self, match, body, query):
        return self.service.get_session(match.group("session_id"))

    def _confirm_session(self, match, body, query):
        return self.service.confirm_session(
            match.group("session_id"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
        )

    def _cancel_session(self, match, body, query):
        return self.service.cancel_session(
            match.group("session_id"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
        )

    def _create_shipment(self, match, body, query):
        return self.service.create_shipment(
            match.group("session_id"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
            kit_ids=body.get("kit_ids") or (),
            item_ids=body.get("item_ids") or (),
            extra_item_ids=body.get("extra_item_ids") or (),
        )

    def _register_return(self, match, body, query):
        return self.service.register_return(
            match.group("session_id"),
            received_by=_require(body, "received_by"),
            role=body.get("role", ROLE_ADMIN),
            items=body.get("items") or (),
            missing=body.get("missing") or (),
        )

    def _close_session(self, match, body, query):
        return self.service.close_session(
            match.group("session_id"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
        )

    # ---- 出库单 ----

    def _get_shipment(self, match, body, query):
        return self.service.get_shipment(match.group("shipment_id"))

    def _sign_shipment(self, match, body, query):
        return self.service.sign_shipment(
            match.group("shipment_id"),
            signed_by=_require(body, "signed_by"),
            items=body.get("items"),
            role=body.get("role", ROLE_SIGNER),
        )

    # ---- 单件 ----

    def _list_items(self, match, body, query):
        return self.service.list_items(
            status=_q1(query, "status"),
            session_id=_q1(query, "session_id"),
            category=_q1(query, "category"),
        )

    def _report_damage(self, match, body, query):
        return self.service.report_damage(
            match.group("item_id"),
            reason=_require(body, "reason"),
            actor=_require(body, "actor"),
            role=body.get("role", "活动老师"),
        )

    def _resolve_item(self, match, body, query):
        return self.service.resolve_item(
            match.group("item_id"),
            outcome=_require(body, "outcome"),
            actor=_require(body, "actor"),
            role=body.get("role", ROLE_ADMIN),
        )

    def _trace_item(self, match, body, query):
        return self.service.trace_item(match.group("item_id"))

    # ---- 分录与对账 ----

    def _query_ledger(self, match, body, query):
        return self.service.query_ledger(
            item_id=_q1(query, "item_id"),
            session_id=_q1(query, "session_id"),
            action=_q1(query, "action"),
            limit=_q1(query, "limit", 200),
        )

    def _reconcile(self, match, body, query):
        return self.service.reconcile(
            repair=bool(body.get("repair", False)),
            actor=body.get("actor", "system"),
        )


_ROUTE_TABLE = [
    ("GET", r"/api/health", "_health", 200),
    ("GET", r"/api/stock", "_stock", 200),
    ("POST", r"/api/batches", "_create_batch", 201),
    ("GET", r"/api/batches/(?P<batch_id>[^/]+)", "_get_batch", 200),
    ("POST", r"/api/templates", "_create_template", 201),
    ("GET", r"/api/templates/(?P<template_id>[^/]+)", "_get_template", 200),
    ("POST", r"/api/kits", "_create_kit", 201),
    ("GET", r"/api/kits/(?P<kit_id>[^/]+)", "_get_kit", 200),
    ("POST", r"/api/kits/(?P<kit_id>[^/]+)/replace", "_replace_kit_item", 200),
    ("POST", r"/api/sessions", "_create_session", 201),
    ("GET", r"/api/sessions", "_list_sessions", 200),
    ("GET", r"/api/sessions/(?P<session_id>[^/]+)", "_get_session", 200),
    ("POST", r"/api/sessions/(?P<session_id>[^/]+)/confirm", "_confirm_session", 200),
    ("POST", r"/api/sessions/(?P<session_id>[^/]+)/cancel", "_cancel_session", 200),
    ("POST", r"/api/sessions/(?P<session_id>[^/]+)/shipments", "_create_shipment", 201),
    ("POST", r"/api/sessions/(?P<session_id>[^/]+)/returns", "_register_return", 201),
    ("POST", r"/api/sessions/(?P<session_id>[^/]+)/close", "_close_session", 200),
    ("GET", r"/api/shipments/(?P<shipment_id>[^/]+)", "_get_shipment", 200),
    ("POST", r"/api/shipments/(?P<shipment_id>[^/]+)/sign", "_sign_shipment", 200),
    ("GET", r"/api/items", "_list_items", 200),
    ("POST", r"/api/items/(?P<item_id>[^/]+)/damage", "_report_damage", 200),
    ("POST", r"/api/items/(?P<item_id>[^/]+)/resolve", "_resolve_item", 200),
    ("GET", r"/api/items/(?P<item_id>[^/]+)/trace", "_trace_item", 200),
    ("GET", r"/api/ledger", "_query_ledger", 200),
    ("POST", r"/api/reconcile", "_reconcile", 200),
]


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PropInventory/0.2"
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        body = json.loads(raw.decode("utf-8"))
                        if not isinstance(body, dict):
                            raise bad_request("invalid_body", "请求体必须是 JSON 对象")
                path = parsed.path.rstrip("/") or "/"
                status, payload = api.dispatch(method, path, query, body, self.headers)
            except DomainError as exc:
                status = exc.status
                payload = {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
            except json.JSONDecodeError:
                status = 400
                payload = {"ok": False, "error": {"code": "invalid_json", "message": "请求体不是合法 JSON", "details": {}}}
            except Exception:  # noqa: BLE001 - 兜底，避免连接悬挂
                traceback.print_exc()
                status = 500
                payload = {"ok": False, "error": {"code": "internal", "message": "服务端内部错误", "details": {}}}
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def log_message(self, *args):  # 静默访问日志
            pass

    return Handler


def create_server(host: str, port: int, db_path: str = ":memory:"):
    """创建 HTTP 服务，返回 (server, service)。"""
    db = Database(db_path)
    service = InventoryService(db)
    api = Api(service)
    httpd = ThreadingHTTPServer((host, port), make_handler(api))
    httpd.daemon_threads = True
    return httpd, service


def serve(host: str, port: int, db_path: str = ":memory:") -> None:
    httpd, _ = create_server(host, port, db_path)
    actual_host, actual_port = httpd.server_address[:2]
    print(f"木偶教具库存履约服务端已启动：http://{actual_host}:{actual_port}（数据库：{db_path}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
