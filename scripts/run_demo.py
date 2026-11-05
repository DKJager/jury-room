"""Run the Jury Room in a real team-chat channel.

Starts six Switch agents in one process: builder, three jurors (one model each from three makers), foreperson and merge
gate. It creates a Switch room with the agents and the named Mattermost user, which Switch bridges to a Mattermost
channel. Then a person types in that channel:
    TASK: page() returns one item too many per page; fix pager.py
and later replies `merge` or `reject`. Only names in JURY_MERGE_AUTHORIZED can decide.

  python scripts/run_demo.py --human user [--stub]      (--stub = deterministic jurors/builder, no model calls)
Env: SWITCH_BASE_URL (default http://127.0.0.1:8000), SWITCH_ENV_FILE (deploy/.env), NEBIUS_API_KEY (real models),
NEBIUS_CHAT_URL (default https://api.tokenfactory.nebius.com/v1/chat/completions).
"""
import argparse, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request, uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
os.environ.setdefault("JURY_UPSTREAM_EVIDENCE", os.path.join(ROOT, "out", "upstream_evidence.jsonl"))
from agents.switch_client import SwitchClient  # noqa: E402
from agents import builder, foreperson, juror, merge_gate  # noqa: E402

JURORS = {"juror-openai": "openai/gpt-oss-120b", "juror-deepseek": "deepseek-ai/DeepSeek-V4-Flash-0731", "juror-qwen": "Qwen/Qwen3-235B-A22B-Instruct-2507"}
BUILDER_MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"
# [#2486] Provider override, Nebius stays the default: JURY_CHAT_URL, JURY_API_KEY,
# JURY_MODELS="juror-a=model,juror-b=model,juror-c=model", JURY_BUILDER_MODEL.
if os.environ.get("JURY_MODELS"):
    JURORS = dict(kv.split("=", 1) for kv in os.environ["JURY_MODELS"].split(","))
BUILDER_MODEL = os.environ.get("JURY_BUILDER_MODEL", BUILDER_MODEL)
MAKERS = {"gpt-oss": "OpenAI", "deepseek": "DeepSeek", "qwen": "Alibaba", "minimax": "MiniMax", "mimo": "Xiaomi", "glm": "Zhipu"}


def maker(model):
    m = model.lower().split("/")[-1]
    return next((v for k, v in MAKERS.items() if m.startswith(k)), "unknown:" + m)


def preflight(dry_run_path):
    """[#2486] Every juror must return non-empty content and the jurors must span >= 3 distinct makers, else abort."""
    rows, ok_makers = [], set()
    for name, m in JURORS.items():
        reply = chat(m, "Reply with the single word OK.", 200)
        live = bool(reply.strip())
        rows.append({"ts": time.time(), "juror": name, "model": m, "maker": maker(m), "live": live})
        if live:
            ok_makers.add(maker(m))
    rows.append({"ts": time.time(), "builder": BUILDER_MODEL, "builder_maker": maker(BUILDER_MODEL),
                 "shares_maker_with_juror": maker(BUILDER_MODEL) in {maker(m) for m in JURORS.values()},
                 "distinct_live_juror_makers": sorted(ok_makers), "gate_pass": len(ok_makers) >= 3})
    with open(dry_run_path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    if len(ok_makers) < 3:
        sys.exit(f"PREFLIGHT FAIL: only {len(ok_makers)} distinct live juror makers: {sorted(ok_makers)}")
    print("PREFLIGHT OK", sorted(ok_makers), flush=True)


def chat(model, prompt, max_tokens=700):
    url = os.environ.get("JURY_CHAT_URL") or os.environ.get("NEBIUS_CHAT_URL", "https://api.tokenfactory.nebius.com/v1/chat/completions")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": max_tokens}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + (os.environ.get("JURY_API_KEY") or os.environ["NEBIUS_API_KEY"]), "Content-Type": "application/json"})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            reply = json.loads(r.read())["choices"][0]["message"]["content"] or ""
    except Exception as e:
        reply = ""
        err = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        err = None
    with open(os.path.join(ROOT, "out", "demo_models.jsonl"), "a") as f:  # every model reply is kept for review
        f.write(json.dumps({"ts": time.time(), "model": model, "latency_s": round(time.time() - t, 2), "error": err, "reply": reply[:2000]}) + "\n")
    return reply


def checked_builder(model, repo, tries=3):
    """The builder's model call, retried until its diff passes `git apply --check` on the work repo (max `tries`)."""
    def call(prompt):
        note, diff = "", ""
        for i in range(tries):
            diff = chat(model, prompt + note, 900).strip()
            if diff.startswith("```"):
                diff = "\n".join(l for l in diff.splitlines() if not l.startswith("```"))
            diff = diff.strip("\n") + "\n"
            p = subprocess.run(["git", "-C", repo, "apply", "--check", "-"], input=diff, capture_output=True, text=True)
            if p.returncode == 0:
                return diff
            note = ("\n\nYour previous answer was not a valid unified diff for this repo (git apply --check said: "
                    + p.stderr.strip()[:300] + "). Reply with ONLY a corrected unified diff starting with '--- a/'.")
        return diff
    return call


def work_repo():
    d = tempfile.mkdtemp(prefix="jury-demo-")
    for f in ("pager.py", "test_pager.py"):
        shutil.copy(os.path.join(ROOT, "demo", f), d)
    g = ["git", "-C", d, "-c", "user.email=jury@3dogs.ai", "-c", "user.name=Jury Room"]
    subprocess.run(["git", "init", "-q", "-b", "main", d], check=True)
    subprocess.run(g + ["add", "."], check=True)
    subprocess.run(g + ["commit", "-qm", "base: demo pager"], check=True)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--human", required=True, help="Mattermost username who may merge/reject")
    ap.add_argument("--stub", action="store_true", help="deterministic builder and jurors, no model calls")
    ap.add_argument("--builder-diff", help="use this diff file as the builder's patch (e.g. accept/bad.diff to put a bad patch on trial)")
    ap.add_argument("--juror-stub", choices=["approve", "split-reject"], help="stub juror replies (only with --stub)")
    ap.add_argument("--timeout", type=float, default=900)
    ap.add_argument("--preflight-only", action="store_true", help="[#2486] run the maker-diversity gate, write out/demo_dry_run.jsonl, exit")
    a = ap.parse_args()
    if not a.stub:  # [#2486] maker-diversity gate before any real-model run
        preflight(os.path.join(ROOT, "out", "demo_dry_run.jsonl"))
        if a.preflight_only:
            return
    base = os.environ.get("SWITCH_BASE_URL", "http://127.0.0.1:8000")
    envf = os.environ.get("SWITCH_ENV_FILE", os.path.join(ROOT, "deploy", ".env"))
    tok = [l.split("=", 1)[1].strip() for l in open(envf) if l.startswith("AGENT_REGISTRATION_TOKEN=")][0]
    os.environ["JURY_MERGE_AUTHORIZED"] = a.human
    run = uuid.uuid4().hex[:4]
    names = {k: f"{k}-{run}" for k in ["builder", *JURORS, "foreperson", "merge-gate"]}
    cl = {}
    for k, n in names.items():
        aid, key = SwitchClient.register(base, tok, n, f"Jury Room {k}")
        cl[k] = SwitchClient(base, key, aid)
    # Switch creates each agent's Mattermost bot and adds it to the team asynchronously; a room created before that
    # finishes cannot add the bots to its channel ("No team member found"). Give the bridge time, then re-invite.
    time.sleep(float(os.environ.get("JURY_BOT_SETTLE_S", "15")))
    room_name = f"jury-room-{run}"
    res = cl["builder"].call_op("create_room", {"name": room_name, "description": "Jury Room: a coding agent's patch on trial; three AI jurors vote, a human decides",
                                                "agent_names": list(names.values()), "user_names": [a.human]})
    room = res["id"]
    time.sleep(3)
    for k, n in names.items():  # idempotent: makes sure every agent's bot is a member of the bridged channel
        try:
            cl["builder"].call_op("invite_agent_to_room", {"room_id": room, "agent_name": n})
        except Exception as e:
            print(f"invite {n}: {e}", flush=True)
    repo = work_repo()
    print(f"ROOM {room_name} ({room}) is a channel in Mattermost now; work repo {repo}", flush=True)
    fix = open(os.path.join(ROOT, "accept", "fix.diff")).read()
    stub_ok = "VERDICT: APPROVE\nREASON: removes the +1 so a page holds exactly size items\nRISK: low"
    stub_rej = "VERDICT: REJECT\nREASON: the patch edits the test expectation instead of fixing pager.py\nRISK: high"
    if a.builder_diff:
        given = open(a.builder_diff).read()
        bmodel = lambda p: given
    else:
        bmodel = (lambda p: fix) if a.stub else checked_builder(BUILDER_MODEL, repo)
    stubs = [stub_rej, stub_rej, stub_ok] if a.juror_stub == "split-reject" else [stub_ok] * 3
    out = {}
    jn = [names[k] for k in JURORS]
    ts = [threading.Thread(target=lambda: out.__setitem__("builder", builder.run_builder(cl["builder"], room, repo, bmodel, timeout=a.timeout)), daemon=True),
          threading.Thread(target=lambda: out.__setitem__("tally", foreperson.run_foreperson(cl["foreperson"], room, jn, timeout=a.timeout)), daemon=True),
          threading.Thread(target=lambda: out.__setitem__("gate", merge_gate.run_merge_gate(cl["merge-gate"], room, repo, [a.human], timeout=a.timeout)), daemon=True)]
    for i, (k, m) in enumerate(JURORS.items()):
        call = (lambda p, _s=stubs[i]: _s) if a.stub else (lambda p, _m=m: chat(_m, p, 200))
        ts.append(threading.Thread(target=lambda k=k, c=call: out.__setitem__(k, juror.run_juror(cl[k], room, c, timeout=a.timeout)), daemon=True))
    for t in ts:
        t.start()
    for t in ts:
        t.join(a.timeout + 30)
    print("RESULT", json.dumps({"gate": out.get("gate"), "tally": (out.get("tally") or {}).get("outcome")}), flush=True)
    for c in cl.values():
        c.close()


if __name__ == "__main__":
    main()
