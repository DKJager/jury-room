"""Slice-2 acceptance test for Jury Room (Committee decision on slice 2). MANAGER-WRITTEN from the live Switch 0.31.0
stack on 127.0.0.1:8000 and the slice-2 SPEC. READ-ONLY: never edit this file to make it pass. No fake server.

Part A (pure): foreperson.parse_verdict and foreperson.tally on fixed inputs.
Part B (live, real stack): for three cases (unanimous, split, malformed) a "human" agent posts a CASE message into a
fresh room; three juror agents (each its own Switch agent and its own SwitchClient) run juror.run_juror with a
DETERMINISTIC stub model caller (no network model, no key needed); a foreperson agent runs foreperson.run_foreperson,
which must read the three VERDICT messages from the room's event stream and post a TALLY message. The human agent
must then read that TALLY back from its own stream with the right outcome. Prints PASS last only if all held.
"""
import json
import os
import sys
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.switch_client import SwitchClient  # noqa: E402
from agents import foreperson, juror  # noqa: E402

BASE = os.environ.get("SWITCH_BASE_URL", "http://127.0.0.1:8000")
ENV_FILE = os.environ.get("SWITCH_ENV_FILE", "/opt/jury-room/stack/.env")
APPROVE = "VERDICT: APPROVE\nREASON: the patch fixes the off-by-one and the tests pass\nRISK: low"
REJECT = "VERDICT: REJECT\nREASON: the patch silences the failing test instead of fixing the bug\nRISK: high"


def _token():
    for line in open(ENV_FILE, encoding="utf-8"):
        if line.startswith("AGENT_REGISTRATION_TOKEN="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("AGENT_REGISTRATION_TOKEN not found")


def test_parse_and_tally():
    v = foreperson.parse_verdict(APPROVE)
    assert v["verdict"] == "APPROVE" and "off-by-one" in v["reason"] and v["risk"] == "low", v
    assert foreperson.parse_verdict("  verdict:  reject \nreason: x\nrisk: medium")["verdict"] == "REJECT"
    for bad in ("", "I think it is fine", "VERDICT: MAYBE", None):
        assert foreperson.parse_verdict(bad)["verdict"] == "ABSTAIN", bad
    A = {"verdict": "APPROVE", "reason": "ok", "risk": "low"}
    R = {"verdict": "REJECT", "reason": "breaks x", "risk": "high"}
    X = {"verdict": "ABSTAIN", "reason": "", "risk": ""}
    t = foreperson.tally({"j1": A, "j2": A, "j3": A})
    assert t["outcome"] == "UNANIMOUS_APPROVE" and t["approve"] == 3 and t["dissent"] == [], t
    assert foreperson.tally({"j1": R, "j2": R, "j3": R})["outcome"] == "UNANIMOUS_REJECT"
    t = foreperson.tally({"j1": A, "j2": A, "j3": R})
    assert t["outcome"] == "SPLIT" and t["majority"] == "APPROVE", t
    assert [d["juror"] for d in t["dissent"]] == ["j3"] and t["dissent"][0]["reason"] == "breaks x", t
    assert foreperson.tally({"j1": A, "j2": X, "j3": X})["outcome"] == "HUNG"
    assert foreperson.tally({"j1": A, "j2": R, "j3": X})["outcome"] == "HUNG"
    t = foreperson.tally({"j1": A, "j2": A, "j3": X})
    assert t["outcome"] == "SPLIT" and [d["juror"] for d in t["dissent"]] == ["j3"], t
    t = foreperson.tally({"j1": A, "j2": X, "j3": X})
    assert t["abstain"] == 2 and t["abstained"] == ["j2", "j3"], t  # #2472: abstainers are audited, never dropped
    text = foreperson.format_tally(foreperson.tally({"j1": A, "j2": A, "j3": R}))
    assert text.splitlines()[0] == "TALLY: SPLIT" and "j3" in text and "breaks x" in text, text
    js = _json_line(text)  # #2472: machine-readable tally
    assert js["outcome"] == "SPLIT" and js["counts"] == {"approve": 2, "reject": 1, "abstain": 0}, js
    assert js["dissent"] == [{"juror": "j3", "verdict": "REJECT", "reason": "breaks x"}], js


def _json_line(text):
    lines = [l for l in text.splitlines() if l.startswith("JSON: ")]
    assert len(lines) == 1, f"tally must carry exactly one 'JSON: ' line, got {text!r}"
    return json.loads(lines[0][6:])


def _case(tok, run, label, stubs, expected):
    names = {k: f"jr-{label}-{k}-{run}" for k in ("human", "fore", "j1", "j2", "j3")}
    cl = {}
    for k, n in names.items():
        aid, key = SwitchClient.register(BASE, tok, n, f"slice-2 acceptance {k}")
        cl[k] = SwitchClient(BASE, key, aid)
    try:
        room = cl["human"].create_room(f"jr-{label}-{run}", "slice-2 acceptance", list(names.values()))
        cl["human"].open_stream([room])
        cl["human"].connect_to_room(room)
        posted = {}

        def jur(k, stub):
            posted[k] = juror.run_juror(cl[k], room, lambda prompt, _s=stub: _s, timeout=45)
        th = [threading.Thread(target=jur, args=(k, stubs[i]), daemon=True) for i, k in enumerate(("j1", "j2", "j3"))]
        fres = {}
        fth = threading.Thread(target=lambda: fres.update(foreperson.run_foreperson(cl["fore"], room, [names["j1"], names["j2"], names["j3"]], timeout=60)), daemon=True)
        for t in th:
            t.start()
        fth.start()
        time.sleep(3)
        case_text = "CASE: fix off-by-one in pagination\nDIFF:\n-    return items[start:end+1]\n+    return items[start:end]\nTESTS: 12 passed"
        cl["human"].post_message(room, case_text)
        for t in th:
            t.join(50)
        fth.join(65)
        assert set(posted) == {"j1", "j2", "j3"}, f"{label}: jurors posted {sorted(posted)}"
        for k in ("j1", "j2", "j3"):
            assert posted[k].startswith("VERDICT: "), f"{label}: {k} posted {posted[k]!r}"
        assert fres.get("outcome") == expected, f"{label}: foreperson returned {fres!r}"
        deadline, seen = time.time() + 20, None
        while time.time() < deadline and seen is None:
            ev = cl["human"].next_event(timeout=2.0)
            if ev is None:
                cl["human"].beat()
                continue
            body = str((ev.get("payload") or {}).get("body", ""))
            if ev.get("type") == "message" and body.startswith("TALLY: "):
                seen = body
        assert seen is not None, f"{label}: human never saw a TALLY message in the room"
        assert seen.splitlines()[0] == f"TALLY: {expected}", f"{label}: room shows {seen.splitlines()[0]!r}"
        js = _json_line(seen)
        assert js["outcome"] == expected, js
        return seen, js, names
    finally:
        for c in cl.values():
            c.close()


def test_live_jury():
    tok = _token()
    run = uuid.uuid4().hex[:5]
    _, js, _n = _case(tok, run, "u", [APPROVE, APPROVE, APPROVE], "UNANIMOUS_APPROVE")
    assert js["counts"] == {"approve": 3, "reject": 0, "abstain": 0} and js["dissent"] == [], js
    s, js, n = _case(tok, run, "s", [APPROVE, APPROVE, REJECT], "SPLIT")
    assert "silences the failing test" in s, "split tally must carry the dissenter's reason"
    assert [d["juror"] for d in js["dissent"]] == [n["j3"]] and "silences the failing test" in js["dissent"][0]["reason"], js
    # #2472 malformed paths: empty string, a non-VERDICT line, an invalid verdict word -> all ABSTAIN -> HUNG
    _, js, n = _case(tok, run, "m", ["", "Looks fine to me", "VERDICT: MAYBE"], "HUNG")
    assert js["counts"] == {"approve": 0, "reject": 0, "abstain": 3}, js
    assert sorted(js["abstained"]) == sorted([n["j1"], n["j2"], n["j3"]]), js
    # one malformed among two approvals: counted, named, never silently dropped into UNANIMOUS
    _, js, n = _case(tok, run, "a", [APPROVE, APPROVE, "{\"oops\": true}"], "SPLIT")
    assert js["counts"]["abstain"] == 1 and js["abstained"] == [n["j3"]], js


if __name__ == "__main__":
    test_parse_and_tally()
    test_live_jury()
    print("PASS")
