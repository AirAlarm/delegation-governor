"""Router proxy: routing, model rewriting, credential handling, quota capture.

No real backend is contacted -- a fake upstream stands in for both Anthropic
and a fallback tier.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from base import DGTest

from dg import launcher, proxy, store, supervisor


class FakeUpstream:
    """Records what the proxy forwarded and replies like an Anthropic endpoint."""

    def __init__(self, headers=None, status=200, stream=False):
        self.seen: list[dict] = []
        self.extra_headers = headers or {}
        self.status = status
        self.stream = stream
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                outer.seen.append({
                    "path": self.path, "body": body,
                    "authorization": self.headers.get("authorization"),
                    "x-api-key": self.headers.get("x-api-key"),
                    "anthropic-beta": self.headers.get("anthropic-beta"),
                    "host": self.headers.get("host"),
                })
                if outer.stream:
                    payload = (b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
                    self.send_response(outer.status)
                    self.send_header("content-type", "text/event-stream")
                    for k, v in outer.extra_headers.items():
                        self.send_header(k, v)
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = json.dumps({
                    "id": "msg_fake", "type": "message", "role": "assistant",
                    "model": body.get("model"), "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 1,
                                                         "output_tokens": 1}}).encode()
                self.send_response(outer.status)
                self.send_header("content-type", "application/json")
                for k, v in outer.extra_headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


class ProxyTest(DGTest):
    def setUp(self):
        super().setUp()
        self.upstreams: list[FakeUpstream] = []

    def tearDown(self):
        for u in self.upstreams:
            u.stop()
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()
        super().tearDown()

    def upstream(self, **kw):
        u = FakeUpstream(**kw)
        self.upstreams.append(u)
        return u

    def start_proxy(self, cfg):
        router = proxy.Router(cfg, reload=False)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy.make_handler(router))
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return router

    def post(self, body=None, headers=None, path="/v1/messages"):
        data = json.dumps(body or {
            "model": "claude-sonnet-4-6", "max_tokens": 4,
            "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"content-type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    def quota(self, five=None, seven=None):
        rl = {}
        if five is not None:
            rl["five_hour"] = {"utilization": five / 100.0,
                               "resets_at": int(time.time()) + 3600}
        if seven is not None:
            rl["seven_day"] = {"utilization": seven / 100.0,
                               "resets_at": int(time.time()) + 86400}
        supervisor.ingest_statusline(self.con, {"rate_limits": rl})


class TestRouting(ProxyTest):
    def test_normal_quota_routes_to_anthropic(self):
        self.quota(10, 10)
        router = proxy.Router(self.cfg, reload=False)
        kind, tier, _ = router.route()
        self.assertEqual(kind, "anthropic")
        self.assertIsNone(tier)

    def test_exhausted_quota_goes_to_a_tier(self):
        up = self.upstream()
        self.cfg["supervisorFallbacks"] = [
            {"name": "lmstudio", "kind": "remote", "baseUrl": up.url,
             "model": "local/main", "smallModel": "local/small"}]
        launcher.probe_tier = lambda t, load=False: (True, "fake ok")
        self.quota(10, 10)
        supervisor.record_hard_limit(self.con, "rate_limit")
        router = self.start_proxy(self.cfg)
        self.post()
        self.assertEqual(router.stats.get("lmstudio"), 1)
        self.assertEqual(up.seen[0]["body"]["model"], "local/main",
                         "the tier's own model must replace the requested one")

    def test_tier_gets_its_own_key_not_the_oauth_token(self):
        """The subscription token must never leave the machine for a fallback."""
        up = self.upstream()
        os.environ["DG_TEST_TIER_KEY"] = "tier-secret"
        self.addCleanup(os.environ.pop, "DG_TEST_TIER_KEY", None)
        self.cfg["supervisorFallbacks"] = [
            {"name": "lmstudio", "kind": "remote", "baseUrl": up.url,
             "model": "local/main", "tokenEnvVar": "DG_TEST_TIER_KEY"}]
        launcher.probe_tier = lambda t, load=False: (True, "fake ok")
        supervisor.record_hard_limit(self.con, "rate_limit")
        self.start_proxy(self.cfg)
        self.post(headers={"authorization": "Bearer sk-ant-oat01-SECRET",
                           "anthropic-beta": "oauth-2025-04-20"})
        seen = up.seen[0]
        self.assertNotIn("SECRET", json.dumps(seen))
        self.assertEqual(seen["authorization"], "Bearer tier-secret")
        self.assertIsNone(seen["anthropic-beta"], "oauth beta flags must be stripped")

    def test_no_configured_tier_does_not_retry_exhausted_anthropic(self):
        self.cfg["supervisorFallbacks"] = []
        supervisor.record_hard_limit(self.con, "rate_limit")
        router = proxy.Router(self.cfg, reload=False)
        kind, tier, reason = router.route()
        self.assertEqual(kind, "tier")
        self.assertIsNone(tier)
        self.assertIn("no fallback tier", reason)

    def test_failed_tier_is_cooled_down(self):
        supervisor.record_hard_limit(self.con, "rate_limit")
        router = proxy.Router(self.cfg, reload=False)
        tier = self.cfg["supervisorFallbacks"][0]
        router.cooldown(tier, "down")
        self.assertNotIn(tier, router.fallbacks())


class TestQuotaCapture(ProxyTest):
    """Anthropic's rate-limit headers are what restore SAVE mode in Desktop."""

    def test_headers_become_supervisor_quota(self):
        router = proxy.Router(self.cfg, reload=False)
        router.record_quota({
            "anthropic-ratelimit-unified-5h-utilization": "0.83",
            "anthropic-ratelimit-unified-5h-reset": "1788640200",
            "anthropic-ratelimit-unified-7d-utilization": "0.55",
            "anthropic-ratelimit-unified-7d-reset": "1789102800"})
        out = supervisor.evaluate(self.con, self.cfg)
        self.assertAlmostEqual(out["fiveHour"]["usedPercent"], 83.0)
        self.assertAlmostEqual(out["sevenDay"]["usedPercent"], 55.0)
        self.assertEqual(out["state"], supervisor.SAVE)

    def test_missing_headers_change_nothing(self):
        router = proxy.Router(self.cfg, reload=False)
        router.record_quota({})
        self.assertIsNone(supervisor.evaluate(self.con, self.cfg)["fiveHour"])

    def test_garbage_headers_are_ignored(self):
        router = proxy.Router(self.cfg, reload=False)
        router.record_quota({"anthropic-ratelimit-unified-5h-utilization": "not-a-number"})
        self.assertIsNone(supervisor.evaluate(self.con, self.cfg)["fiveHour"])

    def test_429_records_the_hard_limit(self):
        router = proxy.Router(self.cfg, reload=False)
        router.note_hard_limit(429, b'{"error":{"message":"rate limited"}}')
        self.assertEqual(supervisor.evaluate(self.con, self.cfg)["state"], supervisor.LOCAL)

    def test_200_does_not_record_a_hard_limit(self):
        router = proxy.Router(self.cfg, reload=False)
        router.note_hard_limit(200, b"{}")
        self.assertIsNone(store.kv_get(self.con, supervisor.K_HARD))


class TestFailover(ProxyTest):
    def test_qwen_failure_retries_oracle_in_same_request(self):
        first, second = self.upstream(status=500), self.upstream(status=200)
        self.cfg["supervisorFallbacks"] = [
            {"name": "qwen", "kind": "remote", "baseUrl": first.url, "model": "qwen"},
            {"name": "oracle", "kind": "remote", "baseUrl": second.url, "model": "oracle"},
        ]
        supervisor.record_hard_limit(self.con, "rate_limit")
        router = self.start_proxy(self.cfg)
        status, _ = self.post(path="/client/desktop/v1/messages")
        self.assertEqual(status, 200)
        self.assertEqual(len(first.seen), 1)
        self.assertEqual(len(second.seen), 1)
        self.assertEqual(second.seen[0]["path"], "/v1/messages")
        self.assertEqual(router.last["client"], "desktop")
        self.assertEqual(router.last["tier"], "oracle")


class TestBodyRewrite(DGTest):
    def test_model_is_replaced(self):
        out = proxy._rewrite_body(b'{"model":"claude-sonnet-4-6","max_tokens":1}', "local/x")
        self.assertEqual(json.loads(out)["model"], "local/x")

    def test_other_fields_survive(self):
        out = proxy._rewrite_body(
            b'{"model":"a","stream":true,"messages":[{"role":"user","content":"hi"}]}', "b")
        got = json.loads(out)
        self.assertTrue(got["stream"])
        self.assertEqual(got["messages"][0]["content"], "hi")

    def test_non_json_is_passed_through(self):
        self.assertEqual(proxy._rewrite_body(b"not json", "x"), b"not json")

    def test_empty_body_is_passed_through(self):
        self.assertEqual(proxy._rewrite_body(b"", "x"), b"")


class TestHopByHop(DGTest):
    def test_hop_by_hop_headers_are_not_forwarded(self):
        for h in ("connection", "transfer-encoding", "keep-alive", "host", "content-length"):
            self.assertIn(h, proxy._DROP)


