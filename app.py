"""CLI and repeatable synthetic demonstration; Python 3.11+ standard library."""

import argparse
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

from api import make_server
from core import Problem, Store, now, validate_branch, validate_employee, validate_template


def emit(data, stream=sys.stdout):
    print(json.dumps(data, ensure_ascii=False, indent=2), file=stream, flush=True)


def seed_demo(store):
    """Install fixtures atomically once. Repeat runs never overwrite user edits."""
    identifiers = {"branchId": "b_demo_north", "templateId": "t_demo_bread"}
    with store.transaction(True) as connection:
        if connection.execute("SELECT 1 FROM demo_seed WHERE id=1").fetchone():
            return {"seeded": False, **identifiers}
        if any(connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
               for table in ("branches", "templates", "employees", "orders", "idempotency")):
            raise Problem(409, "seed_requires_empty", "Seed requires a new empty database; use a different --db path.")
        for record_id, data in [
            ("b_demo_north", {"name": "Demo Bakery North", "address": "Synthetic Avenue 10 (fictional)", "status": "open"}),
            ("b_demo_south", {"name": "Demo Bakery South", "address": "Synthetic Avenue 20 (fictional)", "status": "closed"}),
        ]:
            store.insert(connection, "branches", {"id": record_id, **validate_branch(data)}, "b")
        base = {"description": "Synthetic fixture; no real stock", "costPrice": 1200,
                "salePrice": 400, "pickupFrom": "18:00", "pickupTo": "20:00"}
        for record_id, name, status in [("t_demo_bread", "Demo Bread Box", "active"), ("t_demo_sweets", "Demo Sweet Box", "archived")]:
            store.insert(connection, "templates", {"id": record_id, **validate_template({**base, "name": name, "status": status})}, "t")
        for record_id, name, role, active in [
            ("e_demo_operator", "Demo Operator A", "cashier", True),
            ("e_demo_manager", "Demo Manager B", "manager", False),
        ]:
            data = {"name": name, "branchId": "b_demo_north", "role": role, "active": active}
            store.insert(connection, "employees", {"id": record_id, **validate_employee(data)}, "e")
        connection.execute("INSERT INTO demo_seed(id,createdAt) VALUES(1,?)", (now(),))
        return {"seeded": True, **identifiers}


def run_demo():
    """Use a temporary file and real loopback HTTP; remove data after shutdown."""
    with tempfile.TemporaryDirectory(prefix="yummy-demo-") as directory:
        store = Store(Path(directory) / "demo.sqlite3")
        seed_demo(store)
        server = make_server(store, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, data=None, key=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Idempotency-Key"] = key
            try:
                connection.request(method, path, body=None if data is None else json.dumps(data).encode(), headers=headers)
                response = connection.getresponse()
                payload = json.loads(response.read())
                if response.status >= 400:
                    raise RuntimeError(f"Local demo request failed: {response.status}")
                return response.status, payload
            finally:
                connection.close()

        try:
            data = {"branchId": "b_demo_north", "templateId": "t_demo_bread", "customerName": "Demo Guest A", "quantity": 1}
            first_status, order = request("POST", "/api/orders", data, "demo-reservation-1")
            replay_status, replay = request("POST", "/api/orders", data, "demo-reservation-1")
            path = "/api/orders/" + order["id"]
            request("POST", path + "/transitions", {"status": "ready"})
            _, final = request("POST", path + "/transitions", {"status": "picked_up"})
            _, listing = request("GET", "/api/orders?branchId=b_demo_north&status=picked_up")
            return {"mode": "local-synthetic", "temporaryDatabase": True,
                    "creationStatuses": [first_status, replay_status], "sameReplay": order == replay,
                    "states": [item["status"] for item in final["statusHistory"]],
                    "orderCount": listing["total"], "orderId": order["id"]}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def main(argv=None):
    if sys.version_info < (3, 11):
        raise SystemExit("Use Python 3.11 or newer.")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="yummy.sqlite3", help="SQLite file (default: yummy.sqlite3)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create or inspect schema without fixtures")
    commands.add_parser("seed", help="Atomically seed a fresh database with synthetic fixtures")
    commands.add_parser("demo", help="Run an isolated temporary HTTP lifecycle demo")
    serve = commands.add_parser("server", aliases=["serve"], help="Run the loopback JSON API")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765, help="Local port, or 0 to choose a free port")
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            emit(run_demo())
            return 0
        store = Store(args.db)
        if args.command == "init":
            emit({"database": store.path, "schemaVersion": 1, "mode": "local-synthetic"})
        elif args.command == "seed":
            emit(seed_demo(store))
        else:
            if not 0 <= args.port <= 65535:
                parser.error("--port must be between 0 and 65535")
            server = make_server(store, args.host, args.port)
            try:
                # One-line startup message lets scripts read the selected port.
                print(json.dumps({"url": f"http://{args.host}:{server.server_port}", "mode": "local-synthetic"}), flush=True)
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except Problem as error:
        emit(error.as_dict(), sys.stderr)
        return 1
    except (OSError, sqlite3.Error, RuntimeError) as error:
        emit({"error": {"code": "local_error", "message": str(error)}}, sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
