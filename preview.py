"""Two local-only HabitatOS simulators and an ephemeral EasyRep trial account."""

import argparse
import hmac
import json
import os
import secrets
import tempfile
import threading
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4


CAGE_IDS = ["CAGE-001", "CAGE-002", "CAGE-003"]
DEMO_PASSWORD = "EasyRep-demo-local!"


def fixture_gateway(store_id, gateway_id, token):
    """Start an isolated loopback gateway. Call shutdown() and server_close() to release it."""
    lock = threading.Lock()
    light_until = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data):
            data = json.dumps(data, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            authenticated = hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode())
            if not authenticated:
                self.respond(401, {"detail": "invalid bearer token"})
            return authenticated

        def cage(self, index, now):
            cage_id = CAGE_IDS[index]
            time = now.isoformat()
            light_on = light_until.get(cage_id, now) > now
            readings = {key: {"value": value, "quality": "valid"} for key, value in
                        (("T1", 25.8), ("T2", 24.6), ("T3", 25.1), ("H1", 64.2))}
            if index == 1:
                readings["T2"] = {"value": None, "quality": "disconnected"}
            telemetry = {"schema_version": 1, "data_source": "simulation", "message_id": str(uuid4()),
                         "sampled_at": time, "received_at": time, "geometry_revision": 1, "placement_revision": 1,
                         "readings": readings, "actuators": {"light": light_on}} if index < 2 else None
            observed = {"state": light_on, "source": "telemetry", "occurred_at": time} if telemetry else None
            return {"cage_id": cage_id, "node_id": f"ESP32-{index + 1:03d}", "mode": "manual",
                    "transport": "simulated", "transport_connected": True, "telemetry": telemetry,
                    "actuators": {"light": {"observed": observed, "max_on_s": 30, "cooldown_s": 0,
                                           "on_blocked_reason": [None, "sensor_unavailable:T2", "missing_telemetry"][index]}}}

        def do_GET(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            now = datetime.now(timezone.utc)
            with lock:
                if path == "/v1/gateway":
                    return self.respond(200, {"site_id": store_id, "gateway_id": gateway_id, "transport": "simulated",
                                              "version": "preview", "release_id": "preview", "cage_ids": CAGE_IDS,
                                              "hardware_outputs_enabled": True})
                if path == "/v1/cages":
                    return self.respond(200, [self.cage(i, now) for i in range(len(CAGE_IDS))])
                if path.startswith("/v1/commands/"):
                    command = self.server.commands.get(path.removeprefix("/v1/commands/"))
                    if command:
                        if command["status"] == "published":
                            command.update(status="applied", applied_at=command["created_at"], acknowledged_at=now.isoformat())
                        return self.respond(200, command)
            self.respond(404, {"detail": "not found"})

        def do_POST(self):
            if not self.authorized():
                return
            parts = self.path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["v1", "cages"] or parts[2] not in CAGE_IDS or parts[3] != "commands":
                return self.respond(404, {"detail": "not found"})
            cage_id = parts[2]
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 4096:
                    raise ValueError
                body = json.loads(self.rfile.read(size))
                command_id = str(UUID(body["request_id"]))
                action, duration = body["action"], body["duration_s"]
                if action not in ("on", "off") or type(duration) not in (int, float) or not 0 <= duration <= 3600 or (duration > 0) != (action == "on"):
                    raise ValueError
                if body["actuator_id"] != "light":
                    return self.respond(404, {"detail": "unknown actuator"})
            except (KeyError, ValueError, TypeError, AttributeError):
                return self.respond(422, {"detail": "invalid command"})
            with lock:
                self.server.posts += 1
                command = self.server.commands.get(command_id)
                fingerprint = dict(body, cage_id=cage_id, source="manual")
                if command and command["request"] != fingerprint:
                    return self.respond(409, {"detail": "request_id already used with a different command"})
                if not command:
                    now = datetime.now(timezone.utc)
                    index = CAGE_IDS.index(cage_id)
                    actuator = self.cage(index, now)["actuators"]["light"]
                    reason = None
                    if action == "on":
                        reason = "duration_exceeds_limit" if duration > actuator["max_on_s"] else actuator["on_blocked_reason"]
                    if reason is None:
                        light_until[cage_id] = now + timedelta(seconds=duration)
                    command = {"schema_version": 1, "command_id": command_id, "site_id": store_id, "gateway_id": gateway_id,
                               "cage_id": cage_id, "node_id": f"ESP32-{index + 1:03d}", "actuator_id": "light", "action": action,
                               "duration_s": duration, "source": "manual", "geometry_revision": 1, "placement_revision": 1,
                               "sequence": len(self.server.commands) + 1, "request": fingerprint, "created_at": now.isoformat(),
                               "expires_at": (now + timedelta(seconds=10)).isoformat(), "status": "rejected" if reason else "published", "reason": reason}
                    self.server.commands[command_id] = command
                self.respond(409 if command["status"] == "rejected" else 202, command)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.posts = 0
    httpd.commands = {}
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    from server import add_store, add_user, init_db, make_server

    parser = argparse.ArgumentParser(description="Run an isolated, local EasyRep demonstration with simulated data.")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="easyrep-preview-") as directory, ExitStack() as cleanup:
        database = Path(directory) / "preview.sqlite3"
        init_db(database)
        for index, name in enumerate(("강남 체험 매장", "마포 체험 매장"), start=1):
            store_id, gateway_id = f"STORE-{index:03d}", f"PI-{index:03d}"
            token = secrets.token_urlsafe(32)
            # Unique process-local names prevent collisions with production token configuration.
            token_env = "EASYREP_PREVIEW_" + secrets.token_hex(16).upper()
            os.environ[token_env] = token
            cleanup.callback(os.environ.pop, token_env, None)
            edge = fixture_gateway(store_id, gateway_id, token)
            cleanup.callback(edge.server_close)
            cleanup.callback(edge.shutdown)
            add_store(database, store_id, name, f"http://127.0.0.1:{edge.server_port}", gateway_id, token_env)
        add_user(database, "demo", DEMO_PASSWORD, "admin")
        site = make_server(database, host="127.0.0.1", port=args.port)
        cleanup.callback(site.server_close)
        print(f"EasyRep local simulation: http://127.0.0.1:{site.server_port}  login: demo / {DEMO_PASSWORD}", flush=True)
        try:
            site.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
