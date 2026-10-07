"""Black-box acceptance tests for the URL shortener (SPEC v0.1.0).

These tests never import project code: every check is performed over HTTP
against a real ``./serve`` process with its own temporary ``DATA_DIR``.
Test names carry the SPEC identifiers (``AC`` = acceptance criterion,
``R`` = edge rule) for traceability.

Run with::

    python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(ROOT, "serve")


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class Server:
    """A ``./serve`` subprocess bound to a free loopback port."""

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or tempfile.mkdtemp(prefix="shortener-test-")
        self.port = free_port()
        env = dict(os.environ)
        env.pop("BASE_URL", None)
        env.pop("HOST", None)
        env["DATA_DIR"] = self.data_dir
        self.proc = subprocess.Popen(
            [SERVE, "--port", str(self.port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()

    def _wait_ready(self):
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("serve exited early")
            try:
                if self.request("GET", "/health").status == 200:
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("serve did not become ready within 30s")

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return Response(resp.status, dict(resp.getheaders()), resp.read())
        finally:
            conn.close()

    def post(self, path, payload):
        data = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        return self.request(
            "POST", path, data, {"Content-Type": "application/json"}
        )

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


class ServerTestCase(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.stop()

    def create(self, url="https://example.com/a", alias=None):
        payload = {"url": url}
        if alias is not None:
            payload["alias"] = alias
        return self.server.post("/api/links", payload)


class TestCreation(ServerTestCase):
    def test_ac_2_create_generates_alias(self):
        resp = self.create()
        self.assertEqual(resp.status, 201)
        data = resp.json()
        self.assertRegex(data["alias"], r"^[a-z0-9]{7}$")
        self.assertEqual(data["url"], "https://example.com/a")
        self.assertEqual(
            data["short_url"], "http://127.0.0.1:%d/%s" % (self.server.port, data["alias"])
        )
        self.assertRegex(data["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(data["total_clicks"], 0)

    def test_ac_3_explicit_alias_and_conflict(self):
        first = self.create(alias="ac3link")
        self.assertEqual(first.status, 201)
        self.assertEqual(first.json()["alias"], "ac3link")
        second = self.create(alias="ac3link")
        self.assertEqual(second.status, 409)
        self.assertEqual(second.json(), {"error": "alias_taken"})

    def test_ac_24_empty_alias_generates(self):
        resp = self.create(alias="")
        self.assertEqual(resp.status, 201)
        self.assertRegex(resp.json()["alias"], r"^[a-z0-9]{7}$")


class TestValidation(ServerTestCase):
    def test_ac_4_invalid_url(self):
        for payload in ({"url": "nao-e-url"}, {"url": "ftp://example.com/a"}, {}):
            resp = self.server.post("/api/links", payload)
            self.assertEqual(resp.status, 400, payload)
            self.assertEqual(resp.json(), {"error": "invalid_url"})

    def test_ac_5_invalid_alias(self):
        for alias in ("AB", "api", "com espaço", "a"):
            resp = self.create(alias=alias)
            self.assertEqual(resp.status, 400, alias)
            self.assertEqual(resp.json(), {"error": "invalid_alias"})

    def test_r_1_url_type(self):
        for bad in ("x" * 3000, "http://", "https:///x", "/relative"):
            resp = self.server.post("/api/links", {"url": bad})
            self.assertEqual(resp.status, 400, bad)
            self.assertEqual(resp.json()["error"], "invalid_url")

    def test_ac_20_invalid_json(self):
        for body in ("{", "[]"):
            resp = self.server.post("/api/links", body)
            self.assertEqual(resp.status, 400, body)
            self.assertEqual(resp.json(), {"error": "invalid_json"})

    def test_ac_25_error_shape(self):
        resp = self.server.request("GET", "/no-such-alias")
        self.assertEqual(resp.status, 404)
        self.assertIn("application/json", resp.headers.get("Content-Type", ""))
        self.assertEqual(resp.json(), {"error": "not_found"})


class TestRedirect(ServerTestCase):
    def test_ac_6_redirect_location(self):
        created = self.create(url="https://example.com/target", alias="redir6").json()
        resp = self.server.request("GET", "/redir6")
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], created["url"])
        self.assertEqual(resp.body, b"")

    def test_ac_7_missing_alias(self):
        resp = self.server.request("GET", "/does-not-exist")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.json(), {"error": "not_found"})

    def test_ac_9_head_does_not_count(self):
        self.create(url="https://example.com/head", alias="headlink")
        resp = self.server.request("HEAD", "/headlink")
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], "https://example.com/head")
        stats = self.server.request("GET", "/api/links/headlink/stats").json()
        self.assertEqual(stats["total_clicks"], 0)

    def test_ac_23_query_ignored(self):
        self.create(url="https://example.com/q", alias="querylink")
        resp = self.server.request("GET", "/querylink?utm_source=x")
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], "https://example.com/q")
        stats = self.server.request("GET", "/api/links/querylink/stats").json()
        self.assertEqual(stats["total_clicks"], 1)

    def test_r_26_redirect_methods(self):
        self.create(alias="methodlink")
        resp = self.server.request("POST", "/methodlink")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json(), {"error": "method_not_allowed"})
        self.assertIn("GET", resp.headers.get("Allow", ""))


class TestStats(ServerTestCase):
    def _click(self, alias, referrer=None, user_agent=None):
        headers = {}
        if referrer is not None:
            headers["Referer"] = referrer
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        self.server.request("GET", "/" + alias, headers=headers)

    def test_ac_8_aggregation(self):
        self.create(alias="statslink")
        self._click("statslink", referrer="https://google.com/")
        self._click("statslink", referrer="https://google.com/")
        self._click("statslink")
        stats = self.server.request("GET", "/api/links/statslink/stats").json()
        self.assertEqual(stats["total_clicks"], 3)
        refs = {item["referrer"]: item["count"] for item in stats["top_referrers"]}
        self.assertEqual(refs["https://google.com/"], 2)
        self.assertEqual(refs["(direct)"], 1)

    def test_ac_10_device_classification(self):
        self.create(alias="devicelink")
        self._click("devicelink", user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
        self._click(
            "devicelink",
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148",
        )
        self._click("devicelink", user_agent="curl/8.4.0")
        self._click("devicelink")  # no User-Agent header
        devices = self.server.request("GET", "/api/links/devicelink/stats").json()["devices"]
        self.assertEqual(devices, {"desktop": 1, "mobile": 1, "bot": 2})

    def test_ac_11_zero_clicks(self):
        self.create(alias="zerolink")
        stats = self.server.request("GET", "/api/links/zerolink/stats").json()
        self.assertEqual(stats["total_clicks"], 0)
        self.assertEqual(stats["clicks_by_day"], [])
        self.assertEqual(stats["top_referrers"], [])
        self.assertEqual(stats["devices"], {"desktop": 0, "mobile": 0, "bot": 0})

    def test_ac_12_clicks_by_day_invariants(self):
        self.create(alias="daylink")
        for _ in range(3):
            self._click("daylink")
        stats = self.server.request("GET", "/api/links/daylink/stats").json()
        dates = [item["date"] for item in stats["clicks_by_day"]]
        self.assertEqual(dates, sorted(dates))
        for item in stats["clicks_by_day"]:
            self.assertRegex(item["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(
            sum(item["count"] for item in stats["clicks_by_day"]), stats["total_clicks"]
        )


class TestListingAndDelete(ServerTestCase):
    def test_ac_13_list(self):
        self.create(url="https://example.com/list", alias="listlink")
        resp = self.server.request("GET", "/api/links")
        self.assertEqual(resp.status, 200)
        aliases = [link["alias"] for link in resp.json()["links"]]
        self.assertIn("listlink", aliases)

    def test_ac_14_and_15_delete(self):
        self.create(url="https://example.com/del", alias="deletelink")
        resp = self.server.request("DELETE", "/api/links/deletelink")
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.body, b"")
        self.assertEqual(self.server.request("GET", "/deletelink").status, 404)
        self.assertEqual(
            self.server.request("GET", "/api/links/deletelink/stats").status, 404
        )
        again = self.server.request("DELETE", "/api/links/deletelink")
        self.assertEqual(again.status, 404)
        self.assertEqual(again.json(), {"error": "not_found"})

    def test_ac_16_and_17_recreate_resets_clicks(self):
        self.create(url="https://example.com/old", alias="recreate")
        self.server.request("GET", "/recreate")
        self.server.request("DELETE", "/api/links/recreate")
        self.create(url="https://example.com/new", alias="recreate")
        stats = self.server.request("GET", "/api/links/recreate/stats").json()
        self.assertEqual(stats["total_clicks"], 0)
        resp = self.server.request("GET", "/recreate")
        self.assertEqual(resp.headers["Location"], "https://example.com/new")

    def test_ac_14_list_empty_shape(self):
        other = Server()
        try:
            resp = other.request("GET", "/api/links")
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.json(), {"links": []})
        finally:
            other.stop()


class TestRouting(ServerTestCase):
    def test_ac_21_method_not_allowed(self):
        resp = self.server.request("PUT", "/api/links")
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.json(), {"error": "method_not_allowed"})
        self.assertIn("GET", resp.headers.get("Allow", ""))

    def test_ac_22_unknown_routes(self):
        for path in ("/rota/inexistente", "/health/"):
            resp = self.server.request("GET", path)
            self.assertEqual(resp.status, 404, path)
            self.assertEqual(resp.json(), {"error": "route_not_found"})

    def test_health(self):
        resp = self.server.request("GET", "/health")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.json(), {"status": "ok"})


class TestPersistence(unittest.TestCase):
    def test_ac_18_restart_preserves_data(self):
        data_dir = tempfile.mkdtemp(prefix="shortener-persist-")
        first = Server(data_dir=data_dir)
        try:
            first.post("/api/links", {"url": "https://example.com/p", "alias": "persist"})
            first.request("GET", "/persist")
            before = first.request("GET", "/api/links/persist/stats").json()
        finally:
            first.stop()
        second = Server(data_dir=data_dir)
        try:
            after = second.request("GET", "/api/links/persist/stats").json()
        finally:
            second.stop()
        for key in ("url", "created_at", "total_clicks", "clicks_by_day", "devices"):
            self.assertEqual(after[key], before[key], key)

    def test_ac_19_data_dir_isolation(self):
        first = Server()
        second = Server()
        try:
            first.post("/api/links", {"url": "https://example.com/i", "alias": "isolated"})
            listed = second.request("GET", "/api/links").json()["links"]
            aliases = [link["alias"] for link in listed]
            self.assertNotIn("isolated", aliases)
        finally:
            first.stop()
            second.stop()


if __name__ == "__main__":
    unittest.main()
