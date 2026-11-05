"""One-shot REAL-model smoke (Committee #2472): three Nebius open-weight jurors from three makers judge the demo case
once each. Hard cap: max_tokens 800 per call (#2498), exactly 3 calls, so spend stays far below $0.50. Logs model, prompt
bytes, reply, latency and token usage to out/real_model_smoke.jsonl. Not part of any gate. Key comes from env only."""
import json, os, sys, time, urllib.request

MODELS = ["openai/gpt-oss-120b", "deepseek-ai/DeepSeek-V4.1-Flash-1017", "Qwen/Qwen3-30B-A3B-Instruct-2507"]
URL = os.environ.get("NEBIUS_CHAT_URL", "https://api.tokenfactory.nebius.com/v1/chat/completions")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents import juror, foreperson  # noqa: E402

CASE = ("CASE: page() returns one item too many per page; fix pager.py\nDIFF:\n--- a/pager.py\n+++ b/pager.py\n"
        "@@ -10 +10 @@\n-    return items[start:end + 1]\n+    return items[start:end]\nTESTS: passed (Ran 3 tests, OK)")


def main():
    key = os.environ["NEBIUS_API_KEY"]
    os.makedirs("out", exist_ok=True)
    rows, verdicts = [], {}
    prompt = juror.build_prompt(CASE)
    for m in MODELS:
        body = {"model": m, "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 800}
        t = time.time()
        req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            reply = d["choices"][0]["message"]["content"] or ""
            usage = d.get("usage", {})
            finish = d["choices"][0].get("finish_reason")
        except Exception as e:
            reply, usage, finish = "", {"error": f"{type(e).__name__}: {str(e)[:200]}"}, None
        v = foreperson.parse_verdict(reply)
        verdicts[m] = v
        rows.append({"ts": time.time(), "model": m, "prompt_bytes": len(prompt), "latency_s": round(time.time() - t, 2),
                     "usage": usage, "finish_reason": finish, "completion_tokens": usage.get("completion_tokens"), "reply": reply[:600], "parsed": v})
    t = foreperson.tally(verdicts)
    with open("out/real_model_smoke.jsonl", "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.write(json.dumps({"tally": t}) + "\n")
    print(foreperson.format_tally(t))
    for r in rows:  # #2498 gate: every call must finish on stop with a non-empty reply
        print(f"{r['model']}: finish={r['finish_reason']} completion_tokens={r['completion_tokens']}")
    bad = [r['model'] for r in rows if r['finish_reason'] != 'stop' or not r['reply'].strip()]
    if bad:
        print('ALERT smoke failed (not stop or empty):', ', '.join(bad), file=sys.stderr)
        sys.exit(1)
    print('SMOKE OK: 3/3 finish=stop')


if __name__ == "__main__":
    main()
