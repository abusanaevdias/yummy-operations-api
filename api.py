"""Small loopback-only JSON REST boundary. No external services or payments."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
import sqlite3
from urllib.parse import parse_qs, urlsplit

from core import Problem, ensure_unicode_scalars, fields, integer, invalid


MAX_BODY = 16_384


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_value):
    raise ValueError("Nonfinite JSON number")


def page_value(query, name, default, low, high):
    value = query.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,5}", value):
        invalid(name, "Expected an unsigned decimal integer.")
    return integer(int(value), name, low, high)


def make_server(store, host="127.0.0.1", port=8765):
    """Return an unstarted server. Only IPv4 loopback/localhost is supported."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("This demonstrator binds only to 127.0.0.1 or localhost")

    class Handler(BaseHTTPRequestHandler):
        server_version = "YummyLocalDemo/1"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, _format, *_args):
            # Avoid writing record IDs or query strings to a request log.
            pass

        def send_error(self, code, message=None, explain=None):
            # BaseHTTPRequestHandler otherwise emits HTML and a 501 for an
            # unknown verb. Keep protocol rejections on the JSON contract too.
            if code == 501:
                code = 405
            error_code = "method_not_allowed" if code == 405 else "http_error"
            detail = self.responses.get(code, ("HTTP request rejected",))[0]
            self.send_json(code, {"error": {"code": error_code, "message": detail}})

        def send_json(self, status, data, headers=None):
            # ASCII escaping also keeps error responses encodable if an
            # unexpected internal string contains an unpaired surrogate.
            payload = b"" if status == 204 else json.dumps(data, ensure_ascii=True, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            self.close_connection = True

        def check_origin(self):
            hosts = self.headers.get_all("Host", [])
            accepted = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if len(hosts) != 1 or hosts[0] not in accepted:
                raise Problem(421, "invalid_host", "Use the local host and port printed by the server.")
            origins = self.headers.get_all("Origin", [])
            if origins and (len(origins) != 1 or origins[0] != "http://" + hosts[0]):
                raise Problem(403, "cross_origin", "Cross-origin browser requests are disabled.")

        def read_json(self):
            if self.headers.get("Transfer-Encoding") is not None:
                raise Problem(400, "unsupported_encoding", "Transfer-Encoding is not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if not lengths:
                raise Problem(411, "length_required", "Content-Length is required.")
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise Problem(400, "invalid_length", "Use one nonnegative Content-Length.")
            length = int(lengths[0])
            if length > MAX_BODY:
                raise Problem(413, "body_too_large", f"JSON bodies are limited to {MAX_BODY} bytes.")
            media = self.headers.get_all("Content-Type", [])
            if len(media) != 1 or media[0].partition(";")[0].strip().lower() != "application/json":
                raise Problem(415, "json_required", "Use Content-Type: application/json.")
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete body")
                data = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
                ensure_unicode_scalars(data)
            except (ValueError, UnicodeError, RecursionError) as error:
                raise Problem(400, "invalid_json", "Expected scalar Unicode JSON with unique keys and finite numbers.") from error
            fields(data, data.keys() if isinstance(data, dict) else ())
            return data

        def query(self, raw, allowed):
            try:
                parsed = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
            except ValueError as error:
                raise Problem(422, "invalid_query", "Invalid query string.") from error
            if set(parsed) - allowed or any(len(values) != 1 for values in parsed.values()):
                raise Problem(422, "invalid_query", "Use documented query names once each.")
            return {key: values[0] for key, values in parsed.items()}

        def dispatch(self):
            self.check_origin()
            if len(self.path) > 2048:
                raise Problem(414, "url_too_long", "URLs are limited to 2048 characters.")
            url = urlsplit(self.path)
            if url.scheme or url.netloc or url.fragment:
                raise Problem(400, "invalid_url", "Use a relative API path.")
            path = url.path
            if path == "/api/health" and self.command == "GET":
                self.query(url.query, set())
                return 200, {"status": "ok", "mode": "local-synthetic", "schemaVersion": 1}, {}
            match = re.fullmatch(r"/api/(templates|branches|employees|orders)(?:/([A-Za-z0-9_-]{1,100}))?(?:/(transitions))?", path)
            if not match:
                raise Problem(404, "not_found", "API route not found.")
            collection, record_id, transition = match.groups()
            if transition:
                self.query(url.query, set())
                if collection != "orders" or not record_id:
                    raise Problem(404, "not_found", "API route not found.")
                if self.command != "POST":
                    raise Problem(405, "method_not_allowed", "Transitions accept POST.")
                data = self.read_json()
                fields(data, {"status"}, {"status"})
                return 200, store.transition_order(record_id, data["status"]), {}
            if self.command == "GET":
                if record_id:
                    self.query(url.query, set())
                    singular = {"templates": "template", "branches": "branch", "employees": "employee", "orders": "order"}[collection]
                    return 200, getattr(store, "get_" + singular)(record_id), {}
                filters = {"templates": {"status"}, "branches": set(), "employees": {"branchId", "active"}, "orders": {"branchId", "status"}}[collection]
                query = self.query(url.query, filters | {"limit", "offset"})
                limit = page_value(query, "limit", 50, 1, 100)
                offset = page_value(query, "offset", 0, 0, 10_000)
                if collection == "orders":
                    return 200, store.list_orders(query.get("branchId"), query.get("status"), limit, offset), {}
                if collection == "templates":
                    items = store.list_templates(query.get("status"))
                elif collection == "branches":
                    items = store.list_branches()
                else:
                    active = query.get("active")
                    if active is not None and active not in {"true", "false"}:
                        invalid("active", "Use true or false.")
                    items = store.list_employees(query.get("branchId"), None if active is None else active == "true")
                return 200, {"items": items[offset:offset + limit], "total": len(items), "limit": limit, "offset": offset}, {}
            self.query(url.query, set())
            if collection == "orders":
                if self.command != "POST" or record_id:
                    raise Problem(405, "method_not_allowed", "Orders accept GET or collection POST; use the transitions route to change state.")
                order, replayed = store.create_order(self.read_json(), self.headers.get("Idempotency-Key"))
                return 200 if replayed else 201, order, {"Location": "/api/orders/" + order["id"], "Idempotency-Replayed": str(replayed).lower()}
            singular = {"templates": "template", "branches": "branch", "employees": "employee"}[collection]
            if self.command == "POST" and not record_id:
                record = getattr(store, "create_" + singular)(self.read_json())
                return 201, record, {"Location": "/api/" + collection + "/" + record["id"]}
            if self.command == "PATCH" and record_id:
                return 200, getattr(store, "update_" + singular)(record_id, self.read_json()), {}
            if self.command == "DELETE" and record_id:
                getattr(store, "delete_" + singular)(record_id)
                return 204, None, {}
            raise Problem(405, "method_not_allowed", "Use GET, collection POST, or record PATCH/DELETE.")

        def handle_api(self):
            try:
                status, body, headers = self.dispatch()
            except Problem as error:
                status, body, headers = error.status, error.as_dict(), {}
            except (TimeoutError, socket.timeout):
                status, body, headers = 408, {"error": {"code": "request_timeout", "message": "Request body timed out."}}, {}
            except sqlite3.OperationalError:
                status, body, headers = 503, {"error": {"code": "storage_unavailable", "message": "Storage is busy or unavailable; retry later."}}, {}
            except Exception:
                status, body, headers = 500, {"error": {"code": "internal_error", "message": "Unexpected server error."}}, {}
            try:
                self.send_json(status, body, headers)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                self.close_connection = True

        do_GET = handle_api
        do_POST = handle_api
        do_PATCH = handle_api
        do_DELETE = handle_api
        do_PUT = handle_api
        do_HEAD = handle_api
        do_OPTIONS = handle_api

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
