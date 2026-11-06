"""Behavior checks against real SQLite files; no external services or mocks."""

import concurrent.futures
import tempfile
import unittest
from pathlib import Path

try:
    from core import Problem, Store
except ModuleNotFoundError:
    Problem = Store = None


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Store, "SQLite operations store is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "operations.sqlite3"
        self.store = Store(self.path)
        self.branch = self.store.create_branch(
            {"name": "Demo North", "address": "Synthetic Lane 1", "status": "open"}
        )
        self.template_data = {
            "name": "Demo bread", "description": "Synthetic box",
            "costPrice": 1200, "salePrice": 400,
            "pickupFrom": "18:00", "pickupTo": "20:00", "status": "active",
        }
        self.template = self.store.create_template(self.template_data)
        self.order_data = {
            "branchId": self.branch["id"], "templateId": self.template["id"],
            "customerName": "Demo Customer A", "quantity": 2,
        }

    def assert_problem(self, status, callback, *args):
        with self.assertRaises(Problem) as caught:
            callback(*args)
        self.assertEqual(status, caught.exception.status)

    def test_data_survives_new_store_instance(self):
        order, replay = self.store.create_order(self.order_data, "persist-1")
        reopened = Store(self.path)
        self.assertEqual(self.template, reopened.get_template(self.template["id"]))
        self.assertEqual(order, reopened.get_order(order["id"]))
        self.assertFalse(replay)

    def test_template_patch_and_delete(self):
        updated = self.store.update_template(self.template["id"], {"name": "Renamed"})
        self.assertEqual("Renamed", updated["name"])
        self.assertEqual(400, updated["salePrice"])
        self.store.delete_template(updated["id"])
        self.assert_problem(404, self.store.get_template, updated["id"])

    def test_template_rejects_invalid_prices_and_windows_atomically(self):
        cases = [
            {"salePrice": 0}, {"salePrice": True}, {"salePrice": 1201},
            {"costPrice": -1}, {"salePrice": 4.5},
            {"pickupFrom": "9:00"}, {"pickupTo": "25:00"}, {"pickupFrom": "1９:00"},
            {"pickupTo": "18:00"}, {"pickupTo": "01:00"},
            {"name": " "}, {"status": "published"}, {"extra": "field"},
        ]
        for patch in cases:
            with self.subTest(patch=patch):
                self.assert_problem(422, self.store.update_template, self.template["id"], patch)
                self.assertEqual(self.template, self.store.get_template(self.template["id"]))

    def test_templates_can_be_filtered_by_status(self):
        archived = self.store.create_template({**self.template_data, "status": "archived"})
        self.assertEqual([archived], self.store.list_templates("archived"))
        self.assert_problem(422, self.store.list_templates, "invalid")

    def test_branch_crud_and_employee_state(self):
        employee = self.store.create_employee({
            "name": "Demo Operator A", "branchId": self.branch["id"],
            "role": "manager", "active": True,
        })
        updated = self.store.update_employee(employee["id"], {"active": False})
        self.assertFalse(updated["active"])
        self.assertEqual("Demo North", updated["branchName"])
        self.assertEqual([updated], self.store.list_employees(self.branch["id"], False))
        self.assert_problem(409, self.store.delete_branch, self.branch["id"])
        self.assert_problem(422, self.store.update_employee, employee["id"], {"active": 1})
        self.assert_problem(422, self.store.update_employee, employee["id"], {"role": "admin"})
        self.store.delete_employee(employee["id"])
        self.store.update_branch(self.branch["id"], {"status": "closed"})
        self.assertEqual("closed", self.store.get_branch(self.branch["id"])["status"])
        self.store.delete_branch(self.branch["id"])
        self.assertEqual([], self.store.list_branches())

    def test_order_snapshots_price_and_names(self):
        order, _ = self.store.create_order(self.order_data, "snapshot")
        self.store.update_template(self.template["id"], {"salePrice": 500, "name": "Changed"})
        self.store.update_branch(self.branch["id"], {"name": "New branch name"})
        saved = self.store.get_order(order["id"])
        self.assertEqual(800, saved["amount"])
        self.assertEqual("Demo bread", saved["boxName"])
        self.assertEqual("Demo North", saved["branchName"])
        self.assertEqual("reserved", saved["status"])
        self.assertEqual("18:00", saved["pickupFrom"])
        self.assertEqual(["reserved"], [h["status"] for h in saved["statusHistory"]])

    def test_explicit_transitions_and_terminal_state(self):
        order, _ = self.store.create_order(self.order_data, "transitions")
        self.assert_problem(409, self.store.transition_order, order["id"], "picked_up")
        self.store.transition_order(order["id"], "ready")
        final = self.store.transition_order(order["id"], "picked_up")
        self.assertEqual(["reserved", "ready", "picked_up"], [h["status"] for h in final["statusHistory"]])
        self.assert_problem(409, self.store.transition_order, order["id"], "cancelled")
        self.assert_problem(422, self.store.transition_order, order["id"], "paid")
        self.assertEqual(2, self.store.get_branch(self.branch["id"])["boxesSold"])
        self.assertEqual(100.0, self.store.get_branch(self.branch["id"])["redemptionRate"])

    def test_reserved_order_can_be_cancelled_or_expired(self):
        for key, status in [("cancel", "cancelled"), ("expire", "expired")]:
            order, _ = self.store.create_order(self.order_data, key)
            self.assertEqual(status, self.store.transition_order(order["id"], status)["status"])

    def test_duplicate_key_replays_original_response_even_after_transition(self):
        first, replay = self.store.create_order(self.order_data, "duplicate")
        self.store.transition_order(first["id"], "ready")
        again, replay = Store(self.path).create_order(dict(reversed(list(self.order_data.items()))), "duplicate")
        self.assertTrue(replay)
        self.assertEqual(first, again)
        self.assertEqual(1, self.store.list_orders()["total"])
        self.assert_problem(409, self.store.create_order, {**self.order_data, "quantity": 3}, "duplicate")

    def test_concurrent_duplicate_requests_create_only_one_order(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: self.store.create_order(self.order_data, "race"), range(6)))
        self.assertEqual(1, len({order["id"] for order, _ in results}))
        self.assertEqual(1, sum(not replay for _, replay in results))
        self.assertEqual(1, self.store.list_orders()["total"])

    def test_closed_branch_or_archived_template_rejects_reservation(self):
        self.store.update_branch(self.branch["id"], {"status": "closed"})
        self.assert_problem(409, self.store.create_order, self.order_data, "closed")
        self.store.update_branch(self.branch["id"], {"status": "open"})
        self.store.update_template(self.template["id"], {"status": "archived"})
        self.assert_problem(409, self.store.create_order, self.order_data, "archived")
        self.assertEqual(0, self.store.list_orders()["total"])

    def test_order_validation_and_referenced_records_are_protected(self):
        for patch in [{"quantity": 0}, {"quantity": True}, {"quantity": 101}, {"customerName": " "}, {"paid": True}]:
            self.assert_problem(422, self.store.create_order, {**self.order_data, **patch}, "bad")
        self.assert_problem(422, self.store.create_order, self.order_data, "")
        self.assert_problem(404, self.store.create_order, {**self.order_data, "branchId": "missing"}, "missing")
        order, _ = self.store.create_order(self.order_data, "protected")
        self.assert_problem(409, self.store.delete_template, self.template["id"])
        self.assert_problem(409, self.store.delete_branch, self.branch["id"])
        self.assertEqual(order, self.store.get_order(order["id"]))

    def test_orders_filter_and_paginate_without_leaking_other_branches(self):
        north, _ = self.store.create_order(self.order_data, "north")
        south = self.store.create_branch({"name": "Demo South", "address": "Synthetic Lane 2", "status": "open"})
        other, _ = self.store.create_order({**self.order_data, "branchId": south["id"]}, "south")
        self.store.transition_order(other["id"], "expired")
        result = self.store.list_orders(branch_id=self.branch["id"], status="reserved", limit=1)
        self.assertEqual([north], result["items"])
        self.assertEqual(1, result["total"])
        page = self.store.list_orders(limit=1, offset=1)
        self.assertEqual(1, len(page["items"]))
        self.assertEqual(2, page["total"])
        self.assert_problem(422, self.store.list_orders, None, "paid")
        self.assert_problem(422, self.store.list_orders, None, None, 1000)
        self.assertEqual([], self.store.list_orders(branch_id="missing")["items"])


if __name__ == "__main__":
    unittest.main()
