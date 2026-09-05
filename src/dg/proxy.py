"""`dg proxy` -- a local router that gives Desktop the failover it cannot get
from a process relaunch.

Why this exists
---------------
`dg launch` switches backends by restarting Claude Code. The Claude Desktop app
spawns its own bundled `claude.exe`, so nothing outside can wrap it: measured on
2026-09-05, Desktop never invokes the statusLine hook and cannot be relaunched
by us. A per-request router is the only mechanism that works there.

Why it is a router and not a translator
---------------------------------------
All three backends already speak the Anthropic Messages API -- Anthropic itself,
LM Studio natively, and the Oracle gateway. So this forwards bytes; it does not
convert protocols. That is what keeps it ~200 lines instead of a second product.

Credentials
-----------
Verified live: Claude Code sends its subscription OAuth token to a custom
`ANTHROPIC_BASE_URL` (`Authorization: Bearer sk-ant-oat01...`). For Anthropic
traffic this proxy passes that header through **verbatim and unread** -- it is
never logged, parsed or stored. For a fallback tier the header is replaced with
that tier's own key, so the OAuth token never leaves the machine except to
Anthropic itself.

The proxy binds to loopback only.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import config, launcher, store, supervisor

ANTHROPIC_HOST = "api.anthropic.com"

# Hop-by-hop headers must not be forwarded (RFC 7230 6.1); Host and
# Content-Length are recomputed per upstream.
_DROP = {"host", "content-length", "connection", "keep-alive", "proxy-authenticate",
         "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade",
         "accept-encoding"}

# Anthropic returns quota on every response. Capturing it here is what restores
# SAVE mode in Desktop, where no statusline hook ever runs.
_RL = {"anthropic-ratelimit-unified-5h-utilization": ("fiveHour", "usedPercent"),
       "anthropic-ratelimit-unified-5h-reset": ("fiveHour", "resetsAt"),
       "anthropic-ratelimit-unified-7d-utilization": ("sevenDay", "usedPercent"),
       "anthropic-ratelimit-unified-7d-reset": ("sevenDay", "resetsAt")}


class Router:
    """Decides the upstream for each request and records what it learns."""

    def __init__(self, cfg: dict, reload: bool = True):
        self.cfg = cfg
        # Re-read config per request so edits take effect without a restart.
        # Off when the caller owns the config (tests, embedded use).
        self.reload = reload
        self.lock = threading.Lock()
        # One SQLite connection per handler thread: connections are
        # thread-bound, and ThreadingHTTPServer serves each request on its own.
        self._local = threading.local()
        self._tier_cache: tuple[float, dict | None] = (0.0, None)
        self.stats: dict[str, int] = {}

    @property
    def con(self):
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._local.con = store.connect()
        return con

    def route(self) -> tuple[str, dict | None, str]:
        """('anthropic'|'tier', tier, reason)."""
        cfg = config.load() if self.reload else self.cfg
        saved = store.kv_get(self.con, "overrides") or {}
        cfg["overrides"].update({k: v for k, v in saved.items() if v})
        self.cfg = cfg
        d = launcher.decide_route(self.con, cfg, probe=False)
        if d["route"] == launcher.ANTHROPIC:
            return "anthropic", None, d["reason"]
        t = self._pick_tier()
        if t is None:
            # Nothing local answered: Anthropic is still the best try, and its
            # own error is more useful to the user than one we invent.
            return "anthropic", None, "no fallback tier answered; trying anthropic"
        return "tier", t, d["reason"]

    def _pick_tier(self) -> dict | None:
        """First usable tier, cached briefly so we do not probe every request."""
        age, cached = self._tier_cache
        if cached is not None and time.time() - age < 30:
            return cached
        with self.lock:
            t, _ = launcher.first_usable_tier(self.cfg)
            self._tier_cache = (time.time(), t)
        return t

    def record_quota(self, headers) -> None:
        """Harvest Anthropic's rate-limit headers. Zero cost, no inference."""
        got: dict[str, dict[str, float]] = {}
        for name, (window, field) in _RL.items():
            raw = headers.get(name)
            if raw is None:
                continue
            try:
                got.setdefault(window, {})[field] = float(raw)
            except (TypeError, ValueError):
                pass
        if not got:
            return
        payload = {"rate_limits": {}}
        for window, key in (("fiveHour", "five_hour"), ("sevenDay", "seven_day")):
            w = got.get(window)
            if w and "usedPercent" in w:
                # Raw header value: a 0..1 fraction. supervisor._window owns
                # the conversion so there is exactly one place that knows.
                payload["rate_limits"][key] = {
                    "utilization": w["usedPercent"],
                    "resets_at": int(w.get("resetsAt", 0)) or None}
        if payload["rate_limits"]:
            supervisor.ingest_statusline(self.con, payload)

    def note_hard_limit(self, status: int, body_head: bytes) -> None:
        """A 429 from Anthropic is the hard limit the StopFailure hook records
        in a terminal session. In Desktop this is where we learn it."""
        if status != 429:
            return
        supervisor.record_hard_limit(self.con, "rate_limit",
                                     body_head[:200].decode("utf-8", "replace"))

    def bump(self, key: str) -> None:
        with self.lock:
            self.stats[key] = self.stats.get(key, 0) + 1


def _debug(msg: str) -> None:
    """Diagnostics only, never headers: those carry credentials."""
    try:
        with open(os.environ["DG_PROXY_DEBUG"], "a", encoding="utf-8") as fh:
            fh.write(msg + chr(10))
    except OSError:
        pass


def _connect(base_url: str):
    parts = urlsplit(base_url)
    host, port = parts.hostname, parts.port
    if parts.scheme == "https":
        ctx = ssl.create_default_context()
        return HTTPSConnection(host, port or 443, timeout=900, context=ctx), parts.path.rstrip("/")
    return HTTPConnection(host, port or 80, timeout=900), parts.path.rstrip("/")


def _rewrite_body(body: bytes, model: str) -> bytes:
    """Point the request at the tier's own model name."""
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


def make_handler(router: Router):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dg-proxy"

        def do_POST(self):
            self._proxy()

        def do_GET(self):
            if self.path == "/__dg/health":
                return self._json({"ok": True, "stats": router.stats,
                                   "route": router.route()[0]})
            self._proxy()

        # -- plumbing ----------------------------------------------------
        def _json(self, obj: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self):
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            kind, tier, reason = router.route()

            if kind == "anthropic":
                base, headers, out = "https://" + ANTHROPIC_HOST, self._headers(), body
                router.bump("anthropic")
            else:
                base = tier["baseUrl"]
                headers = self._headers(tier)
                out = _rewrite_body(body, tier["model"])
                router.bump(tier["name"])

            try:
                conn, prefix = _connect(base)
                conn.request(self.command, prefix + self.path, body=out, headers=headers)
                resp = conn.getresponse()
            except (OSError, ssl.SSLError, socket.timeout) as e:
                return self._json({"type": "error", "error": {
                    "type": "api_error",
                    "message": f"dg proxy: upstream {base} unreachable ({type(e).__name__})"}},
                    502)

            if kind == "anthropic":
                router.record_quota(resp.headers)
            if os.environ.get("DG_PROXY_DEBUG"):
                _debug(f"{kind}/{(tier or {}).get('name','-')} -> {resp.status} "
                       f"ct={resp.getheader('content-type')} "
                       f"te={resp.getheader('transfer-encoding')} "
                       f"cl={resp.getheader('content-length')}")

            self.send_response(resp.status)
            streaming = False
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in ("connection", "keep-alive", "transfer-encoding", "content-length"):
                    if lk == "content-length":
                        self.send_header(k, v)
                    continue
                if lk == "content-type" and "event-stream" in v.lower():
                    streaming = True
                self.send_header(k, v)
            if streaming or resp.getheader("content-length") is None:
                self.send_header("transfer-encoding", "chunked")
            self.end_headers()

            first = b""
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    if not first:
                        first = chunk
                    if streaming or resp.getheader("content-length") is None:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                    else:
                        self.wfile.write(chunk)
                    self.wfile.flush()
                if streaming or resp.getheader("content-length") is None:
                    self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()
            if os.environ.get("DG_PROXY_DEBUG"):
                _debug(f"  first 200B: {first[:200]!r}")
            if kind == "anthropic":
                router.note_hard_limit(resp.status, first)

        def _headers(self, tier: dict | None = None) -> dict[str, str]:
            out = {k: v for k, v in self.headers.items() if k.lower() not in _DROP}
            if tier is None:
                # Anthropic: the client's own OAuth header passes through
                # verbatim. It is never read, logged or stored.
                out["Host"] = ANTHROPIC_HOST
                return out
            # A fallback tier gets its own key; the OAuth token stops here.
            for k in list(out):
                if k.lower() in ("authorization", "x-api-key", "anthropic-beta"):
                    out.pop(k)
            tok = launcher.tier_token(tier)
            if tok:
                out["Authorization"] = f"Bearer {tok}"
                out["x-api-key"] = tok
            out["anthropic-version"] = "2023-06-01"
            out["Host"] = urlsplit(tier["baseUrl"]).netloc
            return out

        def log_message(self, *a):
            pass  # never log request lines: they carry paths and tokens

        def handle_one_request(self):
            # A client that hangs up mid-request is normal (Ctrl-C, timeout);
            # it must not spray a stack trace over the proxy's log.
            try:
                super().handle_one_request()
            except (ConnectionResetError, BrokenPipeError, TimeoutError):
                self.close_connection = True

    return Handler


def serve(cfg: dict, port: int | None = None) -> int:
    port = port or cfg.get("proxy", {}).get("port", 8787)
    router = Router(cfg)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(router))
    srv.daemon_threads = True
    store.kv_set(store.connect(), "proxy", {"port": port, "startedAt": time.time()})
    print(f"dg proxy: listening on http://127.0.0.1:{port} (loopback only)", file=sys.stderr)
    print(f"dg proxy: set ANTHROPIC_BASE_URL=http://127.0.0.1:{port} for Claude Code",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def health(port: int, timeout: float = 2.0) -> dict[str, Any] | None:
    """Is a dg proxy already listening there?"""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/__dg/health", timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None
