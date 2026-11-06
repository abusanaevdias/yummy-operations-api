"""Transactional SQLite domain model for a local, synthetic operations demo."""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid


STATES = {
    "reserved": {"ready", "cancelled", "expired"},
    "ready": {"picked_up", "cancelled", "expired"},
    "picked_up": set(), "cancelled": set(), "expired": set(),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
    costPrice INTEGER NOT NULL CHECK(costPrice > 0),
    salePrice INTEGER NOT NULL CHECK(salePrice > 0 AND salePrice <= costPrice),
    pickupFrom TEXT NOT NULL, pickupTo TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived'))
);
CREATE TABLE IF NOT EXISTS branches (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed'))
);
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    branchId TEXT NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK(role IN ('cashier', 'manager')),
    active INTEGER NOT NULL CHECK(active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    branchId TEXT NOT NULL REFERENCES branches(id) ON DELETE RESTRICT,
    templateId TEXT NOT NULL REFERENCES templates(id) ON DELETE RESTRICT,
    customerName TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 100),
    boxName TEXT NOT NULL, branchName TEXT NOT NULL,
    unitPrice INTEGER NOT NULL, amount INTEGER NOT NULL,
    pickupFrom TEXT NOT NULL, pickupTo TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved','ready','picked_up','cancelled','expired')),
    createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_filter ON orders(branchId, status, createdAt);
CREATE TABLE IF NOT EXISTS order_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    orderId TEXT NOT NULL REFERENCES orders(id),
    status TEXT NOT NULL, occurredAt TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_order ON order_events(orderId, sequence);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL
);
"""


class Problem(Exception):
    """A deliberate client-visible error, without database internals."""

    def __init__(self, status, code, message, field=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.field = field

    def as_dict(self):
        detail = {"code": self.code, "message": str(self)}
        if self.field:
            detail["field"] = self.field
        return {"error": detail}


def invalid(field, message):
    raise Problem(422, "validation_error", message, field)


def text(value, field, maximum=100, allow_empty=False):
    if not isinstance(value, str):
        invalid(field, "Expected a string.")
    value = value.strip()
    if (not allow_empty and not value) or len(value) > maximum:
        invalid(field, f"Expected {'0' if allow_empty else '1'} to {maximum} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        invalid(field, "Control characters are not allowed.")
    return value


def choice(value, field, values):
    if not isinstance(value, str) or value not in values:
        invalid(field, "Expected one of: " + ", ".join(sorted(values)) + ".")
    return value


def integer(value, field, low, high):
    if type(value) is not int or not low <= value <= high:
        invalid(field, f"Expected an integer from {low} to {high}.")
    return value


def boolean(value, field):
    if type(value) is not bool:
        invalid(field, "Expected true or false.")
    return value


def fields(data, allowed, required=()):
    if not isinstance(data, dict):
        invalid("body", "Expected a JSON object.")
    if set(data) - set(allowed):
        invalid("body", "Unknown fields: " + ", ".join(sorted(set(data) - set(allowed))) + ".")
    missing = set(required) - set(data)
    if missing:
        invalid("body", "Missing fields: " + ", ".join(sorted(missing)) + ".")
    return data


def validate_template(data):
    required = {"name", "description", "costPrice", "salePrice", "pickupFrom", "pickupTo", "status"}
    fields(data, required, required)
    result = {"name": text(data["name"], "name"),
              "description": text(data["description"], "description", 500, True),
              "costPrice": integer(data["costPrice"], "costPrice", 1, 1_000_000),
              "salePrice": integer(data["salePrice"], "salePrice", 1, 1_000_000),
              "status": choice(data["status"], "status", {"active", "archived"})}
    if result["salePrice"] > result["costPrice"]:
        invalid("salePrice", "Sale price must not exceed cost price.")
    for key in ("pickupFrom", "pickupTo"):
        value = data[key]
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            invalid(key, "Expected 24-hour HH:MM.")
        result[key] = value
    if result["pickupFrom"] >= result["pickupTo"]:
        invalid("pickupTo", "Pickup end must be later on the same day.")
    return result


def validate_branch(data):
    keys = {"name", "address", "status"}
    fields(data, keys, keys)
    return {"name": text(data["name"], "name"), "address": text(data["address"], "address", 200),
            "status": choice(data["status"], "status", {"open", "closed"})}


def validate_employee(data):
    keys = {"name", "branchId", "role", "active"}
    fields(data, keys, keys)
    return {"name": text(data["name"], "name"), "branchId": text(data["branchId"], "branchId"),
            "role": choice(data["role"], "role", {"cashier", "manager"}),
            "active": boolean(data["active"], "active")}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Store:
    """Open one connection per operation, so request threads share no connections."""

    def __init__(self, path):
        self.path = str(Path(path).resolve())
        with sqlite3.connect(self.path, timeout=5) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database schema version: {version}")
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=1;\nCOMMIT;")

    @contextmanager
    def transaction(self, write=False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise Problem(409, "record_in_use", "A referenced record cannot be removed or duplicated.") from error
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def row(connection, table, record_id):
        # Table names originate only from the fixed methods below, never HTTP input.
        found = connection.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if found is None:
            raise Problem(404, "not_found", "Record not found.")
        return dict(found)

    @staticmethod
    def insert(connection, table, data, prefix):
        record = {"id": prefix + "_" + uuid.uuid4().hex, **data}
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(record.values()))
        return record

    @staticmethod
    def update(connection, table, record_id, data):
        assignments = ", ".join(f"{key}=?" for key in data)
        connection.execute(f"UPDATE {table} SET {assignments} WHERE id=?", (*data.values(), record_id))

    def create_template(self, data):
        data = validate_template(data)
        with self.transaction(True) as connection:
            return self.insert(connection, "templates", data, "t")

    def get_template(self, record_id):
        with self.transaction() as connection:
            return self.row(connection, "templates", record_id)

    def list_templates(self, status=None):
        if status is not None:
            choice(status, "status", {"active", "archived"})
        with self.transaction() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM templates WHERE (? IS NULL OR status=?) ORDER BY name, id", (status, status))]

    def update_template(self, record_id, patch):
        with self.transaction(True) as connection:
            current = self.row(connection, "templates", record_id)
            fields(patch, set(current) - {"id"})
            data = validate_template({**{k: v for k, v in current.items() if k != "id"}, **patch})
            self.update(connection, "templates", record_id, data)
            return self.row(connection, "templates", record_id)

    def delete_template(self, record_id):
        self.delete_record("templates", record_id)

    @staticmethod
    def branch_view(connection, branch):
        counts = dict(connection.execute(
            "SELECT status, SUM(quantity) FROM orders WHERE branchId=? GROUP BY status", (branch["id"],)).fetchall())
        collected = counts.get("picked_up", 0)
        resolved = collected + counts.get("expired", 0)
        return {**branch, "boxesSold": collected,
                "redemptionRate": round(100 * collected / resolved, 1) if resolved else None}

    def create_branch(self, data):
        data = validate_branch(data)
        with self.transaction(True) as connection:
            return self.branch_view(connection, self.insert(connection, "branches", data, "b"))

    def get_branch(self, record_id):
        with self.transaction() as connection:
            return self.branch_view(connection, self.row(connection, "branches", record_id))

    def list_branches(self):
        with self.transaction() as connection:
            return [self.branch_view(connection, dict(row)) for row in connection.execute("SELECT * FROM branches ORDER BY name, id")]

    def update_branch(self, record_id, patch):
        with self.transaction(True) as connection:
            current = self.row(connection, "branches", record_id)
            fields(patch, set(current) - {"id"})
            data = validate_branch({**{k: v for k, v in current.items() if k != "id"}, **patch})
            self.update(connection, "branches", record_id, data)
            return self.branch_view(connection, self.row(connection, "branches", record_id))

    def delete_branch(self, record_id):
        self.delete_record("branches", record_id)

    @staticmethod
    def employee_view(connection, employee):
        employee["active"] = bool(employee["active"])
        employee["branchName"] = Store.row(connection, "branches", employee["branchId"])["name"]
        return employee

    def create_employee(self, data):
        data = validate_employee(data)
        with self.transaction(True) as connection:
            self.row(connection, "branches", data["branchId"])
            return self.employee_view(connection, self.insert(connection, "employees", data, "e"))

    def get_employee(self, record_id):
        with self.transaction() as connection:
            return self.employee_view(connection, self.row(connection, "employees", record_id))

    def list_employees(self, branch_id=None, active=None):
        if branch_id is not None:
            text(branch_id, "branchId")
        if active is not None:
            boolean(active, "active")
        with self.transaction() as connection:
            return [self.employee_view(connection, dict(row)) for row in connection.execute(
                "SELECT * FROM employees WHERE (? IS NULL OR branchId=?) AND (? IS NULL OR active=?) ORDER BY name, id",
                (branch_id, branch_id, active, active))]

    def update_employee(self, record_id, patch):
        with self.transaction(True) as connection:
            current = self.row(connection, "employees", record_id)
            current["active"] = bool(current["active"])
            fields(patch, set(current) - {"id"})
            data = validate_employee({**{k: v for k, v in current.items() if k != "id"}, **patch})
            self.row(connection, "branches", data["branchId"])
            self.update(connection, "employees", record_id, data)
            return self.employee_view(connection, self.row(connection, "employees", record_id))

    def delete_employee(self, record_id):
        self.delete_record("employees", record_id)

    def delete_record(self, table, record_id):
        if table not in {"templates", "branches", "employees"}:
            raise ValueError("Unsupported collection")
        with self.transaction(True) as connection:
            self.row(connection, table, record_id)
            connection.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))

    @staticmethod
    def order_view(connection, order):
        order["statusHistory"] = [dict(row) for row in connection.execute(
            "SELECT status, occurredAt FROM order_events WHERE orderId=? ORDER BY sequence", (order["id"],))]
        return order

    def create_order(self, data, idempotency_key):
        keys = {"branchId", "templateId", "customerName", "quantity"}
        fields(data, keys, keys - {"quantity"})
        data = {"branchId": text(data["branchId"], "branchId"),
                "templateId": text(data["templateId"], "templateId"),
                "customerName": text(data["customerName"], "customerName"),
                "quantity": integer(data.get("quantity", 1), "quantity", 1, 100)}
        if not isinstance(idempotency_key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            invalid("Idempotency-Key", "Use 1 to 128 ASCII letters, digits, dots, underscores, colons or hyphens.")
        fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        with self.transaction(True) as connection:
            prior = connection.execute("SELECT * FROM idempotency WHERE key=?", (idempotency_key,)).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise Problem(409, "idempotency_conflict", "This key was used with a different order request.")
                return json.loads(prior["response"]), True
            branch = self.row(connection, "branches", data["branchId"])
            template = self.row(connection, "templates", data["templateId"])
            if branch["status"] != "open" or template["status"] != "active":
                raise Problem(409, "unavailable", "Reservations require an open branch and an active template.")
            timestamp = now()
            order = self.insert(connection, "orders", {
                **data, "boxName": template["name"], "branchName": branch["name"],
                "unitPrice": template["salePrice"], "amount": template["salePrice"] * data["quantity"],
                "pickupFrom": template["pickupFrom"], "pickupTo": template["pickupTo"],
                "status": "reserved", "createdAt": timestamp, "updatedAt": timestamp,
            }, "o")
            connection.execute("INSERT INTO order_events(orderId,status,occurredAt) VALUES(?,?,?)", (order["id"], "reserved", timestamp))
            order = self.order_view(connection, order)
            connection.execute("INSERT INTO idempotency(key,fingerprint,response) VALUES(?,?,?)", (idempotency_key, fingerprint, json.dumps(order)))
            return order, False

    def get_order(self, record_id):
        with self.transaction() as connection:
            return self.order_view(connection, self.row(connection, "orders", record_id))

    def list_orders(self, branch_id=None, status=None, limit=50, offset=0):
        if branch_id is not None:
            text(branch_id, "branchId")
        if status is not None:
            choice(status, "status", STATES)
        integer(limit, "limit", 1, 100)
        integer(offset, "offset", 0, 10_000)
        where = "WHERE (? IS NULL OR branchId=?) AND (? IS NULL OR status=?)"
        parameters = (branch_id, branch_id, status, status)
        with self.transaction() as connection:
            total = connection.execute("SELECT COUNT(*) FROM orders " + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT * FROM orders " + where + " ORDER BY createdAt DESC, id DESC LIMIT ? OFFSET ?", (*parameters, limit, offset))
            return {"items": [self.order_view(connection, dict(row)) for row in rows], "total": total, "limit": limit, "offset": offset}

    def transition_order(self, record_id, status):
        choice(status, "status", STATES)
        with self.transaction(True) as connection:
            order = self.row(connection, "orders", record_id)
            if status not in STATES[order["status"]]:
                raise Problem(409, "invalid_transition", f"Cannot move {order['status']} to {status}.")
            timestamp = now()
            self.update(connection, "orders", record_id, {"status": status, "updatedAt": timestamp})
            connection.execute("INSERT INTO order_events(orderId,status,occurredAt) VALUES(?,?,?)", (record_id, status, timestamp))
            return self.order_view(connection, self.row(connection, "orders", record_id))
