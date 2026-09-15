"""Run an A/B round unattended. Uses no Claude tokens itself.

Usage: python3 autorun.py            run 2 (RUNBOOK "Run 2 steps" 0-6; log: results/run2/autorun.log)
       python3 autorun.py rerun-a    run 2 arm A re-run
       python3 autorun.py round3     round 3 (RUNBOOK "Round 3"; log: results/run3/autorun.log)

Differences from the manual procedure, all identical for both arms:
- sessions run under `expect` (drive.exp) in a pty, with a clean login-shell env like a user terminal;
- bracket readings run from ~/Projects/rin-website (already trusted) instead of this folder;
- an arm is "finished" when its transcript ends on an end_turn, no dg task runs, a commit exists in
  rin-website and the transcript has been quiet for 3 min (or 20 min without a commit, or a 110 min cap).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
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
TASK, CHECK, RUN = "task.md", "check_site.py", "run2"
PROMPT = lambda arm: (HERE / TASK).read_text() + (HERE / f"arm-{arm}.md").read_text()
ARM_CAP = 110 * 60
PROJECTS = Path.home() / ".claude" / "projects"


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


def pending_background(sid: str) -> set[str]:
    """Background task ids the arm started whose completion notification it hasn't consumed yet."""
    started, consumed = set(), set()
    for line in (TRANSCRIPTS / f"{sid}.jsonl").open():
        started |= set(re.findall(r"running in background with ID: (\w+)", line))
        # a delivered <task-notification> becomes a user message or a queued_command attachment
        if '"type":"user"' in line or '"queued_command"' in line:
            consumed |= set(re.findall(r"<task-id>(\w+)</task-id>", line))
    return started - consumed


_dg_busy = {"at": 0.0}


def arm_done(sid: str, t0: float) -> bool:
    elapsed = time.time() - t0
    if elapsed > ARM_CAP:
        log(f"WARNING: arm {sid} hit the {ARM_CAP // 60} min cap")
        return True
    # Run 2's first arm A was stopped in the gap between its Codex task finishing and its own
    # watcher waking it, so both dg and the arm's background tasks must have been quiet for 3 min.
    if "no tasks" not in sh("dg", "tasks", "running").stdout:
        _dg_busy["at"] = time.time()
        return False
    last, idle = last_turn(sid)
    if not (last and last["type"] == "assistant" and last["message"].get("stop_reason") == "end_turn"):
        return False
    if time.time() - _dg_busy["at"] < 180 or pending_background(sid):
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
    log(sh("./snapshot.sh", f"{RUN}-{label}").stdout.splitlines()[0][:200])
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
    check = sh("python3", CHECK, str(SITE)).stdout
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


def rerun_a() -> None:
    """Re-run arm A alone with fresh ids and its own bracket (R3 before, R4 after), after a 5 h reset if close."""
    assert "no tasks" in sh("dg", "tasks", "running").stdout, "dg tasks are running"
    assert {p.name for p in SITE.iterdir()} <= {"assets", ".DS_Store"}, "rin-website is not clean"
    ids = json.loads((OUT / "session-ids.json").read_text())
    ids |= {"R3": str(uuid.uuid4()), "A2r": str(uuid.uuid4()), "R4": str(uuid.uuid4())}
    rl = reading("R3", ids["R3"])
    left = rl["five_hour"]["resets_at"] - time.time()
    if left < 100 * 60:
        log(f"5h window resets in {left / 60:.0f} min, too close; waiting for the reset")
        time.sleep(left + 180)
        ids["R3"] = str(uuid.uuid4())
        reading("R3", ids["R3"])
    (OUT / "session-ids.json").write_text(json.dumps(ids, indent=2) + "\n")
    arm("arm-a2r", "a", ids["A2r"])
    time.sleep(120)
    reading("R4", ids["R4"])
    r = sh("python3", "measure.py", ids["A2r"], "--before", ids["R3"], "--after", ids["R4"])
    (OUT / "arm-a.json").write_text(r.stdout or r.stderr)
    bw = json.loads(r.stdout).get("bracketed_windows", {}) if r.stdout else {}
    log(f"measure arm a (rerun): 5h {bw.get('five_hour')} 7d {bw.get('seven_day')}")
    log("arm A rerun done")


def other_claude_activity(since: float, own: set[str]) -> dict[str, int]:
    """Assistant turns logged since `since` by any transcript not in `own` (session ids).

    Run 2's arm A2r shared its 5 h window with a desktop session nobody noticed, so
    every arm is checked against every transcript on the machine.
    """
    hits: dict[str, int] = {}
    for path in PROJECTS.rglob("*.jsonl"):
        if path.stat().st_mtime < since or any(s in str(path) for s in own):
            continue
        n = 0
        for line in path.open(errors="replace"):
            if '"type":"assistant"' not in line.replace(" ", ""):
                continue
            try:
                ts = json.loads(line).get("timestamp", "")
                if ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() >= since:
                    n += 1
            except ValueError:
                continue
        if n:
            hits[str(path.relative_to(PROJECTS))] = n
    return hits


def wait_quiet(own: set[str], quiet: int = 600) -> None:
    """Don't start an arm while another Claude session is active: wait for `quiet` s of silence."""
    waited = 0
    while other_claude_activity(time.time() - quiet, own):
        if waited % 600 == 0:
            log(f"other Claude sessions active in the last {quiet // 60} min: "
                f"{other_claude_activity(time.time() - quiet, own)}; waiting")
        time.sleep(60)
        waited += 60


def fresh_window(label: str, ids: dict, need: int, max_used: float = 40) -> None:
    """Take reading `label` once Claude is quiet; if the 5 h window can't hold a whole arm,
    wait for its reset and take it again."""
    while True:
        wait_quiet(set(ids.values()))
        ids[label] = str(uuid.uuid4())
        rl = reading(label, ids[label])
        left, used = rl["five_hour"]["resets_at"] - time.time(), rl["five_hour"]["used_percentage"]
        if left >= need and used <= max_used:
            return
        log(f"5h window: {left / 60:.0f} min left, {used}% used; waiting for the reset")
        time.sleep(max(left, 0) + 180)


def round3() -> None:
    """Round 3: bigger task (task-r3.md), dg 0.7.4, arm A then arm B, each bracketed in its own 5 h window."""
    global OUT, FLAGS, TASK, CHECK, RUN, ARM_CAP
    TASK, CHECK, RUN, ARM_CAP = "task-r3.md", "check_r3.py", "run3", 180 * 60
    OUT = HERE / "results" / "run3"
    FLAGS = OUT / ".flags"
    OUT.mkdir(parents=True, exist_ok=True)
    FLAGS.mkdir(exist_ok=True)
    plugins = json.loads((Path.home() / ".claude" / "plugins" / "installed_plugins.json").read_text())
    installed = [i.get("version") for i in plugins["plugins"].get("delegation-governor@delegation-governor-marketplace", [])]
    assert installed == ["0.7.4"], f"delegation-governor plugin is {installed}, round 3 needs 0.7.4"
    dg_py = Path(sh("sh", "-c", "head -1 \"$(command -v dg)\"").stdout[2:].strip())
    dg_src = subprocess.run([str(dg_py), "-c", "import dg.cli; print(dg.cli.__file__)"], capture_output=True, text=True).stdout
    assert "/0.7.4/" in dg_src, f"dg CLI runs {dg_src.strip()}, round 3 needs the 0.7.4 cache"
    assert "no tasks" in sh("dg", "tasks", "running").stdout, "dg tasks are running"
    assert {p.name for p in SITE.iterdir()} <= {"assets", ".DS_Store"}, "rin-website is not clean"
    for a in ("arm-a3", "arm-b3"):
        assert not (SITE.parent / "rin-website-archive" / a).exists(), f"archive {a} exists"

    ids: dict[str, str] = {}
    for name, letter in (("arm-a3", "a"), ("arm-b3", "b")):
        before, after, sid = f"{letter.upper()}3-before", f"{letter.upper()}3-after", str(uuid.uuid4())
        fresh_window(before, ids, need=ARM_CAP + 10 * 60)
        ids[name] = sid
        (OUT / "session-ids.json").write_text(json.dumps(ids, indent=2) + "\n")
        t0 = time.time()
        arm(name, letter, sid)
        contamination = other_claude_activity(t0, set(ids.values()))
        if contamination:
            log(f"WARNING: other Claude sessions were active during {name}: {contamination}")
        time.sleep(120)
        ids[after] = str(uuid.uuid4())
        reading(after, ids[after])
        (OUT / "session-ids.json").write_text(json.dumps(ids, indent=2) + "\n")
        r = sh("python3", "measure.py", sid, "--before", ids[before], "--after", ids[after])
        out = json.loads(r.stdout) if r.stdout else {"error": r.stderr}
        out["other_claude_activity"] = contamination
        (OUT / f"arm-{letter}.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
        log(f"measure {name}: 5h {out.get('bracketed_windows', {}).get('five_hour')} cost {out.get('cost_usd')}")
    log("round 3 done")


if __name__ == "__main__":
    {"rerun-a": rerun_a, "round3": round3}.get(sys.argv[1] if sys.argv[1:] else "", main)()
