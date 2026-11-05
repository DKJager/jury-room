#!/usr/bin/env bash
# Jury Room end-to-end smoke on a REAL Switch stack (no fakes). Every line of output is computed, never echoed as a
# fixed pass. Exit code 0 only if every step passed.
#   1. preflight (docker up, disk, firewall rule)      2. Switch health 200 on 127.0.0.1
#   3. slice 1: live_switch.py   4. slice 2: live_jury.py   5. slice 3: live_e2e.py (sandbox + human merge gate)
#   6. optional (--real-models): one real call per juror model, capped, logged to out/real_model_smoke.jsonl
set -u
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
BASE="${SWITCH_BASE_URL:-http://127.0.0.1:8000}"
fail=0
# repo-relative defaults so a fresh clone works (the pinned tests read these from the environment)
export SWITCH_ENV_FILE="${SWITCH_ENV_FILE:-$PWD/deploy/.env}"
export JURY_GATE_LOG="${JURY_GATE_LOG:-$PWD/out/merge_gate_log.jsonl}"
export JURY_UPSTREAM_EVIDENCE="${JURY_UPSTREAM_EVIDENCE:-$PWD/out/upstream_evidence.jsonl}"
EVIDENCE="${JURY_TEST_EVIDENCE:-out/test_evidence.jsonl}"; mkdir -p "$(dirname "$EVIDENCE")"
# [#2480] the acceptance tests are pinned: smoke refuses to run if any of them changed
check_pins() { [ -f accept/SHA256SUMS ] && (cd accept && sha256sum -c --quiet SHA256SUMS) && echo "acceptance tests match accept/SHA256SUMS"; }
log() { printf '{"ts":%s,"step":"%s","result":"%s","seconds":%s}\n' "$(date +%s)" "$1" "$2" "$3" >> "$EVIDENCE"; }
step() { local name="$1"; shift; local t0=$(date +%s); if out=$("$@" 2>&1); then echo "ok   $name ($(( $(date +%s)-t0 ))s) $(echo "$out" | tail -1)"; log "$name" PASS $(( $(date +%s)-t0 )); else echo "FAIL $name ($(( $(date +%s)-t0 ))s): $(echo "$out" | grep -v '^WARN' | tail -3 | tr '\n' ' ')"; log "$name" FAIL $(( $(date +%s)-t0 )); fail=1; fi; }
step pins check_pins
if [ $fail != 0 ]; then echo "SMOKE: refused, an acceptance test does not match its pinned hash"; exit 2; fi
step preflight bash deploy/preflight.sh
step health bash -c "code=\$(curl -s -o /dev/null -w '%{http_code}' $BASE/health); echo \"health \$code\"; [ \"\$code\" = 200 ]"
for t in live_switch live_jury live_e2e; do
  step "$t" bash -c "$PY accept/$t.py | tail -1 | grep -qx PASS && echo PASS"
done
if [ "${1:-}" = "--real-models" ]; then
  step real_models "$PY" scripts/real_model_smoke.py
fi
[ $fail = 0 ] && echo "SMOKE: all steps passed" || echo "SMOKE: FAILED"
exit $fail
