"""Server-to-server client for a registered HabitatOS Pi. Credentials stay on the server."""

import http.client
import ipaddress
import json
import re
from urllib.parse import urlsplit
from uuid import UUID


IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")
STATES = {"publishing", "published", "requested", "execution_unverified", "applied", "rejected", "expired", "interrupted"}
MAX_BODY = 2 * 1024 * 1024


class GatewayError(Exception):
    def __init__(self, status, code, result=None):
        super().__init__(code)
        self.status = status
        self.code = code
        self.result = result


def _identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise GatewayError(400, "invalid_command_id") from None


class Gateway:
    def __init__(self, base_url, token, *, site_id=None, gateway_id=None):
        try:
            self.url = urlsplit(base_url)
            host, self.port = self.url.hostname, self.url.port
            if not host or self.url.username is not None or self.url.password is not None or self.url.path not in ("", "/") or self.url.query or self.url.fragment:
                raise ValueError
            if self.url.scheme != "https":
                if self.url.scheme != "http" or (host != "localhost" and not ipaddress.ip_address(host).is_loopback):
                    raise ValueError
            if not isinstance(token, str) or not re.fullmatch(r"[!-~]+", token):
                raise ValueError
            if any(value is not None and not _identifier(value) for value in (site_id, gateway_id)):
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise GatewayError(503, "invalid_gateway_configuration") from None
        self.token = token
        self.site_id = site_id
        self.gateway_id = gateway_id

    def _exchange(self, method, path, payload=None):
        connection_type = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
        conn = connection_type(self.url.hostname, self.port, timeout=4)
        try:
            headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
            body = None
            if payload is not None:
                headers["Content-Type"] = "application/json"
                body = json.dumps(payload, allow_nan=False).encode()
            conn.request(method, path, body, headers)
            response = conn.getresponse()
            # http.client does not follow redirects, use ambient proxies, or retry a POST.
            if response.status not in (200, 202, 409):
                if response.status == 404:
                    raise GatewayError(404, "gateway_resource_missing")
                raise GatewayError(502, "gateway_http_" + str(response.status))
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise GatewayError(502, "invalid_gateway_response")
            return response.status, json.loads(raw)
        except (ValueError, RecursionError):
            raise GatewayError(502, "invalid_gateway_response") from None
        except (OSError, http.client.HTTPException):
            raise GatewayError(503, "gateway_unavailable") from None
        finally:
            conn.close()

    def identity(self):
        status, identity = self._exchange("GET", "/v1/gateway")
        if status != 200 or not isinstance(identity, dict) or not all(_identifier(identity.get(key)) for key in ("site_id", "gateway_id")):
            raise GatewayError(502, "invalid_gateway_identity")
        if (self.site_id is not None and identity["site_id"] != self.site_id) or (self.gateway_id is not None and identity["gateway_id"] != self.gateway_id):
            raise GatewayError(502, "gateway_identity_mismatch")
        if not isinstance(identity.get("cage_ids"), list) or not all(_identifier(value) for value in identity["cage_ids"]):
            raise GatewayError(502, "invalid_gateway_identity")
        return identity

    def cages(self):
        identity = self.identity()
        status, cages = self._exchange("GET", "/v1/cages")
        if status != 200 or not isinstance(cages, list) or not all(isinstance(c, dict) and _identifier(c.get("cage_id")) for c in cages):
            raise GatewayError(502, "invalid_gateway_response")
        ids = {c["cage_id"] for c in cages}
        if len(ids) != len(cages) or ids != set(identity["cage_ids"]):
            raise GatewayError(502, "gateway_scope_mismatch")
        return cages

    def _record(self, status, record, identity, command_id, cage_id):
        if status == 409 and isinstance(record, dict) and "command_id" not in record:
            raise GatewayError(409, "gateway_conflict")
        if not isinstance(record, dict) or not isinstance(record.get("status"), str) or record["status"] not in STATES:
            raise GatewayError(502, "invalid_gateway_response")
        if (record.get("command_id") != command_id or record.get("cage_id") not in identity["cage_ids"]
                or (cage_id is not None and record["cage_id"] != cage_id)
                or any(record.get(key) != identity[key] for key in ("site_id", "gateway_id"))):
            raise GatewayError(502, "gateway_scope_mismatch")

    def command(self, cage_id, payload):
        if not _identifier(cage_id) or not isinstance(payload, dict) or set(payload) != {"request_id", "actuator_id", "action", "duration_s"}:
            raise GatewayError(400, "invalid_command")
        command_id = _uuid(payload["request_id"])
        duration = payload["duration_s"]
        if (not _identifier(payload["actuator_id"]) or payload["action"] not in ("on", "off")
                or type(duration) not in (int, float) or not 0 <= duration <= 3600
                or (duration > 0) != (payload["action"] == "on")):
            raise GatewayError(400, "invalid_command")
        identity = self.identity()
        if cage_id not in identity["cage_ids"]:
            raise GatewayError(404, "gateway_resource_missing")
        status, record = self._exchange("POST", f"/v1/cages/{cage_id}/commands", {**payload, "request_id": command_id})
        self._record(status, record, identity, command_id, cage_id)
        if any(record.get(key) != payload[key] for key in ("actuator_id", "action", "duration_s")):
            raise GatewayError(502, "gateway_command_mismatch")
        if status == 409 and record["status"] == "rejected":
            safe = {key: record.get(key) for key in ("site_id", "gateway_id", "cage_id", "command_id", "actuator_id", "action", "duration_s", "status")}
            reason = record.get("reason")
            safe["reason"] = reason if isinstance(reason, str) and re.fullmatch(r"[a-zA-Z0-9_:\-]{1,128}", reason) else "gateway_rejected"
            raise GatewayError(409, "gateway_rejected", safe)
        if status != 202:
            raise GatewayError(502, "invalid_gateway_response")
        return record

    def status(self, command_id, cage_id=None):
        command_id = _uuid(command_id)
        identity = self.identity()
        if cage_id is not None and cage_id not in identity["cage_ids"]:
            raise GatewayError(404, "gateway_resource_missing")
        status, record = self._exchange("GET", "/v1/commands/" + command_id)
        if status != 200:
            raise GatewayError(502, "invalid_gateway_response")
        self._record(status, record, identity, command_id, cage_id)
        return record
