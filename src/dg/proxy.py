"""Loopback Anthropic-compatible router for Claude Code and Desktop.

The client may display its selected Claude model, but the Governor selects the
real upstream per request. OAuth credentials are forwarded only to Anthropic.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import uuid
from http.client import HTTPConnection, HTTPSConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import config, launcher, store, supervisor
from .version import VERSION

ANTHROPIC_HOST = "api.anthropic.com"
_DROP = {"host", "content-length", "connection", "keep-alive", "proxy-authenticate",
         "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
         "accept-encoding"}
_RL = {"anthropic-ratelimit-unified-5h-utilization": ("fiveHour", "usedPercent"),
       "anthropic-ratelimit-unified-5h-reset": ("fiveHour", "resetsAt"),
       "anthropic-ratelimit-unified-7d-utilization": ("sevenDay", "usedPercent"),
       "anthropic-ratelimit-unified-7d-reset": ("sevenDay", "resetsAt")}


class Router:
    def __init__(self, cfg: dict, reload: bool = True):
        self.cfg, self.reload = cfg, reload
        self.lock = threading.Lock()
        self.stats: dict[str, int] = {}
        self.cooldowns: dict[str, tuple[float, str]] = {}
        self.last: dict[str, Any] = {}
        self.instance_id, self.started_at = str(uuid.uuid4()), time.time()

    def _config_and_state(self) -> tuple[dict, dict]:
        cfg = config.load() if self.reload else self.cfg
        with store.session() as con:
            saved = store.kv_get(con, "overrides") or {}
            cfg["overrides"].update({k: v for k, v in saved.items() if v})
            state = supervisor.evaluate(con, cfg)
        self.cfg = cfg
        return cfg, state

    def fallbacks(self) -> list[dict]:
        now = time.time()
        return [t for t in launcher.tiers(self.cfg)
                if self.cooldowns.get(t["name"], (0, ""))[0] <= now]

    def candidates(self) -> tuple[list[tuple[str, dict | None]], str, str]:
        _, state = self._config_and_state()
        tiers = [("tier", t) for t in self.fallbacks()]
        if state["state"] == supervisor.LOCAL:
            return tiers, state["reason"], state["state"]
        return [("anthropic", None), *tiers], state["reason"], state["state"]

    def route(self) -> tuple[str, dict | None, str]:
        candidates, reason, _ = self.candidates()
        if not candidates:
            return "tier", None, "no fallback tier available"
        kind, tier = candidates[0]
        return kind, tier, reason

    def record_quota(self, headers) -> None:
        got: dict[str, dict[str, float]] = {}
        for name, (window, field) in _RL.items():
            raw = headers.get(name)
            if raw is None:
                continue
            try:
                got.setdefault(window, {})[field] = float(raw)
            except (TypeError, ValueError):
                pass
        payload = {"rate_limits": {}}
        for window, key in (("fiveHour", "five_hour"), ("sevenDay", "seven_day")):
            w = got.get(window)
            if w and "usedPercent" in w:
                payload["rate_limits"][key] = {
                    "utilization": w["usedPercent"],
                    "resets_at": int(w.get("resetsAt", 0)) or None}
        if payload["rate_limits"]:
            with store.session() as con:
                supervisor.ingest_statusline(con, payload)

    def note_anthropic(self, status: int, body: bytes, was_probe: bool = False) -> None:
        with store.session() as con:
            if status == 429:
                supervisor.record_hard_limit(
                    con, "rate_limit", body[:200].decode("utf-8", "replace"))
            elif 200 <= status < 300 and was_probe:
                supervisor.clear_hard_limit(con)

    def note_hard_limit(self, status: int, body: bytes) -> None:
        """Backward-compatible name used by older integrations/tests."""
        self.note_anthropic(status, body)

    def cooldown(self, tier: dict, reason: str, auth: bool = False) -> None:
        key = "authCooldownSeconds" if auth else "networkCooldownSeconds"
        default = 300 if auth else 30
        seconds = self.cfg.get("proxy", {}).get(key, default)
        with self.lock:
            self.cooldowns[tier["name"]] = (time.time() + seconds, reason)

    def bump(self, key: str) -> None:
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + 1

    def observed(self, client: str, kind: str, tier: dict | None, status: int,
                 outcome: str) -> None:
        with self.lock:
            self.last = {"at": time.time(), "client": client, "route": kind,
                         "tier": (tier or {}).get("name"), "status": status,
                         "outcome": outcome}

    def snapshot(self) -> dict[str, Any]:
        _, state = self._config_and_state()
        now = time.time()
        with self.lock:
            cooldowns = {k: {"remainingSeconds": max(0, int(v[0] - now)), "reason": v[1]}
                         for k, v in self.cooldowns.items() if v[0] > now}
            return {"ok": True, "version": VERSION, "pid": os.getpid(),
                    "instanceId": self.instance_id, "startedAt": self.started_at,
                    "supervisor": state["state"], "reason": state["reason"],
                    "stats": dict(self.stats), "last": dict(self.last),
                    "cooldowns": cooldowns}


def _connect(base_url: str):
    parts = urlsplit(base_url)
    if parts.scheme == "https":
        return (HTTPSConnection(parts.hostname, parts.port or 443, timeout=900,
                                context=ssl.create_default_context()), parts.path.rstrip("/"))
    return HTTPConnection(parts.hostname, parts.port or 80, timeout=900), parts.path.rstrip("/")


def _rewrite_body(body: bytes, model: str) -> bytes:
    if not body:
        return body
    try:
        payload = json.loads(body)
    except ValueError:
        return body
    if isinstance(payload, dict) and "model" in payload:
        payload["model"] = model
        return json.dumps(payload).encode()
    return body


def _retryable(kind: str, status: int, body: bytes) -> bool:
    malformed = b"failed to generate a valid tool call" in body[:10000].lower()
    if kind == "anthropic":
        return status in (408, 429) or status >= 500
    return malformed or status in (401, 403, 408, 429) or status >= 500


def _client_path(path: str, cfg: dict) -> tuple[str, str]:
    for client, prefix in cfg.get("proxy", {}).get("clientPaths", {}).items():
        if path == prefix or path.startswith(prefix + "/"):
            return client, path[len(prefix):] or "/"
    return "legacy", path


def _token(create: bool = False) -> str | None:
    token_file = config.HOME / "proxy.token"
    try:
        return token_file.read_text("utf-8").strip()
    except OSError:
        if not create:
            return None
    config.ensure_home()
    value = secrets.token_urlsafe(32)
    token_file.write_text(value, "utf-8")
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    return value


def make_handler(router: Router):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dg-proxy"

        def do_GET(self):
            if self.path == "/__dg/health":
                return self._json(router.snapshot())
            self._proxy()

        def do_POST(self):
            if self.path == "/__dg/shutdown":
                expected = _token(False) or ""
                supplied = self.headers.get("x-dg-admin-token") or ""
                if not expected or not hmac.compare_digest(expected, supplied):
                    return self._json({"ok": False, "error": "forbidden"}, 403)
                self._json({"ok": True, "stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._proxy()

        def _json(self, obj: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            client, path = _client_path(self.path, router.cfg)
            candidates, _, state = router.candidates()
            if not candidates:
                return self._json({"type": "error", "error": {"type": "api_error",
                    "message": "dg proxy: local mode is active and no fallback tier is available"}}, 503)
            failures: list[str] = []
            for index, (kind, tier) in enumerate(candidates):
                if kind == "tier" and tier.get("kind") == "lmstudio":
                    ok, detail = launcher.probe_tier(tier, load=True)
                    if not ok:
                        router.cooldown(tier, detail)
                        failures.append(f"{tier['name']}: {detail}")
                        continue
                base = "https://" + ANTHROPIC_HOST if kind == "anthropic" else tier["baseUrl"]
                out = body if kind == "anthropic" else _rewrite_body(body, tier["model"])
                router.bump("anthropic" if kind == "anthropic" else tier["name"])
                conn = None
                try:
                    conn, prefix = _connect(base)
                    conn.request(self.command, prefix + path, body=out,
                                 headers=self._headers(tier if kind == "tier" else None))
                    resp = conn.getresponse()
                except (OSError, ssl.SSLError, socket.timeout) as e:
                    if tier:
                        router.cooldown(tier, type(e).__name__)
                    failures.append(f"{(tier or {}).get('name', 'anthropic')}: {type(e).__name__}")
                    if conn:
                        conn.close()
                    continue
                is_error = resp.status >= 400
                error_body = resp.read() if is_error else b""
                if kind == "anthropic":
                    router.record_quota(resp.headers)
                    router.note_anthropic(resp.status, error_body,
                                          was_probe=state == supervisor.PROBE)
                retry = is_error and _retryable(kind, resp.status, error_body)
                if retry and index + 1 < len(candidates):
                    if tier:
                        router.cooldown(tier, f"HTTP {resp.status}",
                                        auth=resp.status in (401, 403))
                    failures.append(f"{(tier or {}).get('name', 'anthropic')}: HTTP {resp.status}")
                    conn.close()
                    continue
                router.observed(client, kind, tier, resp.status,
                                "served" if not failures else "served-after-retry")
                return self._send_upstream(resp, conn, error_body if is_error else None)
            router.observed(client, "none", None, 503, "all-fallbacks-failed")
            return self._json({"type": "error", "error": {"type": "api_error",
                "message": "dg proxy: all eligible upstreams failed: " + "; ".join(failures)}}, 503)

        def _send_upstream(self, resp: HTTPResponse, conn, buffered: bytes | None) -> None:
            self.send_response(resp.status)
            streaming = False
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in ("connection", "keep-alive", "transfer-encoding", "content-length"):
                    continue
                if lk == "content-type" and "event-stream" in v.lower():
                    streaming = True
                self.send_header(k, v)
            if buffered is not None:
                self.send_header("content-length", str(len(buffered)))
            elif resp.getheader("content-length") is not None and not streaming:
                self.send_header("content-length", resp.getheader("content-length"))
            else:
                self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            try:
                if buffered is not None:
                    self.wfile.write(buffered)
                    return
                chunked = streaming or resp.getheader("content-length") is None
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self.wfile.write((b"%x\r\n%s\r\n" % (len(chunk), chunk)) if chunked else chunk)
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

        def _headers(self, tier: dict | None = None) -> dict[str, str]:
            out = {k: v for k, v in self.headers.items() if k.lower() not in _DROP}
            if tier is None:
                out["Host"] = ANTHROPIC_HOST
                return out
            for k in list(out):
                if k.lower() in ("authorization", "x-api-key", "anthropic-beta"):
                    out.pop(k)
            tok = launcher.tier_token(tier)
            if tok:
                out["Authorization"], out["x-api-key"] = f"Bearer {tok}", tok
            out["anthropic-version"] = "2023-06-01"
            out["Host"] = urlsplit(tier["baseUrl"]).netloc
            return out

        def log_message(self, *args):
            pass

        def handle_one_request(self):
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError, TimeoutError):
                self.close_connection = True
    return Handler


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """A router port has exactly one owner, including on Windows.

    Windows can otherwise allow several Python listeners on the same loopback
    port, distributing requests between old and new releases. That makes an
    upgrade appear intermittently unhealthy and, worse, leaves routing policy
    nondeterministic.
    """
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        return super().server_bind()


def serve(cfg: dict, port: int | None = None) -> int:
    port = port or cfg.get("proxy", {}).get("port", 8787)
    _token(True)
    router = Router(cfg)
    srv = ExclusiveThreadingHTTPServer(("127.0.0.1", port), make_handler(router))
    srv.daemon_threads = True
    with store.session() as con:
        store.kv_set(con, "proxy", {"port": port, "startedAt": time.time(),
                                    "pid": os.getpid(), "instanceId": router.instance_id})
    print(f"dg proxy: listening on http://127.0.0.1:{port} (loopback only)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def health(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/__dg/health", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def stop(port: int, timeout: float = 5.0) -> dict[str, Any]:
    import urllib.error
    import urllib.request
    token = _token(False)
    if not token:
        return {"ok": False, "error": "proxy admin token is missing"}
    req = urllib.request.Request(f"http://127.0.0.1:{port}/__dg/shutdown", data=b"{}",
                                 method="POST", headers={"x-dg-admin-token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)}
