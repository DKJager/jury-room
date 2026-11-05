# Jury Room

**Jury Room puts a coding agent's patch on trial in team chat: three AI jurors from three different makers vote, a human decides.**

Built for the [Flip the Switch hackathon](https://www.wemakedevs.org/hackathons/switch) (WeMakeDevs × DevTools Academy × Switch), Track 01 Best Business Workflow. By [3Dogs Nexus](https://3dogs.ai).

## Why

A coding agent can make the tests pass by changing the tests. Green CI is not the same as a good patch. Jury Room makes every agent patch go through a room where independent reviewers argue about it in the open, and nothing lands until a named person says so.

## How Switch is used (and why it stops working without it)

Every step is a message in one Switch room. The room's shared log *is* the trial record:

1. A person posts `TASK: …` in the room.
2. **Builder** agent reads it, asks a model for a unified diff, runs the repo's tests in a sealed sandbox (`docker run --network none --read-only`, 1 CPU, 512 MB, 128 PIDs), and posts `CASE / DIFF / TESTS` to the room.
3. Three **juror** agents (one open-weight model each, from different makers) read the case from the room and each post `VERDICT / REASON / RISK`.
4. The **foreperson** agent tallies them (UNANIMOUS / SPLIT with named dissent / HUNG) and posts a human-readable tally plus a machine-readable `JSON:` line.
5. Only an authorized human (`JURY_MERGE_AUTHORIZED`) replying `merge` or `reject` decides. A juror saying "merge" is ignored. Merge commits on a `jury/<id>` branch, never on `main`; reject touches nothing.

All agents join through the [Switch agent protocol](https://docs.switchagents.ai) (SSE down, HTTP up): `POST /agents`, `GET /agents/{id}/events`, `connect_to_room`, `connection/beat`, `post_message`, `read_context`. Without Switch there is no room, no shared log, and no jury.

## Do X, see Y

Needs Linux with Docker (Compose v2), Python 3.12, git, openssl. Everything binds to `127.0.0.1`.

1. **Start Switch 0.31.0 with Mattermost:**
   ```bash
   git clone https://github.com/DKJager/jury-room && cd jury-room
   ./deploy/fetch_switch.sh                     # pinned upstream compose, commit 2d72c57
   sudo ./deploy/preflight.sh                   # docker up, disk, firewall rule (computed, not echoed)
   COMPOSE_PROFILES=collab ./deploy/up.sh      # Switch API :8000, Mattermost :8065, secrets in deploy/.env
   ```
   **See:** `Switch healthy on 127.0.0.1:8000`; Mattermost at http://127.0.0.1:8065 (user and password are in `deploy/.env`: `MATTERMOST_USER`, `MATTERMOST_USER_PASSWORD`).
2. **Prove it works with no models and no fakes:** `bash scripts/smoke.sh`
   **See:** `ok pins … ok live_switch PASS … ok live_jury PASS … ok live_e2e PASS … SMOKE: all steps passed`. Every step is also logged to `out/test_evidence.jsonl`.
3. **Run the jury in team chat:** `NEBIUS_API_KEY=… python3 scripts/run_demo.py --human user` (or `--stub` for no model calls).
   Any OpenAI-compatible provider works: set `JURY_CHAT_URL`, `JURY_API_KEY`, `JURY_MODELS="juror-a=model,juror-b=model,juror-c=model"` and `JURY_BUILDER_MODEL`. Before any real-model run, a pre-flight gate calls every juror once and aborts unless their live replies span **at least three distinct makers**. The result is logged to `out/demo_dry_run.jsonl` (`--preflight-only` runs just the gate).
   **See:** a new `jury-room-xxxx` channel in Mattermost with six agent bots. Type `TASK: page() returns one item too many per page; fix pager.py`. The builder posts `CASE/DIFF/TESTS`, three jurors post verdicts, and the foreperson posts the tally. Type `merge`, then see `MERGED <hash> on jury/<id> by user`.
4. **See the jury catch a bad patch:** `accept/live_e2e.py` replays `accept/bad.diff`, which edits the test so the bug "passes". The tests go green, the jurors review the diff itself, the human rejects, and the repo is untouched.
5. `./deploy/teardown.sh` removes everything.

If more than ~50 agents register over time, raise Mattermost's `TeamSettings.MaxUsersPerTeam` (System Console → Users and Teams). Each agent gets a bot that joins the team, and at the cap new bots can't join channels.

## Demo video

The video is screenshots of the live Mattermost UI, captured at each step of two real runs and played back with captions.

- **Scene 1:** a model writes the patch, and the person merges.
- **Scene 2:** a planted bad patch (`accept/bad.diff`) is put on trial, and the person rejects it.

The models in the video are real open-weight models on **Vultr Serverless Inference**, through the same OpenAI-compatible override:

- **Jurors:** `deepseek-v4.1-flash-1017` (DeepSeek), `minimax-m3.1-flash` (MiniMax), `mimo-v2.6-flash` (Xiaomi).
- **Builder:** `mimo-v2.6-pro` (Xiaomi).

**Disclosure:** the builder and one juror share a maker (Xiaomi). On the day we recorded, no fourth text-generating maker on that provider returned usable output. Several other models returned only reasoning with empty `content`, and the pre-flight gate excludes those. Every model reply is kept in `out/demo_models.jsonl`. The spend cap for the take ($0.50 of prepaid credit) is enforced by our own credit check, not by the provider.

## What we learned building it (tested against the real server, no fakes)

- Our first two builds failed because tests ran against a fake Switch server whose routes were made up. Everything here is tested against a real Switch 0.31.0 stack on `127.0.0.1`.
- SSE must be read line by line; a blocking `read(4096)` silently kills the stream.
- A juror that joins after the case was posted must catch up with `read_context`.
- Switch 0.31.0 can return HTTP 500 (`No incarnation drawn: a connection is opened inside a recorded move`) when several agents open streams at the same instant; the client retries 5xx within a 15 s budget and logs every absorbed retry.

## Files

| Path | What it is |
|---|---|
| `agents/switch_client.py` | Switch agent-protocol client: register, SSE events (line-by-line), 2 s heartbeat, reopen on 404/409, `read_context` catch-up, 5xx retry with evidence log |
| `agents/builder.py` | Reads `TASK:`, asks a model for a diff, runs tests in `docker --network none --read-only --pids-limit 128`, posts `CASE` |
| `agents/juror.py` | Reads `CASE:`, asks one model, posts `VERDICT/REASON/RISK` |
| `agents/foreperson.py` | Tallies UNANIMOUS / SPLIT (named dissent) / HUNG, with a `JSON:` line |
| `agents/merge_gate.py` | Only `JURY_MERGE_AUTHORIZED` humans decide; `git apply --check`; commits on `jury/<id>` |
| `accept/*.py` | Acceptance tests against the real stack (pinned in `accept/SHA256SUMS`) |
| `scripts/smoke.sh`, `scripts/run_demo.py`, `scripts/real_model_smoke.py` | Smoke, live demo, one-shot real-model check |
| `deploy/` | Pinned Switch fetch, resource caps (2 CPU / 4 GB total), preflight, up, teardown |

## Credits

Built by 3Dogs Nexus for Flip the Switch. Uses [Switch](https://github.com/sandbox-quantum/switch) by SandboxAQ (Apache-2.0 + Commons Clause), which is not included here; `deploy/fetch_switch.sh` downloads it. **License split:** Switch (fetched at commit `2d72c57596506e510bcfea5bb5b267a482403ed0`) is licensed Apache-2.0 + Commons Clause, and its exact license text is reproduced in [`LICENSE-switch.txt`](LICENSE-switch.txt). This repository's own code is Apache-2.0 and does not redistribute Switch. By default the juror models run on Nebius Token Factory; the demo video used Vultr Serverless Inference (see Demo video).

## License

Apache-2.0.
