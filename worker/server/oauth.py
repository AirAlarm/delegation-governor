"""Headless OAuth device-flow manager (spec 002/003 — Copilot for now).

litellm's own device flow (`Authenticator._login`) prints the verification
URL + user code to stdout, which is useless for a headless MCP server. We
split it: call `_get_device_code()` to capture {verification_uri, user_code}
and relay them to the supervisor, then run the blocking
`_poll_for_access_token()` on a daemon thread so the event loop stays free.

litellm is imported lazily INSIDE the authenticator factory, so this module
(and its tests) import cleanly without litellm installed — the test suite
monkeypatches the factory.

Security: device_code and access tokens never leave this module. Tool
responses only ever carry verification_uri, user_code, and an opaque flow_id.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable

# Provider key -> factory returning an object with the three methods we use:
#   _get_device_code() -> dict, _poll_for_access_token(device_code) -> str, get_api_key() -> str
# Tests replace entries here with stubs; litellm is imported only when a real
# factory runs.
def _github_copilot_authenticator() -> Any:
    from litellm.llms.github_copilot.authenticator import Authenticator

    return Authenticator()


class _ChatGPTDeviceFlowAdapter:
    """Adapts litellm's ChatGPT ``Authenticator`` to the two-method shape
    ``start_device_flow``/``_run_poll`` expect (matching github_copilot's
    Authenticator: ``_get_device_code()`` then ``_poll_for_access_token(...)``).

    The real ChatGPT authenticator is a THREE-step flow — request a device
    code, poll for an authorization code, exchange that code for tokens — and
    its own ``_login_device_code()`` convenience method prints the
    verification URL/code to stdout (useless headless, same problem
    github_copilot's ``_login()`` had). This adapter drives the three
    non-printing steps itself and persists the result to the same auth file
    ``get_access_token()``/``get_account_id()`` read from, so normal
    completions work immediately afterward — without changing the generic
    device-flow runner at all.
    """

    def __init__(self, authenticator: Any, verify_url: str) -> None:
        self._auth = authenticator
        self._verify_url = verify_url

    def _get_device_code(self) -> dict[str, Any]:
        device_code = self._auth._request_device_code()
        self._auth._record_device_code_request()
        return {
            "device_code": device_code,  # whole dict; round-tripped as-is to _poll_for_access_token
            "verification_uri": self._verify_url,
            "user_code": device_code["user_code"],
            "interval": device_code.get("interval"),
        }

    def _poll_for_access_token(self, device_code: dict[str, Any]) -> str:
        auth_code = self._auth._poll_for_authorization_code(device_code)
        tokens = self._auth._exchange_code_for_tokens(auth_code)
        auth_data = self._auth._build_auth_record(tokens)
        self._auth._write_auth_file(auth_data)
        return tokens["access_token"]


def _chatgpt_authenticator() -> Any:
    from litellm.llms.chatgpt.authenticator import Authenticator
    from litellm.llms.chatgpt.common_utils import CHATGPT_DEVICE_VERIFY_URL

    return _ChatGPTDeviceFlowAdapter(Authenticator(), CHATGPT_DEVICE_VERIFY_URL)


PROVIDER_AUTHENTICATORS: dict[str, Callable[[], Any]] = {
    "github_copilot": _github_copilot_authenticator,
    "chatgpt": _chatgpt_authenticator,
}

# Model-string substrings that map to an OAuth provider key. Kept in sync with
# config_store.OAUTH_CACHE_DIRS; only providers wired above are actionable.
_MODEL_PREFIX_TO_PROVIDER = {
    "github_copilot": "github_copilot",
    "chatgpt": "chatgpt",
}

_flows: dict[str, dict[str, Any]] = {}
_flows_lock = threading.Lock()


def provider_key_for_model(model: str) -> str | None:
    """Return the OAuth provider key implied by a model string, or None."""
    for prefix, provider in _MODEL_PREFIX_TO_PROVIDER.items():
        if prefix in model:
            return provider
    return None


def _run_poll(flow_id: str, authenticator: Any, device_code: str) -> None:
    """Background: block on litellm's poller, then warm the api-key cache."""
    try:
        authenticator._poll_for_access_token(device_code)
        # Mint/refresh the downstream key so the first real completion doesn't
        # have to; some providers key api access separately from the token.
        get_api_key = getattr(authenticator, "get_api_key", None)
        if callable(get_api_key):
            get_api_key()
        _set_status(flow_id, "authorized")
    except Exception as e:  # noqa: BLE001 - report, never crash the daemon
        _set_status(flow_id, "failed", error=f"{type(e).__name__}: {e}")


def _set_status(flow_id: str, status: str, error: str | None = None) -> None:
    with _flows_lock:
        flow = _flows.get(flow_id)
        if flow is None:
            return
        flow["status"] = status
        if error is not None:
            flow["error"] = error


def start_device_flow(provider: str) -> dict[str, Any]:
    """Begin a device flow. Returns relay-safe fields + an opaque flow_id.

    Raises ValueError for an unsupported provider. Any litellm/network error
    while requesting the device code propagates to the caller (main.py turns
    it into a clean error response).
    """
    factory = PROVIDER_AUTHENTICATORS.get(provider)
    if factory is None:
        supported = ", ".join(sorted(PROVIDER_AUTHENTICATORS)) or "(none)"
        raise ValueError(f"OAuth not supported for provider {provider!r}; supported: {supported}")

    authenticator = factory()
    device_info = authenticator._get_device_code()

    flow_id = secrets.token_hex(8)
    with _flows_lock:
        _flows[flow_id] = {
            "provider": provider,
            "status": "pending",
            "started_at": time.time(),
            "error": None,
        }

    thread = threading.Thread(
        target=_run_poll,
        args=(flow_id, authenticator, device_info["device_code"]),
        daemon=True,
        name=f"dg-worker-oauth-{flow_id}",
    )
    thread.start()

    return {
        "flow_id": flow_id,
        "verification_uri": device_info["verification_uri"],
        "user_code": device_info["user_code"],
        "expires_in": device_info.get("expires_in"),
        "interval": device_info.get("interval"),
    }


def poll_status(flow_id: str) -> dict[str, Any]:
    """Non-secret status for a flow: pending | authorized | failed."""
    with _flows_lock:
        flow = _flows.get(flow_id)
        if flow is None:
            return {"error": "unknown flow_id"}
        out = {"flow_id": flow_id, "status": flow["status"], "provider": flow["provider"]}
        if flow.get("error"):
            out["error"] = flow["error"]
        return out
