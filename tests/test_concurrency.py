"""Two processes, one database.

The CLI and the server are separate processes writing the same SQLite file.
WAL mode plus BEGIN IMMEDIATE plus busy_timeout is what makes that safe; this
asserts it actually is.
"""

import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import REPO, DatabaseTestCase  # noqa: E402

from db import model  # noqa: E402
from db.httpd import Server  # noqa: E402


class TestInProcessConcurrency(DatabaseTestCase):

    def test_many_threads_writing_at_once(self):
        """The write lock serialises this process's own threads, so they
        queue rather than burning the busy_timeout against each other."""
        errors = []

        def worker(n):
            try:
                model.create(self.db, title=f"Thread {n}", tags=["concurrent"])
            except Exception as exc:                      # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM item WHERE tags_cache='concurrent'").fetchone()[0], 32)
        self.assert_all_integrity_clean()

    def test_a_reader_is_not_blocked_by_a_writer(self):
        """In WAL mode a reader sees a consistent snapshot while a write is
        in flight. Without it, the CLI could not query a live server."""
        model.create(self.db, title="before")
        seen = []

        def read_during_write():
            time.sleep(0.05)
            seen.append(self.conn.execute("SELECT count(*) FROM item").fetchone()[0])

        reader = threading.Thread(target=read_during_write)
        with self.db.write():
            reader.start()
            time.sleep(0.15)
            self.db.conn().execute(
                "INSERT INTO item(uid, kind, title, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                ("f" * 32, "note", "during", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"))
        reader.join()
        self.assertEqual(seen, [1], "the reader should have seen the pre-write snapshot")


class TestCrossProcessConcurrency(DatabaseTestCase):
    """A separate OS process writing while a server holds the same database."""

    def _db(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "db", *args, "--db", str(self.layout.db)],
            cwd=str(REPO), env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin",
                                "NO_COLOR": "1"},
            capture_output=True, text=True, timeout=60)

    def test_cli_writes_are_visible_to_a_running_server(self):
        server = Server(self.db, port=8877,
                        web_root=self.layout.root / "no-web").start(background=True)
        try:
            for n in range(5):
                result = self._db("add", "note", f"External {n}", "--tag", "ext")
                self.assertEqual(result.returncode, 0, result.stderr)

            with urllib.request.urlopen(
                    "http://127.0.0.1:8877/api/v1/search?q=tag:ext") as response:
                payload = json.load(response)
            self.assertEqual(payload["total"], 5)
        finally:
            server.stop()

    def test_server_writes_are_visible_to_the_cli(self):
        server = Server(self.db, port=8878,
                        web_root=self.layout.root / "no-web").start(background=True)
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:8878/api/v1/items", method="POST",
                data=json.dumps({"kind": "note", "title": "Made over HTTP"}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 201)

            result = self._db("find", "Made over HTTP", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["hits"][0]["title"], "Made over HTTP")
        finally:
            server.stop()

    def test_the_database_stays_sound_after_both_have_written(self):
        server = Server(self.db, port=8879,
                        web_root=self.layout.root / "no-web").start(background=True)
        try:
            for n in range(3):
                self._db("add", "note", f"cli-{n}")
                request = urllib.request.Request(
                    "http://127.0.0.1:8879/api/v1/items", method="POST",
                    data=json.dumps({"kind": "note", "title": f"http-{n}"}).encode(),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(request).read()
        finally:
            server.stop()
        result = self._db("doctor", "--json")
        self.assertTrue(json.loads(result.stdout)["ok"], result.stdout)


if __name__ == "__main__":
    unittest.main()
