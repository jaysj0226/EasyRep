"""EasyRep store portal. Run `python server.py --help` for provisioning."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
from getpass import getpass
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from gateway import Gateway, GatewayError


STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {
    "/index.html": "text/html; charset=utf-8",
    "/style.css": "text/css; charset=utf-8",
    "/app.js": "text/javascript; charset=utf-8",
    "/canopy.webp": "image/webp",
}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,79}\Z")
SESSION_LIFETIME = 8 * 3600
PASSWORD_ITERATIONS = 600_000
COMMAND_FIELDS = ("request_id", "store_id", "cage_id", "username", "actuator_id",
                  "action", "duration_s", "status", "created_at", "updated_at")


def db_open(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db(path):
    with closing(db_open(path)) as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS stores (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL,
                gateway_id TEXT NOT NULL, token_env TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, salt BLOB NOT NULL, digest BLOB NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin','operator','viewer')),
                store_id TEXT REFERENCES stores(id),
                CHECK((role='admin') = (store_id IS NULL)));
            CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, username TEXT NOT NULL REFERENCES users(username),
                csrf TEXT NOT NULL, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (
                client TEXT PRIMARY KEY, started INTEGER NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (
                request_id TEXT PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id),
                cage_id TEXT NOT NULL, username TEXT NOT NULL REFERENCES users(username),
                actuator_id TEXT NOT NULL, action TEXT NOT NULL, duration_s REAL NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS commands_by_store ON commands(store_id, created_at DESC);
        """)


def add_store(path, store_id, name, url, gateway_id, token_env):
    if not IDENTIFIER.fullmatch(store_id) or not IDENTIFIER.fullmatch(gateway_id):
        raise ValueError("Use letters, numbers, _ or - for store and gateway identifiers.")
    if not name.strip() or len(name) > 100 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ValueError("A store name and token environment variable name are required.")
    # The same URL rules apply during provisioning and at dispatch time.
    Gateway(url, "configuration-check", site_id=store_id, gateway_id=gateway_id)
    with closing(db_open(path)) as db, db:
        db.execute("INSERT INTO stores VALUES (?,?,?,?,?)", (store_id, name, url, gateway_id, token_env))


def password_digest(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PASSWORD_ITERATIONS)


def add_user(path, username, password, role, store_id=None):
    if not USERNAME.fullmatch(username) or not 12 <= len(password) <= 256:
        raise ValueError("Use a valid username and a password of 12 to 256 characters.")
    if role not in ("admin", "operator", "viewer") or (role == "admin") != (store_id is None):
        raise ValueError("Store users require --store; administrators have no assigned store.")
    salt = secrets.token_bytes(16)
    with closing(db_open(path)) as db, db:
        db.execute("INSERT INTO users VALUES (?,?,?,?,?)",
                   (username, salt, password_digest(password, salt), role, store_id))


def now():
    return datetime.now(timezone.utc).isoformat()


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def invalid():
    return ApiError(400, "invalid_request", "입력값을 확인해 주세요.")


def missing():
    return ApiError(404, "not_found", "대상을 찾을 수 없습니다.")


def command_json(row):
    return {key: row[key] for key in COMMAND_FIELDS}


def valid_request(body):
    if set(body) != {"request_id", "actuator_id", "action", "duration_s"}:
        raise invalid()
    try:
        key = UUID(body["request_id"])
    except (ValueError, TypeError, AttributeError):
        raise invalid() from None
    value = body["duration_s"]
    if (key.version != 4 or str(key) != body["request_id"] or
            not isinstance(body["actuator_id"], str) or not IDENTIFIER.fullmatch(body["actuator_id"]) or
            body["action"] not in ("on", "off") or type(value) not in (int, float) or
            not 0 <= value <= 3600 or (body["action"] == "off") != (value == 0)):
        raise invalid()


def gateway_for(store):
    token = os.environ.get(store["token_env"])
    if not token:
        raise ApiError(503, "gateway_unconfigured", "이 매장의 Pi 연결 정보가 설정되지 않았습니다.")
    return Gateway(store["url"], token, site_id=store["id"], gateway_id=store["gateway_id"])


def saved(db, request_id):
    return db.execute("SELECT * FROM commands WHERE request_id=?", (request_id,)).fetchone()


def check_replay(row, store_id, cage_id, username, body):
    expected = (store_id, cage_id, username, body["actuator_id"], body["action"], body["duration_s"])
    if tuple(row[k] for k in ("store_id", "cage_id", "username", "actuator_id", "action", "duration_s")) != expected:
        raise ApiError(409, "request_id_conflict", "이미 쓰인 요청 번호입니다.")


def save_status(db, row, status):
    # A slower response must never undo a later observation of the same command.
    with db:
        db.execute("""UPDATE commands SET status=?,updated_at=?,revision=revision+1
                      WHERE request_id=? AND revision=?""", (status, now(), row["request_id"], row["revision"]))
    return saved(db, row["request_id"])


PI_STATUSES = frozenset(("publishing", "published", "applied", "rejected", "requested",
                         "execution_unverified", "interrupted", "expired"))


def status_from(result, row):
    if (not isinstance(result, dict) or result.get("command_id") != row["request_id"]
            or result.get("cage_id") != row["cage_id"] or result.get("site_id") != row["store_id"]
            or result.get("actuator_id") != row["actuator_id"] or result.get("action") != row["action"]
            or result.get("duration_s") != row["duration_s"] or result.get("status") not in PI_STATUSES):
        return "unknown"
    return result["status"]


def verify_target(gateway, cage_id, body):
    cage = next((c for c in gateway.cages() if c.get("cage_id") == cage_id), None)
    if cage is None:
        raise missing()
    actuators = cage.get("actuators")
    info = actuators.get(body["actuator_id"]) if isinstance(actuators, dict) else None
    if not isinstance(info, dict) or info.get("enabled") is False:
        raise ApiError(409, "actuator_unavailable", "이 사육장에 사용 가능한 장치가 없습니다.")
    if cage.get("mode") != "manual" or cage.get("transport_connected") is not True:
        raise ApiError(409, "control_unavailable", "Pi의 수동 제어 모드와 장치 연결을 확인해 주세요.")
    if body["action"] == "on":
        maximum = info.get("max_on_s")
        if ("on_blocked_reason" not in info or info["on_blocked_reason"] is not None or type(maximum) not in (int, float)
                or not math.isfinite(maximum) or body["duration_s"] > maximum):
            raise ApiError(409, "activation_blocked", "Pi의 안전 조건 또는 최대 작동 시간에 걸렸습니다.")


class Handler(BaseHTTPRequestHandler):
    server_version = "EasyRep"
    timeout = 15

    def log_message(self, fmt, *args):
        # URLs, cookies, bodies and gateway credentials never enter the console log.
        pass

    def reply(self, status, data, kind="application/json; charset=utf-8", cookie=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                         "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def cookie(self, token, age):
        return (f"easyrep_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}"
                + ("; Secure" if self.server.secure_cookie else ""))

    def cookie_hash(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            token = jar["easyrep_session"].value
        except (KeyError, ValueError, CookieError):
            return ""
        return hashlib.sha256(token.encode()).hexdigest()

    def session(self, db):
        row = db.execute("""SELECT users.username,role,store_id,csrf FROM sessions JOIN users USING(username)
                            WHERE sessions.digest=? AND expires>?""", (self.cookie_hash(), time.time())).fetchone()
        if row is None:
            raise ApiError(401, "unauthorized", "로그인해 주세요.")
        return row

    def body(self):
        if self.headers.get_content_type() != "application/json" or "Transfer-Encoding" in self.headers:
            raise invalid()
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 1 <= length <= 4096:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, UnicodeError, RecursionError):
            raise invalid() from None

    def login(self, db, body):
        username, password = body.get("username"), body.get("password")
        if (not isinstance(username, str) or not USERNAME.fullmatch(username) or
                not isinstance(password, str) or not 1 <= len(password) <= 256):
            raise invalid()
        client = self.client_address[0]
        stamp = int(time.time())
        with db:
            db.execute("DELETE FROM attempts WHERE started<=?", (stamp - 300,))
            db.execute("""INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(client)
                          DO UPDATE SET count=count+1""", (client, stamp))
            count = db.execute("SELECT count FROM attempts WHERE client=?", (client,)).fetchone()[0]
        if count > 30:
            raise ApiError(429, "too_many_attempts", "잠시 후 다시 로그인해 주세요.")
        row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        digest = password_digest(password, row["salt"] if row else bytes(16))
        if row is None or not hmac.compare_digest(digest, row["digest"]):
            raise ApiError(401, "invalid_credentials", "사용자명 또는 비밀번호를 확인해 주세요.")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with db:
            db.execute("UPDATE attempts SET count=count-1 WHERE client=?", (client,))
            db.execute("DELETE FROM sessions WHERE expires<=? OR digest=?", (stamp, self.cookie_hash()))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), username, csrf, stamp + SESSION_LIFETIME))
        self.reply(200, self.identity(row, csrf), cookie=self.cookie(token, SESSION_LIFETIME))

    @staticmethod
    def identity(row, csrf):
        return {"user": {key: row[key] for key in ("username", "role", "store_id")}, "csrf": csrf}

    def dispatch(self, db, store, cage_id, user, body):
        valid_request(body)
        request_id = body["request_id"]
        stamp = now()
        # Reserve the ID durably before touching hardware, including across process crashes.
        with db:
            inserted = db.execute("""INSERT OR IGNORE INTO commands
                        (request_id,store_id,cage_id,username,actuator_id,action,duration_s,status,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,'unknown',?,?)""",
                        (request_id, store["id"], cage_id, user["username"], body["actuator_id"],
                         body["action"], body["duration_s"], stamp, stamp)).rowcount
        row = saved(db, request_id)
        check_replay(row, store["id"], cage_id, user["username"], body)
        if not inserted:
            return self.reply(409 if row["status"] in ("not_sent", "rejected") else 200,
                              {"command": command_json(row)})
        try:
            gateway = gateway_for(store)
            verify_target(gateway, cage_id, body)
        except (ApiError, GatewayError) as error:
            row = save_status(db, row, "not_sent")
            message = str(error) if isinstance(error, ApiError) else "매장 Pi와 연결할 수 없어 장치 요청을 보내지 않았습니다."
            return self.reply(error.status, {"code": error.code, "error": message, "command": command_json(row)})
        try:
            result = gateway.command(cage_id, body)
        except GatewayError as error:
            result = error.result
        status = status_from(result, row)
        row = save_status(db, row, status)
        self.reply(409 if row["status"] == "rejected" else 202, {"command": command_json(row)})

    def route(self, db):
        method = self.command
        url = urlsplit(self.path)
        asset = "/index.html" if url.path == "/" else url.path
        if method == "GET" and asset in STATIC_TYPES:
            return self.reply(200, (STATIC / asset[1:]).read_bytes(), STATIC_TYPES[asset])
        if not url.path.startswith("/api/"):
            raise missing()
        parts = url.path[5:].split("/")
        if method == "POST":
            origin = self.headers.get("Origin")
            if origin and (origin == "null" or urlsplit(origin).netloc != self.headers.get("Host")):
                raise ApiError(403, "origin_mismatch", "같은 사이트에서 요청해 주세요.")
            body = self.body()
            if parts == ["login"]:
                return self.login(db, body)
        user = self.session(db)
        if method == "POST" and not secrets.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), user["csrf"].encode()):
            raise ApiError(403, "csrf", "페이지를 새로고침하고 다시 시도해 주세요.")
        if parts == ["session"] and method == "GET":
            return self.reply(200, self.identity(user, user["csrf"]))
        if parts == ["logout"] and method == "POST":
            with db:
                db.execute("DELETE FROM sessions WHERE digest=?", (self.cookie_hash(),))
            return self.reply(200, {"ok": True}, cookie=self.cookie("", 0))
        if parts == ["stores"] and method == "GET":
            stores = db.execute("SELECT id,name FROM stores WHERE ?='admin' OR id=? ORDER BY name,id",
                                (user["role"], user["store_id"]))
            return self.reply(200, {"stores": [dict(row) for row in stores]})
        if len(parts) < 3 or parts[0] != "stores" or not IDENTIFIER.fullmatch(parts[1]):
            raise missing()
        store = db.execute("SELECT * FROM stores WHERE id=? AND (?='admin' OR id=?)",
                           (parts[1], user["role"], user["store_id"])).fetchone()
        if not store:
            raise missing()
        rest = parts[2:]
        if rest == ["cages"] and method == "GET":
            return self.reply(200, {"store": {"id": store["id"], "name": store["name"]},
                                    "cages": gateway_for(store).cages()})
        if rest == ["commands"] and method == "GET":
            cage = parse_qs(url.query).get("cage_id", [None])[0]
            if cage is not None and not IDENTIFIER.fullmatch(cage):
                raise invalid()
            rows = db.execute("""SELECT * FROM commands WHERE store_id=? AND (? IS NULL OR cage_id=?)
                                 ORDER BY created_at DESC LIMIT 100""", (store["id"], cage, cage))
            return self.reply(200, {"commands": [command_json(record) for record in rows]})
        if len(rest) == 2 and rest[0] == "commands" and method == "GET":
            row = saved(db, rest[1])
            if row is None or row["store_id"] != store["id"]:
                raise missing()
            if row["status"] not in ("applied", "rejected", "execution_unverified", "not_sent"):
                try:
                    result = gateway_for(store).status(row["request_id"], cage_id=row["cage_id"])
                except GatewayError:
                    result = None
                if result:
                    status = status_from(result, row)
                    if status != "unknown" and status != row["status"]:
                        row = save_status(db, row, status)
            return self.reply(200, {"command": command_json(row)})
        if len(rest) == 3 and rest[0] == "cages" and rest[2] == "commands" and method == "POST":
            if user["role"] == "viewer":
                raise ApiError(403, "read_only", "조회 전용 계정입니다.")
            if not IDENTIFIER.fullmatch(rest[1]):
                raise missing()
            return self.dispatch(db, store, rest[1], user, body)
        raise missing()

    def handle_api(self):
        try:
            with closing(db_open(self.server.db_path)) as db:
                self.route(db)
        except ApiError as error:
            self.reply(error.status, {"code": error.code, "error": str(error)})
        except GatewayError:
            self.reply(502, {"code": "gateway_unavailable", "error": "매장 Pi 연결 또는 매장 식별 정보를 확인해 주세요."})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (sqlite3.Error, OSError):
            self.reply(503, {"code": "unavailable", "error": "서비스 연결을 확인해 주세요."})
        except (ValueError, UnicodeError):
            self.reply(400, {"code": "invalid_request", "error": HTTPStatus.BAD_REQUEST.phrase})

    do_POST = do_GET = handle_api


def make_server(db_path, host="127.0.0.1", port=8080, secure_cookie=False):
    init_db(db_path)
    server = ThreadingHTTPServer((host, port), Handler)
    server.db_path = db_path
    server.secure_cookie = secure_cookie
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="easyrep.sqlite3")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    store = sub.add_parser("add-store")
    store.add_argument("id")
    store.add_argument("name")
    store.add_argument("--url", required=True)
    store.add_argument("--gateway-id", required=True)
    store.add_argument("--token-env", required=True)
    user = sub.add_parser("add-user")
    user.add_argument("username")
    user.add_argument("--role", choices=("admin", "operator", "viewer"), required=True)
    user.add_argument("--store")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8080, type=int)
    serve.add_argument("--secure-cookie", action="store_true", help="required behind an HTTPS reverse proxy")
    args = parser.parse_args()
    try:
        if args.cmd == "serve":
            if args.host not in ("127.0.0.1", "localhost") and not args.secure_cookie:
                parser.error("Public/LAN binding requires an HTTPS reverse proxy and --secure-cookie.")
            with make_server(args.db, args.host, args.port, args.secure_cookie) as server:
                print(f"EasyRep listening on {args.host}:{server.server_port}", flush=True)
                server.serve_forever()
        else:
            init_db(args.db)
            if args.cmd == "add-store":
                add_store(args.db, args.id, args.name, args.url, args.gateway_id, args.token_env)
            elif args.cmd == "add-user":
                password = os.environ.get("EASYREP_NEW_PASSWORD") or getpass("New password: ")
                add_user(args.db, args.username, password, args.role, args.store)
    except (ValueError, sqlite3.Error, GatewayError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
