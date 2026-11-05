"""Slice-3 acceptance test for Jury Room: builder sandbox + human merge gate, end to end on the REAL Switch stack.
MANAGER-WRITTEN. READ-ONLY: never edit this file to make it pass. No fake server; model calls are deterministic stubs.

Part A (sandbox, no Switch): builder.run_sandbox on a throwaway git copy of demo/ runs the tests inside
`docker run --network none` and never touches the source repo; the base tree fails, accept/fix.diff passes.
Part B (live, two cases in fresh rooms):
  merge case: human posts TASK -> builder (stub returns accept/fix.diff) posts CASE with DIFF + sandbox result ->
  3 jurors (stub APPROVE) -> foreperson TALLY -> a JUROR agent posts "merge" (must be ignored: only humans decide) ->
  the human posts "merge" -> merge gate commits on branch jury/<id> and posts "MERGED <hash> on jury/<id>";
  the hash must exist in the work repo and the tests must pass there.
  reject case: builder stub returns accept/bad.diff (edits the test to hide the bug) -> jurors REJECT -> human posts
  "reject" -> gate posts "REJECTED" and the work repo is byte-identical to before.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

LANE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LANE)
from agents.switch_client import SwitchClient  # noqa: E402
from agents import builder, foreperson, juror, merge_gate  # noqa: E402

BASE = os.environ.get("SWITCH_BASE_URL", "http://127.0.0.1:8000")
ENV_FILE = os.environ.get("SWITCH_ENV_FILE", "/opt/jury-room/stack/.env")
FIX = open(os.path.join(LANE, "accept", "fix.diff")).read()
BAD = open(os.path.join(LANE, "accept", "bad.diff")).read()
GATE_LOG = os.environ.get("JURY_GATE_LOG", "/opt/jury-room/merge_gate_log.jsonl")  # #2479
FORKBOMB = open(os.path.join(LANE, "accept", "forkbomb.diff")).read()
APPROVE = "VERDICT: APPROVE\nREASON: removes the +1 so a page holds exactly size items\nRISK: low"
REJECT = "VERDICT: REJECT\nREASON: the patch edits the test expectation instead of fixing pager.py\nRISK: high"


def _token():
    for line in open(ENV_FILE, encoding="utf-8"):
        if line.startswith("AGENT_REGISTRATION_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("AGENT_REGISTRATION_TOKEN not found")


def _work_repo():
    d = tempfile.mkdtemp(prefix="jr-work-")
    for f in ("pager.py", "test_pager.py"):
        shutil.copy(os.path.join(LANE, "demo", f), d)
    g = ["git", "-C", d, "-c", "user.email=jury@3dogs.ai", "-c", "user.name=Jury Room"]
    subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["commit", "-qm", "base: demo pager with off-by-one"], check=True)
    return d


def _tree_hash(d):
    h = hashlib.sha256()
    for root, dirs, files in os.walk(d):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(p[len(d):].encode())
            h.update(open(p, "rb").read())
    return h.hexdigest()


def test_sandbox():
    cmd = " ".join(builder.SANDBOX_CMD)
    assert "--network none" in cmd and "--rm" in cmd, f"sandbox must be docker --rm --network none, got {cmd!r}"
    d = _work_repo()
    before = _tree_hash(d)
    base = builder.run_sandbox(d, "")
    assert base["applied"] is True and base["passed"] is False, f"base tree must fail its tests: {base}"
    good = builder.run_sandbox(d, FIX)
    assert good["applied"] is True and good["passed"] is True and good["returncode"] == 0, good
    # #2475 integrity: the files the diff names are exactly the files that changed in the sandbox checkout
    assert good["diff_files"] == ["pager.py"] and good["changed_files"] == ["pager.py"], good
    # #2475 stress: a fork bomb added by a diff is stopped by --pids-limit and the run still returns in time
    t0 = time.time()
    fb = builder.run_sandbox(d, FORKBOMB, timeout=90)
    assert time.time() - t0 < 100, "sandbox run must be bounded"
    assert fb["applied"] is True and fb["diff_files"] == ["test_forkbomb.py"] == fb["changed_files"], fb
    assert any(l.startswith("test_pids_limit_holds") and l.rstrip().endswith("... ok") for l in fb["output"].splitlines()), f"pids limit did not hold: {fb['output'][-800:]}"
    broken = builder.run_sandbox(d, "--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-x\n+y\n")
    assert broken["applied"] is False and broken["passed"] is False, broken
    assert _tree_hash(d) == before, "run_sandbox must never modify the source repo"


def _case(tok, run, label, diff, stubs, decision):
    names = {k: f"jr3-{label}-{k}-{run}" for k in ("human", "impostor", "builder", "fore", "gate", "j1", "j2", "j3")}
    cl = {}
    for k, n in names.items():
        aid, key = SwitchClient.register(BASE, tok, n, f"slice-3 acceptance {k}")
        cl[k] = SwitchClient(BASE, key, aid)
    repo = _work_repo()
    before = _tree_hash(repo)
    os.environ["JURY_MERGE_AUTHORIZED"] = names["human"]  # #2475: only this human may decide
    try:
        room = cl["human"].create_room(f"jr3-{label}-{run}", "slice-3 acceptance", list(names.values()))
        cl["human"].open_stream([room])
        cl["human"].connect_to_room(room)
        out = {}
        jn = [names["j1"], names["j2"], names["j3"]]
        ts = [
            threading.Thread(target=lambda: out.__setitem__("builder", builder.run_builder(cl["builder"], room, repo, lambda p: diff, timeout=90)), daemon=True),
            threading.Thread(target=lambda: out.__setitem__("fore", foreperson.run_foreperson(cl["fore"], room, jn, timeout=150)), daemon=True),
            threading.Thread(target=lambda: out.__setitem__("gate", merge_gate.run_merge_gate(cl["gate"], room, repo, [names["impostor"], names["human"]], timeout=180)), daemon=True),
        ]
        for i, k in enumerate(("j1", "j2", "j3")):
            ts.append(threading.Thread(target=lambda k=k, s=stubs[i]: out.__setitem__(k, juror.run_juror(cl[k], room, lambda p, _s=s: _s, timeout=150)), daemon=True))
        for t in ts:
            t.start()
        time.sleep(3)
        cl["human"].post_message(room, "TASK: page() returns one item too many per page; fix pager.py")
        seen = {"case": None, "tally": None, "result": None}
        deadline = time.time() + 170
        juror_said_merge = False
        while time.time() < deadline and seen["result"] is None:
            ev = cl["human"].next_event(timeout=2.0)  # the client heartbeats on its own timer
            if ev is None:
                continue
            if ev.get("type") != "message":
                continue
            body = str((ev.get("payload") or {}).get("body", ""))
            if body.startswith("CASE:") and seen["case"] is None:
                seen["case"] = body
            elif body.startswith("TALLY: ") and seen["tally"] is None:
                seen["tally"] = body
                if decision == "merge":
                    cl["j1"].post_message(room, "merge")  # a juror is not a human: must be ignored
                    cl["impostor"].post_message(room, "merge")  # listed but not in JURY_MERGE_AUTHORIZED: ignored
                    juror_said_merge = True
                    time.sleep(4)
                cl["human"].post_message(room, decision)
            elif body.startswith(("MERGED ", "REJECTED", "NO DECISION", "MERGE FAILED")):
                seen["result"] = body  # every documented terminal message counts as the gate's result
                try:  # #2479: every merge-gate output is logged for calibration
                    with open(GATE_LOG, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts": time.time(), "case": label, "run": run, "result": body[:300],
                                            "diff": (seen["case"] or "").split("DIFF:\n", 1)[-1][:2000]}) + "\n")
                except OSError:
                    pass
        for t in ts:
            t.join(10)
        assert seen["case"] and "DIFF:" in seen["case"] and "TESTS:" in seen["case"], f"{label}: CASE missing/incomplete: {seen['case']!r}"
        assert seen["tally"], f"{label}: no TALLY in the room"
        assert seen["result"], f"{label}: merge gate posted nothing"
        return repo, before, seen, out, juror_said_merge
    finally:
        for c in cl.values():
            c.close()


def test_live_e2e():
    tok = _token()
    run = uuid.uuid4().hex[:5]
    repo, before, seen, out, jsm = _case(tok, run, "m", FIX, [APPROVE, APPROVE, APPROVE], "merge")
    assert jsm and "TESTS: passed" in seen["case"], seen["case"]
    assert seen["tally"].splitlines()[0] == "TALLY: UNANIMOUS_APPROVE", seen["tally"]
    assert not seen["result"].startswith("MERGE FAILED"), f"merge gate reported: {seen['result']}"
    parts = seen["result"].split()
    assert parts[0] == "MERGED" and len(parts) >= 4 and parts[2] == "on" and parts[3].startswith("jury/"), seen["result"]
    h, branch = parts[1], parts[3]
    r = subprocess.run(["git", "-C", repo, "log", "--format=%H %an", "-1", branch], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.startswith(h[:7]), f"commit {h} not on {branch}: {r.stdout!r} {r.stderr!r}"
    main = subprocess.run(["git", "-C", repo, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    assert not main.startswith(h[:7]), "the merge gate must commit on its own jury/ branch, not on main"
    t = subprocess.run(["git", "-C", repo, "show", f"{branch}:pager.py"], capture_output=True, text=True).stdout
    assert "items[start:end]" in t and "end + 1" not in t, "merged branch must carry the fix"
    assert out.get("gate", {}).get("decided_by", "").endswith("-human-" + run) or "-human-" in out["gate"].get("decided_by", ""), out.get("gate")
    assert "impostor" not in out["gate"]["decided_by"] and "-j1-" not in out["gate"]["decided_by"], out.get("gate")
    repo, before, seen, out, _ = _case(tok, run, "r", BAD, [REJECT, REJECT, APPROVE], "reject")
    assert "TESTS: passed" in seen["case"], "the bad diff hides the bug, so its tests pass: the jury must catch it"
    assert seen["tally"].splitlines()[0] == "TALLY: SPLIT", seen["tally"]
    assert seen["result"].startswith("REJECTED"), seen["result"]
    assert _tree_hash(repo) == before, "a rejected patch must leave the work repo untouched"


if __name__ == "__main__":
    test_sandbox()
    test_live_e2e()
    print("PASS")
