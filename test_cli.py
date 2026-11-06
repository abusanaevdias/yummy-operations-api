"""Exercise CLI commands as separate processes, using only temporary files."""

import http.client
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

from core import Store


ROOT = Path(__file__).resolve().parent


class CLITests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "app.py").is_file(), "Runnable CLI is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cli.sqlite3"

    def command(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "app.py"), "--db", str(self.path), *args],
                              text=True, capture_output=True, timeout=10, cwd=self.temp.name)

    def test_init_and_seed_are_repeatable_without_overwriting_edits(self):
        initialized = self.command("init")
        self.assertEqual(0, initialized.returncode, initialized.stderr)
        self.assertEqual(0, Store(self.path).list_orders()["total"])
        first = self.command("seed")
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertTrue(json.loads(first.stdout)["seeded"])
        store = Store(self.path)
        self.assertEqual(2, len(store.list_branches()))
        self.assertEqual(2, len(store.list_templates()))
        self.assertEqual(2, len(store.list_employees()))
        store.update_employee("e_demo_operator", {"active": False})
        repeated = self.command("seed")
        self.assertEqual(0, repeated.returncode, repeated.stderr)
        self.assertFalse(json.loads(repeated.stdout)["seeded"])
        self.assertFalse(store.get_employee("e_demo_operator")["active"])
        self.assertEqual(2, len(store.list_branches()))

    def test_seed_refuses_a_nonempty_unmarked_database(self):
        store = Store(self.path)
        store.create_branch({"name": "Custom Demo", "address": "Synthetic Street", "status": "open"})
        result = self.command("seed")
        self.assertEqual(1, result.returncode)
        self.assertEqual("seed_requires_empty", json.loads(result.stderr)["error"]["code"])
        self.assertEqual(1, len(store.list_branches()))
        self.assertEqual([], store.list_templates())

    def test_demo_runs_complete_synthetic_http_lifecycle_without_persistent_db(self):
        result = self.command("demo")
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([201, 200], report["creationStatuses"])
        self.assertTrue(report["sameReplay"])
        self.assertEqual(["reserved", "ready", "picked_up"], report["states"])
        self.assertEqual(1, report["orderCount"])
        self.assertFalse(self.path.exists())

    def test_server_command_serves_seeded_data(self):
        self.assertEqual(0, self.command("seed").returncode)
        process = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--db", str(self.path), "server", "--port", "0"],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_process, process)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), "Server did not announce its local URL")
        notice = json.loads(process.stdout.readline())
        url = urlsplit(notice["url"])
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/templates?status=active")
        response = connection.getresponse()
        self.assertEqual(200, response.status)
        self.assertEqual("t_demo_bread", json.loads(response.read())["items"][0]["id"])

    @staticmethod
    def stop_process(process):
        process.terminate()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
