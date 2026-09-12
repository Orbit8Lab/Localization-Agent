#!/usr/bin/env bash
# End-to-end check against a RUNNING server. Works against localhost or
# a deployed URL — the same script is the post-deploy verification.
#
#   ./scripts/smoke_api.sh                         # http://127.0.0.1:8000
#   ./scripts/smoke_api.sh https://x.fly.dev       # deployed
#   BASIC_AUTH=user:pass ./scripts/smoke_api.sh https://x.fly.dev
#
# Exits non-zero on the first failure so it can gate a deploy.
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
CURL=(curl -sS --max-time 30)
[ -n "${BASIC_AUTH:-}" ] && CURL+=(-u "$BASIC_AUTH")

pass=0; fail=0
check() {  # check <label> <actual> <expected>
    if [ "$2" = "$3" ]; then printf '  ✓ %-46s %s\n' "$1" "$2"; pass=$((pass+1))
    else printf '  ✗ %-46s got %s, want %s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
code() { "${CURL[@]}" -o /dev/null -w '%{http_code}' "$@"; }

work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
cat > "$work/strings.json" <<'JSON'
{"UI_START": "开始游戏", "UI_QUIT": "退出", "ITEM_SWORD": "铁剑"}
JSON

echo "→ $BASE"
echo
echo "health"
check "GET /health" "$(code "$BASE/health")" "200"

echo
echo "create"
# A 401 here means credentials are needed; say so instead of dying on a
# KeyError, because "no BASIC_AUTH set" is the likeliest reason a deploy
# check fails and the traceback hides it.
authcode=$(code "$BASE/jobs")
if [ "$authcode" = "401" ] && [ -z "${BASIC_AUTH:-}" ]; then
    echo "  ✗ server requires auth; re-run with BASIC_AUTH=user:password" >&2
    exit 1
fi

resp=$("${CURL[@]}" -X POST "$BASE/jobs" \
    -F "file=@$work/strings.json" \
    -F "game=Smoke Test" -F "source_lang=zh" -F "target_locales=en" \
    -F "tenant_id=smoke" -F "autostart=false")
jid=$(printf '%s' "$resp" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("job_id",""))' \
    2>/dev/null || true)
if [ -z "$jid" ]; then
    echo "  ✗ POST /jobs did not return a job_id. Response was:" >&2
    printf '    %s\n' "$resp" >&2
    exit 1
fi
check "POST /jobs returns a job_id" "$([ -n "$jid" ] && echo yes)" "yes"
check "stage derived as INTAKE" \
    "$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["stage"]["phase"])')" \
    "INTAKE"
check "tenant_id recorded" \
    "$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["tenant_id"])')" \
    "smoke"

echo
echo "read"
check "GET /jobs/{id}" "$(code "$BASE/jobs/$jid")" "200"
check "GET /jobs (list)" "$(code "$BASE/jobs")" "200"
check "GET /jobs/{id}/artifacts" "$(code "$BASE/jobs/$jid/artifacts")" "200"
check "intake artifact downloads" "$(code "$BASE/jobs/$jid/artifacts/0/intake")" "200"

echo
echo "refusals (these MUST fail closed)"
check "unknown job -> 404" "$(code "$BASE/jobs/does-not-exist")" "404"
check "path traversal in job id" "$(code "$BASE/jobs/..%2F..%2Fetc")" "404"
check "traversal in artifact name" "$(code "$BASE/jobs/$jid/artifacts/0/..%2Fjob")" "404"
check "missing artifact -> 404" "$(code "$BASE/jobs/$jid/artifacts/5/nothing")" "404"
check "wrong gate -> 409" \
    "$(code -X POST "$BASE/jobs/$jid/approve" -H 'Content-Type: application/json' \
         -d '{"gate":"G3","by":"smoke"}')" "409"
check "duplicate job_id -> 409" \
    "$(code -X POST "$BASE/jobs" -F "file=@$work/strings.json" -F "game=X" \
         -F "source_lang=zh" -F "target_locales=en" -F "job_id=$jid")" "409"
# --form-string, not -F: curl DROPS a -F field whose value is only
# whitespace, so the server sees it missing (422) rather than empty (400).
check "no valid locale -> 400" \
    "$(code -X POST "$BASE/jobs" -F "file=@$work/strings.json" -F "game=X" \
         -F "source_lang=zh" --form-string "target_locales= ")" "400"
check "locale list of only commas -> 400" \
    "$(code -X POST "$BASE/jobs" -F "file=@$work/strings.json" -F "game=X" \
         -F "source_lang=zh" -F "target_locales=,,")" "400"
check "missing required field -> 422" \
    "$(code -X POST "$BASE/jobs" -F "file=@$work/strings.json" -F "game=X" \
         -F "source_lang=zh")" "422"

echo
echo "lifecycle (background run must stop at a gate, not push through)"
"${CURL[@]}" -o /dev/null -X POST "$BASE/jobs/$jid/run"
gate=""; for _ in $(seq 1 30); do
    sleep 1
    gate=$("${CURL[@]}" "$BASE/jobs/$jid" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["stage"]["gate"] or "")')
    [ -n "$gate" ] && break
done
check "stops at gate G0" "$gate" "G0"

"${CURL[@]}" -o /dev/null -X POST "$BASE/jobs/$jid/approve" \
    -H 'Content-Type: application/json' -d '{"gate":"G0","by":"smoke"}'
gate=""; for _ in $(seq 1 40); do
    sleep 1
    gate=$("${CURL[@]}" "$BASE/jobs/$jid" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin)["stage"]["gate"] or "")')
    [ "$gate" = "G1" ] && break
done
check "G0 approved -> advances to G1" "$gate" "G1"

echo
echo "──────────────────────────────────────────────"
printf '  %d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ] || exit 1
