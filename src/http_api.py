"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
HOLDING_RE = re.compile(r"^/api/holdings/(\d+)$")
HOLDING_ADJUST_RE = re.compile(r"^/api/holdings/(\d+)/adjust$")
HOLDING_AUDIT_RE = re.compile(r"^/api/holdings/(\d+)/audit$")
CA_RE = re.compile(r"^/api/corporate-actions/(\d+)$")
CA_CALC_RE = re.compile(r"^/api/corporate-actions/(\d+)/calculate$")
CA_FINALIZE_RE = re.compile(r"^/api/corporate-actions/(\d+)/finalize$")
CA_ENTITLEMENTS_RE = re.compile(r"^/api/corporate-actions/(\d+)/entitlements$")
CA_AUDIT_RE = re.compile(r"^/api/corporate-actions/(\d+)/audit$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "securities-settlement/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _version(self, body: Dict[str, Any]) -> int:
            version = body.get("expected_version")
            if not isinstance(version, int) or isinstance(version, bool):
                raise ValidationError("expected_version必须是整数")
            return version

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                actor = self._actor()
                query = parse_qs(parsed.query)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "securities-settlement", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    page = (static_dir / "index.html").read_bytes()
                    self._send(200, page, "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/records":
                    records = service.list_records(actor, state=query.get("state", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": records})
                    return
                if parsed.path == "/api/holdings":
                    holdings = service.list_holdings(
                        actor,
                        instrument=query.get("instrument", [None])[0],
                        account=query.get("account", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                    )
                    self._send(200, {"items": holdings})
                    return
                if parsed.path == "/api/corporate-actions":
                    cas = service.list_corporate_actions(actor, status=query.get("status", [None])[0], limit=int(query.get("limit", ["100"])[0]))
                    self._send(200, {"items": cas})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(actor))
                    return

                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(actor, int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.timeline(actor, int(match.group(1)))})
                    return
                match = HOLDING_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_holding(actor, int(match.group(1))))
                    return
                match = HOLDING_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.holding_timeline(actor, int(match.group(1)))})
                    return
                match = CA_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_corporate_action(actor, int(match.group(1))))
                    return
                match = CA_ENTITLEMENTS_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.entitlements(actor, int(match.group(1)))})
                    return
                match = CA_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.corporate_action_timeline(actor, int(match.group(1)))})
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                actor = self._actor()
                if parsed.path == "/api/records":
                    record = service.create(actor, body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                if parsed.path == "/api/holdings":
                    holding = service.create_holding(actor, payload=body.get("data", {}))
                    self._send(201, holding)
                    return
                if parsed.path == "/api/corporate-actions":
                    ca = service.create_corporate_action(actor, body.get("reference", ""), body.get("data", {}))
                    self._send(201, ca)
                    return

                match = ACTION_RE.match(parsed.path)
                if match:
                    record = service.act(actor, int(match.group(1)), self._version(body), match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                match = HOLDING_ADJUST_RE.match(parsed.path)
                if match:
                    holding = service.adjust_holding(actor, int(match.group(1)), self._version(body), body.get("data", {}))
                    self._send(200, holding)
                    return
                match = CA_CALC_RE.match(parsed.path)
                if match:
                    result = service.calculate_entitlements(actor, int(match.group(1)), self._version(body))
                    self._send(200, result)
                    return
                match = CA_FINALIZE_RE.match(parsed.path)
                if match:
                    result = service.finalize_corporate_action(actor, int(match.group(1)), self._version(body), body.get("idempotency_key"))
                    self._send(200, result)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
