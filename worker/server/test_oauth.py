"""OAuth device-flow manager tests — stdlib only, litellm fully mocked."""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oauth


class StubAuthenticator:
    """Controllable stand-in for litellm's Authenticator.

    `release` gates `_poll_for_access_token` so the background thread's state
    transition is deterministic: the test sets the event, then observes the
    move to authorized/failed. Set `raise_on_poll` to exercise the failure path.
    """

    def __init__(self, raise_on_poll=False):
        self.release = threading.Event()
        self.raise_on_poll = raise_on_poll
        self.api_key_called = False

    def _get_device_code(self):
        return {
            "device_code": "SECRET-DEVICE-CODE",
            "user_code": "WXYZ-1234",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }

    def _poll_for_access_token(self, device_code):
        assert device_code == "SECRET-DEVICE-CODE"
        self.release.wait(timeout=5)
        if self.raise_on_poll:
            raise RuntimeError("user denied")
        return "SECRET-ACCESS-TOKEN"

    def get_api_key(self):
        self.api_key_called = True
        return "SECRET-API-KEY"


class OAuthTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_factories = dict(oauth.PROVIDER_AUTHENTICATORS)
        oauth._flows.clear()

    def tearDown(self):
        oauth.PROVIDER_AUTHENTICATORS.clear()
        oauth.PROVIDER_AUTHENTICATORS.update(self._saved_factories)
        oauth._flows.clear()

    def _install(self, stub):
        oauth.PROVIDER_AUTHENTICATORS["github_copilot"] = lambda: stub

    def _wait_status(self, flow_id, target, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if oauth.poll_status(flow_id)["status"] == target:
                return True
            time.sleep(0.02)
        return False


class TestProviderMapping(OAuthTestCase):
    def test_maps_known_prefixes(self):
        self.assertEqual(oauth.provider_key_for_model("litellm:github_copilot/gpt-5"), "github_copilot")
        self.assertEqual(oauth.provider_key_for_model("litellm:chatgpt/gpt-5.3-codex"), "chatgpt")

    def test_unknown_model_returns_none(self):
        self.assertIsNone(oauth.provider_key_for_model("litellm:minimax/MiniMax-M3"))


class TestStartFlow(OAuthTestCase):
    def test_unsupported_provider_raises(self):
        with self.assertRaises(ValueError):
            oauth.start_device_flow("nope")

    def test_returns_relay_fields_only(self):
        stub = StubAuthenticator()
        self._install(stub)
        info = oauth.start_device_flow("github_copilot")
        self.assertEqual(info["verification_uri"], "https://github.com/login/device")
        self.assertEqual(info["user_code"], "WXYZ-1234")
        self.assertIn("flow_id", info)
        # The secret device code must never be relayed.
        self.assertNotIn("device_code", info)
        self.assertNotIn("SECRET-DEVICE-CODE", str(info))
        stub.release.set()  # let the daemon finish cleanly

    def test_pending_then_authorized(self):
        stub = StubAuthenticator()
        self._install(stub)
        info = oauth.start_device_flow("github_copilot")
        fid = info["flow_id"]
        self.assertEqual(oauth.poll_status(fid)["status"], "pending")
        stub.release.set()
        self.assertTrue(self._wait_status(fid, "authorized"))
        self.assertTrue(stub.api_key_called)

    def test_pending_then_failed(self):
        stub = StubAuthenticator(raise_on_poll=True)
        self._install(stub)
        info = oauth.start_device_flow("github_copilot")
        fid = info["flow_id"]
        stub.release.set()
        self.assertTrue(self._wait_status(fid, "failed"))
        status = oauth.poll_status(fid)
        self.assertIn("user denied", status.get("error", ""))


class TestPollStatus(OAuthTestCase):
    def test_unknown_flow_id(self):
        self.assertEqual(oauth.poll_status("deadbeef")["error"], "unknown flow_id")


class StubChatGPTInnerAuthenticator:
    """Mimics litellm's real ChatGPT Authenticator's private method surface
    (a 3-step flow: request device code -> poll for an authorization code ->
    exchange the code for tokens), so _ChatGPTDeviceFlowAdapter is exercised
    without importing real litellm."""

    def __init__(self):
        self.written_auth_data = None
        self.recorded_request = False

    def _request_device_code(self):
        return {"device_auth_id": "DAI-1", "user_code": "ABCD-1234", "interval": "5"}

    def _record_device_code_request(self):
        self.recorded_request = True

    def _poll_for_authorization_code(self, device_code):
        assert device_code == {"device_auth_id": "DAI-1", "user_code": "ABCD-1234", "interval": "5"}
        return {"authorization_code": "AC-1", "code_challenge": "x", "code_verifier": "y"}

    def _exchange_code_for_tokens(self, auth_code):
        assert auth_code["authorization_code"] == "AC-1"
        return {"access_token": "AT-1", "refresh_token": "RT-1", "id_token": "IDT-1"}

    def _build_auth_record(self, tokens):
        return {"access_token": tokens["access_token"], "built": True}

    def _write_auth_file(self, auth_data):
        self.written_auth_data = auth_data


class TestChatGPTDeviceFlowAdapter(OAuthTestCase):
    def test_get_device_code_shape(self):
        inner = StubChatGPTInnerAuthenticator()
        adapter = oauth._ChatGPTDeviceFlowAdapter(inner, "https://chatgpt.com/codex/device")
        info = adapter._get_device_code()
        self.assertEqual(info["verification_uri"], "https://chatgpt.com/codex/device")
        self.assertEqual(info["user_code"], "ABCD-1234")
        self.assertTrue(inner.recorded_request)
        self.assertEqual(info["device_code"]["device_auth_id"], "DAI-1")  # round-trips whole dict

    def test_poll_for_access_token_chains_all_three_steps_and_persists(self):
        inner = StubChatGPTInnerAuthenticator()
        adapter = oauth._ChatGPTDeviceFlowAdapter(inner, "https://x")
        device_code = adapter._get_device_code()["device_code"]
        token = adapter._poll_for_access_token(device_code)
        self.assertEqual(token, "AT-1")
        self.assertEqual(inner.written_auth_data, {"access_token": "AT-1", "built": True})

    def test_full_flow_via_start_device_flow(self):
        inner = StubChatGPTInnerAuthenticator()
        oauth.PROVIDER_AUTHENTICATORS["chatgpt"] = lambda: oauth._ChatGPTDeviceFlowAdapter(inner, "https://x")
        info = oauth.start_device_flow("chatgpt")
        self.assertEqual(info["user_code"], "ABCD-1234")
        self.assertNotIn("device_code", info)  # never relayed, same guarantee as github_copilot
        self.assertTrue(self._wait_status(info["flow_id"], "authorized"))
        self.assertEqual(inner.written_auth_data["access_token"], "AT-1")


if __name__ == "__main__":
    unittest.main()
