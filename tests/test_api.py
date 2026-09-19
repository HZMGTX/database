"""The HTTP API: concurrency control, error shape, and the security layer."""

import http.client
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import DatabaseTestCase  # noqa: E402

from db import model  # noqa: E402
from db.httpd import Server  # noqa: E402

PORT = 8811


class ApiTestCase(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        type(self).PORT = getattr(type(self), "PORT", PORT)
        self.port = ApiTestCase._next_port()
        self.server = Server(self.db, port=self.port,
                             web_root=self.layout.root / "no-web").start(background=True)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    _port_counter = [8820]

    @classmethod
    def _next_port(cls):
        cls._port_counter[0] += 1
        return cls._port_counter[0]

    def call(self, method, path, body=None, headers=None):
        request = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read()
                return response.status, dict(response.headers), json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, dict(exc.headers), json.loads(raw) if raw else None

    def call_no_redirect(self, path):
        """urllib follows 3xx by default, which would silently turn a test of
        redirect behaviour into a test of the destination."""
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(self.base + path) as response:
                return response.status, dict(response.headers)
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, dict(exc.headers)

    def raw_get(self, path, headers):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        response.read()
        conn.close()
        return response.status


class TestBasics(ApiTestCase):

    def test_health(self):
        status, _, body = self.call("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_search(self):
        model.create(self.db, title="Q3 planning", body="review the budget")
        status, _, body = self.call("GET", "/api/v1/search?q=budget")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["hits"]), 1)

    def test_a_bad_query_is_a_problem_document(self):
        status, headers, body = self.call("GET", "/api/v1/search?q=is:nonsense")
        self.assertEqual(status, 400)
        self.assertIn("problem+json", headers["Content-Type"])
        self.assertEqual(body["status"], 400)
        self.assertTrue(body["detail"])

    def test_unknown_route_is_404(self):
        self.assertEqual(self.call("GET", "/api/v1/nope")[0], 404)

    def test_wrong_method_says_what_is_allowed(self):
        status, _, body = self.call("DELETE", "/api/v1/health")
        self.assertEqual(status, 405)
        self.assertEqual(body["allow"], ["GET"])

    def test_schema_drives_client_forms(self):
        status, _, body = self.call("GET", "/api/v1/schema")
        self.assertEqual(status, 200)
        kinds = {k["name"] for k in body["kinds"]}
        self.assertIn("task", kinds)
        task = next(k for k in body["kinds"] if k["name"] == "task")
        self.assertTrue(any(f["key"] == "status" for f in task["fields"]))


class TestConcurrencyControl(ApiTestCase):

    def _make(self):
        status, headers, doc = self.call(
            "POST", "/api/v1/items", {"kind": "note", "title": "Original"})
        self.assertEqual(status, 201)
        return doc["uid"], headers["ETag"]

    def test_create_returns_location_and_etag(self):
        status, headers, doc = self.call(
            "POST", "/api/v1/items", {"kind": "note", "title": "x"})
        self.assertEqual(status, 201)
        self.assertIn(doc["uid"], headers["Location"])
        self.assertEqual(headers["ETag"], f'"{doc["uid"]}.1"')

    def test_conditional_get_returns_304(self):
        uid, etag = self._make()
        status = self.raw_get(f"/api/v1/items/{uid}",
                              {"Host": f"127.0.0.1:{self.port}", "If-None-Match": etag})
        self.assertEqual(status, 304)

    def test_patch_without_if_match_is_428(self):
        """428 rather than just doing it: a blind write can silently discard
        somebody else's edit."""
        uid, etag = self._make()
        status, _, body = self.call("PATCH", f"/api/v1/items/{uid}", {"title": "x"})
        self.assertEqual(status, 428)
        self.assertEqual(body["current_etag"], etag)

    def test_patch_with_a_current_etag_succeeds(self):
        uid, etag = self._make()
        status, headers, body = self.call(
            "PATCH", f"/api/v1/items/{uid}", {"title": "Renamed"}, {"If-Match": etag})
        self.assertEqual(status, 200)
        self.assertEqual(body["title"], "Renamed")
        self.assertEqual(headers["ETag"], f'"{uid}.2"')

    def test_patch_with_a_stale_etag_is_409_and_names_the_fields(self):
        """A 409 that says only "it changed" makes the client re-fetch and
        guess. Naming the fields lets it merge."""
        uid, etag = self._make()
        self.call("PATCH", f"/api/v1/items/{uid}", {"title": "First"}, {"If-Match": etag})
        status, _, body = self.call(
            "PATCH", f"/api/v1/items/{uid}", {"title": "Second"}, {"If-Match": etag})
        self.assertEqual(status, 409)
        self.assertEqual(body["expected_rev"], 1)
        self.assertEqual(body["current_rev"], 2)
        self.assertIn("title", body["changed"])
        self.assertIn("current", body)

    def test_a_tag_change_moves_the_etag(self):
        """Otherwise a client holding a stale copy can overwrite a concurrent
        edit while its ETag still matches."""
        uid, etag = self._make()
        self.call("PUT", f"/api/v1/items/{uid}/tags", {"tags": ["work"]})
        status, _, _ = self.call(
            "PATCH", f"/api/v1/items/{uid}", {"title": "x"}, {"If-Match": etag})
        self.assertEqual(status, 409)


class TestIdempotency(ApiTestCase):

    def test_a_replayed_key_returns_the_original_and_creates_nothing(self):
        key = "abc-123"
        first = self.call("POST", "/api/v1/items",
                          {"kind": "note", "title": "Once"}, {"Idempotency-Key": key})
        second = self.call("POST", "/api/v1/items",
                           {"kind": "note", "title": "Once"}, {"Idempotency-Key": key})
        self.assertEqual(first[2]["uid"], second[2]["uid"])
        self.assertEqual(second[1].get("Idempotency-Replayed"), "true")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM item WHERE title='Once'").fetchone()[0], 1)

    def test_the_same_key_with_a_different_body_is_refused(self):
        key = "abc-123"
        self.call("POST", "/api/v1/items", {"kind": "note", "title": "A"},
                  {"Idempotency-Key": key})
        status, _, body = self.call("POST", "/api/v1/items",
                                    {"kind": "note", "title": "B"},
                                    {"Idempotency-Key": key})
        self.assertEqual(status, 409)


class TestSecurityLayer(ApiTestCase):
    """A server on 127.0.0.1 is not private.

    Any page the user visits can point a name it controls at 127.0.0.1 and
    have the browser send requests here.
    """

    def test_a_foreign_host_header_is_refused(self):
        self.assertEqual(self.raw_get("/api/v1/health", {"Host": "evil.example"}), 403)

    def test_loopback_host_is_allowed(self):
        self.assertEqual(
            self.raw_get("/api/v1/health", {"Host": f"127.0.0.1:{self.port}"}), 200)

    def test_cross_site_fetch_is_refused(self):
        self.assertEqual(self.raw_get("/api/v1/health", {
            "Host": f"127.0.0.1:{self.port}", "Sec-Fetch-Site": "cross-site"}), 403)

    def test_a_foreign_origin_is_refused(self):
        self.assertEqual(self.raw_get("/api/v1/health", {
            "Host": f"127.0.0.1:{self.port}", "Origin": "https://evil.example"}), 403)

    def test_responses_carry_hardening_headers(self):
        _, headers, _ = self.call("GET", "/api/v1/health")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_an_internal_error_does_not_leak_a_traceback(self):
        status, _, body = self.call("GET", "/api/v1/items/zzzz")
        self.assertIn(status, (404, 400))
        self.assertNotIn("Traceback", json.dumps(body))


class TestLifecycleOverHttp(ApiTestCase):

    def test_delete_restore_and_purge(self):
        _, _, doc = self.call("POST", "/api/v1/items", {"kind": "note", "title": "x"})
        uid = doc["uid"]
        self.assertEqual(self.call("DELETE", f"/api/v1/items/{uid}")[0], 204)
        self.assertEqual(self.call("POST", f"/api/v1/items/{uid}/restore")[0], 200)
        self.assertEqual(self.call("POST", f"/api/v1/items/{uid}/purge", {})[0], 428)
        self.assertEqual(
            self.call("POST", f"/api/v1/items/{uid}/purge", {"confirm": True})[0], 200)

    def test_a_merged_uid_redirects(self):
        doc = model.create(self.db, title="Gone")
        item = model.resolve(self.db, doc["uid"])
        survivor = model.create(self.db, title="Survivor")
        model.purge(self.db, item)
        self.conn.execute("UPDATE tombstone SET redirect_to_uid=? WHERE uid=?",
                          (survivor["uid"], doc["uid"]))
        status, headers = self.call_no_redirect(f"/api/v1/items/{doc['uid']}")
        self.assertEqual(status, 301)
        self.assertIn(survivor["uid"], headers["Location"])

    def test_revisions_and_revert_over_http(self):
        _, headers, doc = self.call("POST", "/api/v1/items",
                                    {"kind": "note", "title": "v1", "body": "one"})
        uid = doc["uid"]
        self.call("PATCH", f"/api/v1/items/{uid}", {"body": "two"},
                  {"If-Match": headers["ETag"]})
        status, _, body = self.call("GET", f"/api/v1/items/{uid}/revisions")
        self.assertEqual(len(body["revisions"]), 2)
        status, _, reverted = self.call("POST", f"/api/v1/items/{uid}/revert/1")
        self.assertEqual(reverted["body"], "one")


if __name__ == "__main__":
    unittest.main()
