"""Exercise the real HTTP portal, its SQLite store and two isolated test gateways."""

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
from unittest.mock import patch
from uuid import uuid4

import server
from gateway import Gateway, GatewayError
from preview import fixture_gateway


class Browser:
    def __init__(self, base):
        self.base = base
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.csrf = None

    def api(self, path, body=None, csrf=True):
        headers = {"Content-Type": "application/json"}
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        data = None if body is None else json.dumps(body).encode()
        try:
            response = self.opener.open(Request(self.base + path, data=data, headers=headers), timeout=8)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def login(self, username):
        status, _ = self.api("/api/login", {"username": username, "password": "fixture-only-password"})
        if status != 200:
            raise AssertionError(f"Login failed ({status})")
        self.csrf = self.api("/api/session")[1]["csrf"]
        return self


class PortalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        db = Path(tmp.name) / "test.db"
        server.init_db(db)
        cls.edges = []
        for store in ("STORE-A", "STORE-B"):
            env = "EASYREP_TEST_" + store[-1]
            token = "test-secret-" + str(uuid4())
            os.environ[env] = token
            cls.addClassCleanup(os.environ.pop, env)
            edge = fixture_gateway(store, "PI-001", token)
            cls.addClassCleanup(edge.server_close)
            cls.addClassCleanup(edge.shutdown)
            cls.edges.append(edge)
            server.add_store(db, store, store, f"http://127.0.0.1:{edge.server_port}", "PI-001", env)
        for username, role, store in (("owner", "admin", None), ("alice", "operator", "STORE-A"),
                                      ("bob", "operator", "STORE-B"), ("observer", "viewer", "STORE-A")):
            server.add_user(db, username, "fixture-only-password", role, store)
        portal = server.make_server(db, host="127.0.0.1", port=0)
        cls.addClassCleanup(portal.server_close)
        cls.addClassCleanup(portal.shutdown)
        threading.Thread(target=portal.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{portal.server_port}"

    def test_canopy_content_type_and_static_allowlist(self):
        client = Browser(self.base)
        with tempfile.TemporaryDirectory() as temp, patch.object(server, "STATIC", Path(temp)):
            sample = b"RIFF\x0c\x00\x00\x00WEBPVP8 \x00\x00\x00\x00"
            (server.STATIC / "canopy.webp").write_bytes(sample)
            (server.STATIC / "private.webp").write_bytes(sample)
            with client.opener.open(self.base + "/canopy.webp", timeout=5) as response:
                self.assertEqual(response.headers["Content-Type"], "image/webp")
                self.assertEqual(response.read(), sample)
            self.assertEqual(client.api("/private.webp")[0], 404)
            self.assertEqual(client.api("/../server.py")[0], 404)

    def test_store_and_sensor_visibility(self):
        anon = Browser(self.base)
        self.assertEqual(anon.api("/api/stores")[0], 401)
        owner = Browser(self.base).login("owner")
        self.assertEqual({s["id"] for s in owner.api("/api/stores")[1]["stores"]}, {"STORE-A", "STORE-B"})
        alice = Browser(self.base).login("alice")
        self.assertEqual([s["id"] for s in alice.api("/api/stores")[1]["stores"]], ["STORE-A"])
        self.assertIn(alice.api("/api/stores/STORE-B/cages")[0], (403, 404))
        cages = alice.api("/api/stores/STORE-A/cages")[1]["cages"]
        good = next(c for c in cages if c["cage_id"] == "CAGE-001")
        self.assertEqual(good["telemetry"]["readings"]["T1"]["quality"], "valid")
        self.assertTrue(any(c["telemetry"] is None for c in cages))
        self.assertTrue(any(r["quality"] != "valid" for c in cages if c["telemetry"]
                            for r in c["telemetry"]["readings"].values()))

    def test_local_control_is_scoped_and_delivered_once(self):
        route = "/api/stores/STORE-A/cages/CAGE-001/commands"
        alice = Browser(self.base).login("alice")
        second = Browser(self.base).login("alice")
        observer = Browser(self.base).login("observer")
        bob = Browser(self.base).login("bob")
        payload = {"request_id": str(uuid4()), "actuator_id": "light", "action": "on", "duration_s": 2}
        before = [edge.posts for edge in self.edges]
        self.assertIn(bob.api(route, payload)[0], (403, 404))
        self.assertEqual(observer.api(route, payload)[0], 403)
        self.assertIn(alice.api(route, payload, csrf=False)[0], (400, 403))
        self.assertEqual([edge.posts for edge in self.edges], before)
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda b: b.api(route, payload), (alice, second)))
        self.assertTrue(all(code in (200, 201, 202, 409) for code, _ in replies), replies)
        self.assertIn(202, [code for code, _ in replies])
        self.assertEqual([edge.posts for edge in self.edges], [before[0] + 1, before[1]])
        lookup = "/api/stores/STORE-A/commands/" + payload["request_id"]
        self.assertEqual(alice.api(lookup)[1]["command"]["status"], "applied")
        self.assertEqual(alice.api(route, {**payload, "duration_s": 3})[0], 409)
        self.assertIn(bob.api(lookup)[0], (403, 404))
        self.assertEqual(bob.api(lookup.replace("STORE-A", "STORE-B"))[0], 404)
        audit = alice.api("/api/stores/STORE-A/commands?cage_id=CAGE-001")[1]["commands"]
        record = next(c for c in audit if c["request_id"] == payload["request_id"])
        self.assertEqual((record["store_id"], record["username"], record["cage_id"]),
                         ("STORE-A", "alice", "CAGE-001"))
        self.assertEqual(bob.api("/api/stores/STORE-B/commands")[1]["commands"], [])
        self.assertIn(alice.api("/api/logout", {})[0], (200, 204))
        self.assertEqual(alice.api("/api/stores")[0], 401)

    def test_lost_receipt_and_preflight_refusal(self):
        alice = Browser(self.base).login("alice")
        route = "/api/stores/STORE-A/cages/CAGE-001/commands"
        payload = {"request_id": str(uuid4()), "actuator_id": "light", "action": "on", "duration_s": 2}
        before = self.edges[0].posts
        deliver = Gateway.command

        def lose_receipt(gateway, cage, body):
            deliver(gateway, cage, body)
            raise GatewayError(503, "gateway_unavailable")

        with patch.object(Gateway, "command", lose_receipt):
            self.assertEqual(alice.api(route, payload)[1]["command"]["status"], "unknown")
            self.assertEqual(alice.api(route, payload)[1]["command"]["status"], "unknown")
        self.assertEqual(self.edges[0].posts, before + 1)
        lookup = "/api/stores/STORE-A/commands/" + payload["request_id"]
        self.assertEqual(alice.api(lookup)[1]["command"]["status"], "applied")
        denied = {**payload, "request_id": str(uuid4())}
        for _ in range(2):
            status, reply = alice.api(route.replace("CAGE-001", "CAGE-002"), denied)
            self.assertEqual((status, reply["command"]["status"]), (409, "not_sent"))
        self.assertEqual(self.edges[0].posts, before + 1)


if __name__ == "__main__":
    unittest.main()
