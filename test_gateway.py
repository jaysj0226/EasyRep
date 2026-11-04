import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from gateway import Gateway, GatewayError


class GatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def handle_request(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else None
                self.server.received.append((self.command, self.path, body, self.headers.get("Authorization")))
                status, data = self.server.replies[self.command, self.path]
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/redirect-target")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            do_GET = do_POST = handle_request

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.gateway = Gateway(f"http://127.0.0.1:{self.server.server_port}", "example-token", site_id="STORE-001", gateway_id="PI-001")
        self.identity = {"site_id": "STORE-001", "gateway_id": "PI-001", "cage_ids": ["CAGE-001", "CAGE-002"]}
        self.record = {"command_id": str(uuid4()), "site_id": "STORE-001", "gateway_id": "PI-001", "cage_id": "CAGE-001", "actuator_id": "light", "action": "on", "duration_s": 2, "status": "published"}
        self.payload = {"request_id": self.record["command_id"], "actuator_id": "light", "action": "on", "duration_s": 2}
        self.server.received = []
        self.server.replies = {("GET", "/v1/gateway"): (200, self.identity), ("POST", "/v1/cages/CAGE-001/commands"): (202, self.record)}

    def test_command_and_cage_specific_status(self):
        self.assertEqual(self.gateway.command("CAGE-001", self.payload)["status"], "published")
        self.assertEqual(self.server.received[-1], ("POST", "/v1/cages/CAGE-001/commands", self.payload, "Bearer example-token"))
        self.server.replies["GET", "/v1/commands/" + self.record["command_id"]] = 200, {**self.record, "status": "execution_unverified"}
        self.assertEqual(self.gateway.status(self.record["command_id"], "CAGE-001")["status"], "execution_unverified")
        with self.assertRaisesRegex(GatewayError, "scope_mismatch"):
            self.gateway.status(self.record["command_id"], "CAGE-002")

    def test_reject_wrong_tunnel_before_actuation(self):
        self.identity["site_id"] = "OTHER"
        with self.assertRaisesRegex(GatewayError, "identity_mismatch"):
            self.gateway.command("CAGE-001", self.payload)
        self.assertEqual([r[0] for r in self.server.received], ["GET"])

    def test_refusal_and_no_retry(self):
        self.server.replies["POST", "/v1/cages/CAGE-001/commands"] = 409, {**self.record, "reason": "sensor_unavailable:T1", "status": "rejected", "extra": "do not expose"}
        with self.assertRaises(GatewayError) as caught:
            self.gateway.command("CAGE-001", self.payload)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.result["reason"], "sensor_unavailable:T1")
        self.assertNotIn("extra", caught.exception.result)
        self.assertEqual([r[0] for r in self.server.received], ["GET", "POST"])

    def test_cage_list_scoped_and_redirect_refused(self):
        local = [{"cage_id": cage} for cage in self.identity["cage_ids"]]
        self.server.replies["GET", "/v1/cages"] = 200, local
        self.assertEqual(self.gateway.cages(), local)
        self.server.replies["GET", "/v1/cages"] = 200, local[:1]
        with self.assertRaisesRegex(GatewayError, "scope_mismatch"):
            self.gateway.cages()
        self.server.replies["GET", "/v1/cages"] = 302, {}
        with self.assertRaisesRegex(GatewayError, "gateway_http_302"):
            self.gateway.cages()
        self.assertFalse(any(r[1] == "/redirect-target" for r in self.server.received))

    def test_unsafe_config_and_invalid_inputs(self):
        for url in ("http://192.168.0.2:8000", "https://name:pass@example.org", "http://127.0.0.1:8000/a"):
            with self.subTest(url=url), self.assertRaises(GatewayError):
                Gateway(url, "example-token")
        for payload in ({**self.payload, "duration_s": True}, {**self.payload, "duration_s": 0}, {**self.payload, "actuator_id": "../other"}):
            with self.assertRaises(GatewayError):
                self.gateway.command("CAGE-001", payload)
        self.assertEqual(self.server.received, [])


if __name__ == "__main__":
    unittest.main()
