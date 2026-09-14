"""Run the whole run 2 procedure (RUNBOOK "Run 2 steps" 0-6) unattended. Uses no Claude tokens itself.

Usage: python3 autorun.py            (log: results/run2/autorun.log)

Differences from the manual procedure, all identical for both arms:
- sessions run under `expect` (drive.exp) in a pty, with a clean login-shell env like a user terminal;
- bracket readings run from ~/Projects/rin-website (already trusted) instead of this folder;
- an arm is "finished" when its transcript ends on an end_turn, no dg task runs, a commit exists in
  rin-website and the transcript has been quiet for 3 min (or 20 min without a commit, or a 75 min cap).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = Path.home() / "Projects" / "rin-website"
TRANSCRIPTS = Path.home() / ".claude" / "projects" / "-Users-georgiy-Projects-rin-website"
OUT = HERE / "results" / "run2"
FLAGS = OUT / ".flags"
IDS = {"R0": "aa95fbc3-a809-4471-9c43-6f8dd4460fa4", "A2": "6a016168-8d57-408d-abc9-8fe293fa8e4f",
       "R1": "b3a086d2-524a-4377-8429-112a4397acf0", "B2": "1ecfb531-6c9e-42e9-8eb9-452364ca0fbd",
       "R2": "ce41867d-f008-40d4-8f34-7e25869cea76"}
MODEL = ["--model", "claude-opus-5"]
PROMPT = lambda arm: (HERE / "task.md").read_text() + (HERE / f"arm-{arm}.md").read_text()
ARM_CAP = 75 * 60


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with (OUT / "autorun.log").open("a") as f:
        f.write(line + "\n")


def sh(*cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=HERE, **kw)


def usage_rows(sid: str) -> list[dict]:
    rows = []
    for line in (HERE / "usage.jsonl").open():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("session_id") == sid:
            rows.append(r)
    return rows


def last_turn(sid: str) -> tuple[dict | None, float]:
    """Last user/assistant transcript entry and seconds since the transcript was written."""
    path = TRANSCRIPTS / f"{sid}.jsonl"
    if not path.exists():
        return None, 0.0
    last = None
    for line in path.open():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") in ("user", "assistant"):
            last = d
    return last, time.time() - path.stat().st_mtime


def drive(sid: str, args: list[str], done) -> None:
    flag = FLAGS / sid
    env = {k: os.environ[k] for k in ("HOME", "USER", "LOGNAME", "LANG") if k in os.environ}
    env |= {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "xterm-256color", "SHELL": "/bin/zsh"}
    cmd = ["expect", str(HERE / "drive.exp"), str(SITE), str(flag),
           "/bin/zsh", "-lic", 'exec claude "$@"', "zsh", "--session-id", sid, *args]
    t0 = time.time()
    log(f"start session {sid}")
    p = subprocess.Popen(cmd, env=env, stdout=(OUT / f"drive-{sid[:8]}.out").open("w"), stderr=subprocess.STDOUT)
    while p.poll() is None:
        time.sleep(10)
        if done(sid, t0):
            flag.touch()
            break
    try:
        p.wait(timeout=180)
    except subprocess.TimeoutExpired:
        p.kill()
        log(f"WARNING: had to kill driver for {sid}")
    log(f"end session {sid} after {(time.time() - t0) / 60:.1f} min")


def reading_done(sid: str, t0: float) -> bool:
    last, idle = last_turn(sid)
    ok = (last and last["type"] == "assistant" and last["message"].get("stop_reason") == "end_turn"
          and any(r.get("rate_limits") for r in usage_rows(sid)) and idle > 5)
    if not ok and time.time() - t0 > 300:
        log(f"FAIL: reading {sid} got no rate_limits in 5 min")
        return True
    return bool(ok)


def arm_done(sid: str, t0: float) -> bool:
    elapsed = time.time() - t0
    if elapsed > ARM_CAP:
        log(f"WARNING: arm {sid} hit the {ARM_CAP // 60} min cap")
        return True
    last, idle = last_turn(sid)
    if not (last and last["type"] == "assistant" and last["message"].get("stop_reason") == "end_turn"):
        return False
    if "no tasks" not in sh("dg", "tasks", "running").stdout:
        return False
    rows = usage_rows(sid)
    if not rows or rows[-1]["ts"] < (TRANSCRIPTS / f"{sid}.jsonl").stat().st_mtime - 2:
        return False  # statusline hasn't rendered after the last turn yet
    committed = sh("git", "-C", str(SITE), "rev-parse", "HEAD").returncode == 0
    if committed and idle >= 180:
        return True
    if idle >= 1200:
        log(f"WARNING: arm {sid} idle 20 min without a commit")
        return True
    return False


def reading(label: str, sid: str) -> dict:
    log(sh("./snapshot.sh", f"run2-{label}").stdout.splitlines()[0][:200])
    drive(sid, [*MODEL, "--settings", str(HERE / "arm-b.settings.json"), "Reply with OK."], reading_done)
    rl = next((r["rate_limits"] for r in usage_rows(sid) if r.get("rate_limits")), None)
    log(f"reading {label} ({sid}): {json.dumps(rl)}")
    if not rl:
        sys.exit(f"reading {label} has no rate_limits; stopping")
    return rl


def arm(name: str, letter: str, sid: str) -> None:
    log(sh("./snapshot.sh", f"arm-{name}-start").stdout.splitlines()[0][:200])
    drive(sid, [*MODEL, "--effort", "high", "--settings", str(HERE / f"arm-{letter}.settings.json"),
                PROMPT(letter)], arm_done)
    log(sh("./snapshot.sh", f"arm-{name}-end").stdout.splitlines()[0][:200])
    check = sh("python3", "check_site.py", str(SITE)).stdout
    (OUT / f"arm-{letter}-check.txt").write_text(check)
    log(f"check arm {letter}: {check.strip().splitlines()[-1] if check.strip() else 'no output'}")
    log(sh("./reset.sh", name).stdout.strip().replace("\n", " | "))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    FLAGS.mkdir(exist_ok=True)
    assert "no tasks" in sh("dg", "tasks", "running").stdout, "dg tasks are running"
    assert {p.name for p in SITE.iterdir()} <= {"assets", ".DS_Store"}, "rin-website is not clean"
    for a in ("arm-a2", "arm-b2"):
        assert not (SITE.parent / "rin-website-archive" / a).exists(), f"archive {a} exists"
    ids = dict(IDS)

    rl = reading("R0", ids["R0"])
    left = rl["five_hour"]["resets_at"] - time.time()
    if left < 120 * 60:
        log(f"5h window resets in {left / 60:.0f} min, too close; waiting for the reset")
        time.sleep(left + 180)
        ids["R0"] = str(uuid.uuid4())
        reading("R0", ids["R0"])
    (OUT / "session-ids.json").write_text(json.dumps(ids, indent=2) + "\n")

    arm("a2", "a", ids["A2"])
    time.sleep(120)
    reading("R1", ids["R1"])
    arm("b2", "b", ids["B2"])
    time.sleep(120)
    reading("R2", ids["R2"])

    for letter, sid, before, after in (("a", ids["A2"], ids["R0"], ids["R1"]), ("b", ids["B2"], ids["R1"], ids["R2"])):
        r = sh("python3", "measure.py", sid, "--before", before, "--after", after)
        (OUT / f"arm-{letter}.json").write_text(r.stdout or r.stderr)
        bw = json.loads(r.stdout).get("bracketed_windows", {}) if r.stdout else {}
        log(f"measure arm {letter}: 5h {bw.get('five_hour')} 7d {bw.get('seven_day')}")
    log("run 2 done")


if __name__ == "__main__":
    main()
