"""HTTP 接口：仅依赖标准库，把领域服务暴露为 JSON REST 接口。

幂等：写请求可带 Idempotency-Key 头，中途失败后用同样的请求体重试安全。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import DomainError
from .service import FulfillmentService


def _make_handler(service: FulfillmentService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PuppetFulfillment/0.2"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静音
            return

        # -- 工具 ---------------------------------------------------------
        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                from .errors import ValidationError

                raise ValidationError(f"请求体不是合法 JSON：{exc}")
            if not isinstance(body, dict):
                from .errors import ValidationError

                raise ValidationError("请求体必须是 JSON 对象")
            return body

        def _idem(self, body: dict) -> str | None:
            key = self.headers.get("Idempotency-Key")
            body.pop("_idem_key", None)
            return key

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, fn: Callable[[], Any]) -> None:
            try:
                self._send(200, {"ok": True, "data": fn()})
            except DomainError as exc:
                self._send(exc.status, {"ok": False, "error": exc.to_dict()})

        # -- 路由 ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.strip("/").split("/")
            self._dispatch(lambda: self._route_get(path))

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path.strip("/").split("/")
            self._dispatch(lambda: self._route_post(path))

        def _route_get(self, path: list[str]) -> Any:
            if path == ["health"]:
                return {"status": "ok"}
            if len(path) == 3 and path[0] == "items" and path[2] == "history":
                return service.item_history(path[1])
            if len(path) == 2 and path[0] == "items":
                return service.locate_item(path[1])
            if len(path) == 2 and path[0] == "shows":
                return service.show_overview(path[1])
            if path[:1] == ["discrepancies"]:
                status = None
                query = urlparse(self.path).query
                for pair in query.split("&"):
                    if pair.startswith("status="):
                        from urllib.parse import unquote

                        status = unquote(pair.split("=", 1)[1])
                return {"discrepancies": service.list_discrepancies(status=status)}
            if path == ["reconcile"]:
                return service.reconcile()
            from .errors import NotFound

            raise NotFound(f"无此路由：/{'/'.join(path)}")

        def _route_post(self, path: list[str]) -> Any:
            body = self._read_json()
            idem = self._idem(body)
            s = service

            if path == ["staff"]:
                return s.register_staff(body["staff_id"], body["name"], body["role"])
            if path == ["kinds"]:
                return s.define_kind(body["kind_code"], body["name"],
                                     unit=body.get("unit", "件"),
                                     tracking=body.get("tracking", "单件"))
            if path == ["sets"]:
                return s.define_set(body["set_code"], body["name"], body["components"])
            if path == ["batches"]:
                return s.receive_batch(body["batch_no"], body["kind_code"], int(body["total_qty"]),
                                       supplier=body.get("supplier"), note=body.get("note"),
                                       actor_id=body.get("actor_id"), idem_key=idem)
            if path == ["shows"]:
                return s.create_show(body["show_id"], body["school"],
                                     scheduled_at=body.get("scheduled_at"),
                                     created_by=body.get("created_by"), note=body.get("note"))
            if len(path) == 3 and path[0] == "shows" and path[2] == "reservations":
                return s.add_reservation(body["reservation_id"], path[1], body["set_code"],
                                         int(body.get("set_units", 1)))
            if len(path) == 3 and path[0] == "shows" and path[2] == "confirm":
                return s.confirm_show(path[1], actor_id=body.get("actor_id"), idem_key=idem)
            if len(path) == 3 and path[0] == "shows" and path[2] == "cancel":
                return s.cancel_show(path[1], actor_id=body.get("actor_id"), idem_key=idem)
            if len(path) == 3 and path[0] == "shows" and path[2] == "substitute":
                return s.substitute(path[1], body["original_item_code"],
                                    body["replacement_item_code"], actor_id=body.get("actor_id"),
                                    note=body.get("note"), idem_key=idem)
            if path == ["outbounds"]:
                return s.create_outbound(body["outbound_id"], body["show_id"],
                                         created_by=body.get("created_by"), plan=body.get("plan"))
            if len(path) == 3 and path[0] == "outbounds" and path[2] == "ship":
                return s.ship_items(path[1], body["item_codes"], actor_id=body.get("actor_id"),
                                    idem_key=idem)
            if len(path) == 3 and path[0] == "outbounds" and path[2] == "receive":
                return s.receive(path[1], body["receiver_id"], body["lines"],
                                 receipt_no=body["receipt_no"], note=body.get("note"),
                                 idem_key=idem)
            if path == ["returns"]:
                return s.return_items(body["return_no"], body["outbound_id"], body["handler_id"],
                                      body["lines"], note=body.get("note"), idem_key=idem)
            if len(path) == 3 and path[0] == "items" and path[2] == "damage":
                return s.report_damage(path[1], actor_id=body.get("actor_id"),
                                       note=body.get("note"), idem_key=idem)
            if len(path) == 3 and path[0] == "substitutions" and path[2] == "swap-back":
                return s.swap_back(int(path[1]), condition=body.get("condition", "完好"),
                                   actor_id=body.get("actor_id"), note=body.get("note"),
                                   idem_key=idem)
            if len(path) == 3 and path[0] == "discrepancies" and path[2] == "resolve":
                return s.resolve_discrepancy(int(path[1]), body["resolution"],
                                             action=body.get("action", "核销"),
                                             actor_id=body.get("actor_id"), idem_key=idem)
            from .errors import NotFound

            raise NotFound(f"无此路由：/{'/'.join(path)}")

    return Handler


def create_server(service: FulfillmentService, host: str = "127.0.0.1",
                  port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _make_handler(service))
    server.service = service  # type: ignore[attr-defined]
    return server


def serve(service: FulfillmentService, host: str = "127.0.0.1", port: int = 8080) -> None:
    httpd = create_server(service, host, port)
    print(f"木偶教具库存履约服务已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
