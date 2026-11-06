"""HTTP contract checks using an actual local server and temporary SQLite file."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from core import Store

try:
    from api import make_server
except ModuleNotFoundError:
    make_server = None


class APITests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(make_server, "Local JSON HTTP API is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "http.sqlite3")
        self.server = make_server(self.store, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.port = self.server.server_address[1]
        self.branch = self.store.create_branch({"name": "Demo A", "address": "Synthetic Street A", "status": "open"})
        self.template = self.store.create_template({
            "name": "Demo box", "description": "Synthetic",
            "costPrice": 900, "salePrice": 300,
            "pickupFrom": "18:00", "pickupTo": "20:00", "status": "active",
        })
        self.order = {"branchId": self.branch["id"], "templateId": self.template["id"], "customerName": "Demo Customer"}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None, raw=False):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        supplied = dict(headers or {})
        if body is not None and not raw:
            body = json.dumps(body).encode()
            supplied.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), json.loads(payload) if payload else None

    def test_health_has_json_and_local_demo_mode(self):
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertEqual("local-synthetic", body["mode"])
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertIn("application/json", headers["Content-Type"])

    def test_order_creation_replay_and_transition_over_http(self):
        status, headers, order = self.request("POST", "/api/orders", self.order, {"Idempotency-Key": "http-order"})
        self.assertEqual(201, status)
        self.assertEqual("false", headers["Idempotency-Replayed"])
        self.assertEqual("/api/orders/" + order["id"], headers["Location"])
        status, headers, replay = self.request("POST", "/api/orders", self.order, {"Idempotency-Key": "http-order"})
        self.assertEqual(200, status)
        self.assertEqual("true", headers["Idempotency-Replayed"])
        self.assertEqual(order, replay)
        status, _, ready = self.request("POST", "/api/orders/" + order["id"] + "/transitions", {"status": "ready"})
        self.assertEqual(200, status)
        self.assertEqual("ready", ready["status"])
        status, _, result = self.request("GET", "/api/orders?branchId=" + self.branch["id"] + "&status=ready&limit=1")
        self.assertEqual(200, status)
        self.assertEqual([ready], result["items"])
        self.assertEqual(1, result["total"])

    def test_crud_routes_persist_and_return_204_on_delete(self):
        status, _, branch = self.request("POST", "/api/branches", {"name": "Demo B", "address": "Synthetic Street B", "status": "closed"})
        self.assertEqual(201, status)
        path = "/api/branches/" + branch["id"]
        self.assertEqual(200, self.request("PATCH", path, {"status": "open"})[0])
        self.assertEqual("open", self.request("GET", path)[2]["status"])
        status, _, body = self.request("DELETE", path)
        self.assertEqual(204, status)
        self.assertIsNone(body)
        self.assertEqual(404, self.request("GET", path)[0])

    def test_employee_boolean_query_is_parsed_strictly(self):
        status, _, employee = self.request("POST", "/api/employees", {
            "name": "Demo Operator", "branchId": self.branch["id"], "role": "cashier", "active": True,
        })
        self.assertEqual(201, status)
        self.request("PATCH", "/api/employees/" + employee["id"], {"active": False})
        result = self.request("GET", "/api/employees?active=false")[2]
        self.assertEqual([False], [item["active"] for item in result["items"]])
        self.assertEqual(422, self.request("GET", "/api/employees?active=0")[0])

    def test_missing_key_and_invalid_transition_return_clear_4xx(self):
        status, _, problem = self.request("POST", "/api/orders", self.order)
        self.assertEqual(422, status)
        self.assertEqual("Idempotency-Key", problem["error"]["field"])
        order, _ = self.store.create_order(self.order, "existing")
        status, _, problem = self.request("POST", "/api/orders/" + order["id"] + "/transitions", {"status": "picked_up"})
        self.assertEqual(409, status)
        self.assertEqual("invalid_transition", problem["error"]["code"])

    def test_body_requires_json_object_and_valid_json(self):
        for raw in [b"{", b'{"name":NaN}', b'{"name":"a","name":"b"}', b"\xff"]:
            with self.subTest(raw=raw):
                status, _, body = self.request("POST", "/api/branches", raw, {"Content-Type": "application/json"}, raw=True)
                self.assertEqual(400, status)
                self.assertEqual("invalid_json", body["error"]["code"])
        self.assertEqual(422, self.request("POST", "/api/branches", [1, 2])[0])
        self.assertEqual(415, self.request("POST", "/api/branches", b"{}", {"Content-Type": "text/plain"}, raw=True)[0])

    def test_declared_oversized_body_rejected_without_reading_it(self):
        status, _, problem = self.request("POST", "/api/branches", b"{}", {
            "Content-Type": "application/json", "Content-Length": "16385",
        }, raw=True)
        self.assertEqual(413, status)
        self.assertEqual("body_too_large", problem["error"]["code"])

    def test_missing_length_and_transfer_encoding_rejected(self):
        # http.client.request() adds Content-Length: 0 itself; send headers directly.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        connection.putrequest("PATCH", "/api/branches/" + self.branch["id"])
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(411, response.status)
        response.read()
        status, _, _ = self.request("POST", "/api/branches", b"{}", {
            "Content-Type": "application/json", "Transfer-Encoding": "chunked",
        }, raw=True)
        self.assertEqual(400, status)

    def test_invalid_filters_and_repeated_query_values_are_rejected(self):
        for query in ["limit=0", "limit=abc", "offset=-1", "status=paid", "limit=1&limit=2", "unrecognized=true"]:
            with self.subTest(query=query):
                self.assertEqual(422, self.request("GET", "/api/orders?" + query)[0])

    def test_cross_origin_mutation_and_unexpected_host_are_rejected(self):
        self.assertEqual(403, self.request("POST", "/api/branches", {}, {"Origin": "https://example.invalid"})[0])
        self.assertEqual(421, self.request("GET", "/api/health", headers={"Host": "example.invalid"})[0])
        self.assertEqual(200, self.request("GET", "/api/health", headers={"Origin": f"http://127.0.0.1:{self.port}"})[0])

    def test_missing_route_method_and_long_url_are_bounded_json_errors(self):
        self.assertEqual(404, self.request("GET", "/missing")[0])
        self.assertEqual(405, self.request("PUT", "/api/branches", {})[0])
        self.assertEqual(414, self.request("GET", "/" + "a" * 2100)[0])

    def test_server_refuses_nonlocal_binding(self):
        with self.assertRaises(ValueError):
            make_server(self.store, host="0.0.0.0", port=0)

    def test_unknown_http_method_returns_a_json_client_error(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("BREW", "/api/health")
        response = connection.getresponse()
        self.assertEqual(405, response.status)
        self.assertIn("application/json", response.getheader("Content-Type"))
        self.assertEqual("method_not_allowed", json.loads(response.read())["error"]["code"])

    def test_ambiguous_body_lengths_are_rejected_before_mutation(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        connection.putrequest("POST", "/api/branches")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "2")
        connection.putheader("Content-Length", "3")
        connection.endheaders(b"{}")
        response = connection.getresponse()
        self.assertEqual(400, response.status)
        self.assertEqual("invalid_length", json.loads(response.read())["error"]["code"])
        self.assertEqual(1, len(self.store.list_branches()))

    def test_lone_surrogate_keys_and_nested_values_return_json_without_disconnect(self):
        bodies = [b'{"\\ud800":1}',
                  b'{"name":"\\ud800","address":"Synthetic","status":"open"}',
                  b'{"unexpected":{"nested":["\\udfff"]}}']
        for body in bodies:
            with self.subTest(body=body):
                status, _, problem = self.request("POST", "/api/branches", body,
                                                  {"Content-Type": "application/json"}, raw=True)
                self.assertEqual(400, status)
                self.assertEqual("invalid_json", problem["error"]["code"])
        self.assertEqual(1, len(self.store.list_branches()))

    def test_valid_unicode_surrogate_pair_is_accepted(self):
        status, _, branch = self.request("POST", "/api/branches",
            b'{"name":"Demo \\ud83e\\udd50","address":"Synthetic","status":"open"}',
            {"Content-Type": "application/json"}, raw=True)
        self.assertEqual(201, status)
        self.assertEqual("Demo 🥐", branch["name"])


if __name__ == "__main__":
    unittest.main()
