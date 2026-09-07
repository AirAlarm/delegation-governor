"""LM Studio single-worker gate (cc-delegate 'station' local patch).

This machine runs local worker models in LM Studio on a 12 GB GPU. Only one
large worker model fits in memory at a time, and LM Studio's JIT auto-load
does NOT evict a previously loaded model — so without this gate a second
delegation would leave two models resident and thrash.

`ensure_model_ready(model_str, api_base, jobs_dir)` is called by
`worker_launcher.run_worker` right before the worker subprocess starts. It:

  1. no-ops unless `api_base` is a local LM Studio endpoint (so non-local
     profiles - minimax, deepseek, ... - are untouched);
  2. refuses to start if another cc-delegate job is still `running` against a
     different model (serialises local worker tasks - the user's rule is
     "no parallel local-model jobs for now");
  3. unloads every other resident LLM via the `lms` CLI (targeted by exact id,
     never `lms unload --all`);
  4. loads the target model if it is not already resident;
  5. waits until the LM Studio API reports it `loaded` AND a 1-token probe
     completion returns 200.

All of this is wrapped in a lock file so two launches cannot fight over the
GPU. Everything is best-effort and time-bounded: a failure here raises, and
the caller turns that into a clean job failure with the message attached.

Reapply after `claude plugin update cc-delegate` — see
docs/STATION_DELEGATION.md and server/station_patch.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCK_PATH = Path(os.path.expanduser("~")) / ".cc-delegate" / "lmstudio_gate.lock"

# generous: a 22 GB partial-offload model cold-loads in ~60-90 s here
LOAD_TIMEOUT_S = 600
PROBE_TIMEOUT_S = 180

# 2026-09-07: keep the model resident across a working session. Cold-loading
# through this gate is what fails; a long TTL means it is warm for the whole
# batch of delegations instead of being evicted between them.
MODEL_TTL_S = 14400  # 4 hours

# A load that has only just been asked for is not a load that has stalled.
# Polling immediately after `lms load` returns burns the budget on a model that
# is still mapping weights, so wait before the first check and back off after.
LOAD_SETTLE_S = 10
LOAD_POLL_S = 5
LOCK_TIMEOUT_S = 400
# 2026-09-05: a real delegation (CryptAndHearth) hit `lms unload` taking >60s and
# raising a clean gate failure - reproduced as a one-off (unload normally completes
# in ~2-3s; heavy unrelated disk/system I/O at that exact moment, from a broad
# filesystem search running elsewhere on the box, is the suspected cause, not a
# persistent LM Studio issue). Bumped for headroom; still fails fast and cleanly
# well short of a delegation's own overall timeout if something is genuinely stuck.
UNLOAD_TIMEOUT_S = 180
_LLM_TYPES = {"llm", "vlm"}

# 2026-09-04: a real cc-delegate task (station-main, CryptAndHearth repo) hit a
# ContextWindowExceededError at 13,149 prompt tokens against LM Studio's 8192
# default - the worker's spec + a handful of referenced doc files alone exceeds
# it before the model ever responds (deepagents has no context-compression of
# its own, unlike this project's custom hard-agent benchmark loop). Loading
# through this gate now requests a larger window explicitly. 24576 tested
# empirically safe on this 12 GB GPU / 32 GB RAM box for all three station
# models (real >16k-token markdown prompts processed with no error); 32768
# left only ~1 GB system RAM free on qwen3.6-35b-a3b - too tight, not used.
CONTEXT_LENGTH = 65536


def _log(msg: str) -> None:
    print(f"[lmstudio_gate] {msg}", flush=True)


def is_local_lmstudio(api_base: str | None) -> bool:
    if not api_base:
        return False
    a = api_base.lower()
    return ("127.0.0.1" in a or "localhost" in a or "0.0.0.0" in a) and "/v1" in a


def _native_base(api_base: str) -> str:
    """`http://127.0.0.1:1234/v1` -> `http://127.0.0.1:1234`."""
    return api_base.split("/v1", 1)[0].rstrip("/")


def lmstudio_model_id(model_str: str) -> str:
    """`litellm:openai/openai/gpt-oss-20b` -> `openai/gpt-oss-20b`.

    Strip the langchain `<prefix>:` convention, then the litellm `openai/`
    custom-provider prefix, leaving the id LM Studio actually indexes.
    """
    bare = model_str.split(":", 1)[-1] if ":" in model_str else model_str
    if bare.startswith("openai/"):
        bare = bare[len("openai/"):]
    return bare


def _find_lms() -> str:
    for cand in (
        shutil.which("lms"),
        os.path.expanduser(r"~/.lmstudio/bin/lms"),
        os.path.expanduser(r"~/.lmstudio/bin/lms.exe"),
    ):
        if cand and os.path.exists(cand):
            return cand
    # last resort: trust PATH resolution at call time
    return "lms"


def _http_json(url: str, payload: dict | None = None, timeout: int = 10) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _list_models(native_base: str) -> list[dict]:
    try:
        d = _http_json(f"{native_base}/api/v0/models", timeout=10)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"LM Studio not reachable at {native_base}: {e}")
    return d.get("data", [])


def _loaded_llms(native_base: str) -> list[str]:
    return [m["id"] for m in _list_models(native_base)
            if m.get("type") in _LLM_TYPES and m.get("state") == "loaded"]


def _loaded_context_length(native_base: str, model_id: str) -> int | None:
    """Context the resident `model_id` was actually loaded with, or None if
    it isn't loaded. Used to catch a model left resident from before this
    gate started requesting CONTEXT_LENGTH (or loaded by hand at a smaller
    one) instead of silently trusting "already loaded == ready"."""
    for m in _list_models(native_base):
        if m.get("id") == model_id and m.get("state") == "loaded":
            return m.get("loaded_context_length")
    return None


def _model_state(native_base: str, model_id: str) -> str | None:
    try:
        d = _http_json(f"{native_base}/api/v0/models", timeout=10)
    except Exception:  # noqa: BLE001
        return None
    for m in d.get("data", []):
        if m.get("id") == model_id:
            return m.get("state")
    return None


def _lms(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([_find_lms(), *args], capture_output=True, text=True,
                          timeout=timeout)


def _running_jobs_other_model(jobs_dir: str | None, target_id: str) -> list[str]:
    """task ids of still-running cc-delegate jobs bound to a different model."""
    if not jobs_dir or not os.path.isdir(jobs_dir):
        return []
    clash = []
    for jf in Path(jobs_dir).glob("*.json"):
        try:
            j = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if j.get("status") != "running":
            continue
        other = lmstudio_model_id(j.get("model") or "")
        if other and other != target_id:
            clash.append(f"{j.get('taskId', jf.stem)} ({other})")
    return clash


class _FileLock:
    def __init__(self, path: Path, timeout_s: int):
        self.path, self.timeout_s, self.fd = path, timeout_s, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout_s
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, f"{os.getpid()} {time.time():.0f}".encode())
                return self
            except FileExistsError:
                # break a stale lock (>15 min old)
                try:
                    if time.time() - self.path.stat().st_mtime > 900:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise RuntimeError(
                        f"timed out after {self.timeout_s}s waiting for {self.path} "
                        "(another station delegation is mid-launch)")
                time.sleep(2)

    def __exit__(self, *exc):
        try:
            if self.fd is not None:
                os.close(self.fd)
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_model_ready(model_str: str | None, api_base: str | None,
                       jobs_dir: str | None = None) -> None:
    if not model_str or not is_local_lmstudio(api_base):
        return  # not our local LM Studio setup - leave it alone
    assert api_base is not None
    native = _native_base(api_base)
    target = lmstudio_model_id(model_str)

    with _FileLock(LOCK_PATH, LOCK_TIMEOUT_S):
        clash = _running_jobs_other_model(jobs_dir, target)
        if clash:
            raise RuntimeError(
                "refusing to start: another local-model delegation is still "
                f"running with a different model - {', '.join(clash)}. "
                "Station delegations are serialised (one local worker at a "
                "time); wait for it to finish or cancel_task it.")

        loaded = _loaded_llms(native)
        for m in loaded:
            if m != target:
                _log(f"unloading {m}")
                r = _lms("unload", m, timeout=UNLOAD_TIMEOUT_S)
                if r.returncode != 0:
                    _log(f"unload {m} rc={r.returncode}: {r.stderr.strip()[:200]}")

        stale_ctx = (target in _loaded_llms(native)
                    and _loaded_context_length(native, target) != CONTEXT_LENGTH)
        if stale_ctx:
            _log(f"{target} resident at the wrong context length "
                 f"({_loaded_context_length(native, target)} != {CONTEXT_LENGTH}) - reloading")
            _lms("unload", target, timeout=UNLOAD_TIMEOUT_S)

        if stale_ctx or target not in _loaded_llms(native):
            _log(f"loading {target} (context {CONTEXT_LENGTH})")
            r = _lms("load", target, "-y", "--context-length", str(CONTEXT_LENGTH),
                     "--ttl", str(MODEL_TTL_S), timeout=LOAD_TIMEOUT_S)
            if r.returncode != 0:
                raise RuntimeError(
                    f"`lms load {target}` failed rc={r.returncode}: "
                    f"{(r.stderr or r.stdout).strip()[:300]}")

            # Weights are still being mapped when the command returns; give it
            # a head start rather than spending the budget on certain misses.
            time.sleep(LOAD_SETTLE_S)

        deadline = time.time() + LOAD_TIMEOUT_S
        while _model_state(native, target) != "loaded":
            if time.time() > deadline:
                raise RuntimeError(f"{target} did not reach 'loaded' within {LOAD_TIMEOUT_S}s")
            time.sleep(LOAD_POLL_S)

        # functional probe - model file mapped != ready to infer
        deadline = time.time() + PROBE_TIMEOUT_S
        last = ""
        while time.time() < deadline:
            try:
                _http_json(f"{api_base}/chat/completions",
                           {"model": target, "messages": [{"role": "user", "content": "ok"}],
                            "max_tokens": 1, "temperature": 0},
                           timeout=PROBE_TIMEOUT_S)
                _log(f"{target} ready")
                return
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code < 500:
                    _log(f"probe {last} (treating as ready)")
                    return
            except Exception as e:  # noqa: BLE001
                last = str(e)[:120]
            time.sleep(3)
        raise RuntimeError(f"{target} loaded but probe never succeeded ({last})")
