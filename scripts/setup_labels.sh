#!/usr/bin/env bash
# Create the shared label set in both repos.
#
# Both, identically: an issue moved between repos keeps its labels only
# if the destination defines them. A label that exists in one repo and
# not the other silently drops on transfer.
#
#   ./scripts/setup_labels.sh            # both repos
#   ./scripts/setup_labels.sh --dry-run
set -euo pipefail

REPOS=("Orbit8Lab/Localization-Agent" "Orbit8Lab/orbit8-web")
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

# name|colour|description
LABELS=(
  "type:bug|d73a4a|Did something other than what it promised"
  "type:feature|a2eeef|Never promised this, and it should"
  "type:content|fbca04|Glossary or style-rule decision, not code"
  "type:docs|0075ca|Behaved correctly, explained itself badly"
  "type:question|d876e3|Not yet known whether this is a defect"

  "layer:ui|c2e0c6|Console: rendering, labels, buttons"
  "layer:api|bfd4f2|HTTP surface: status codes, auth, upload"
  "layer:agent|e99695|Assistant: wrong claim, wrong tool, loop"
  "layer:pipeline|f9d0c4|Translation or LQA output"
  "layer:infra|d4c5f9|Deploy, Fly, Vercel, secrets, volume"

  "sev:1-silent-wrong|b60205|Wrong with no error, or a check that did not run"
  "sev:2-blocked|d93f0b|Cannot complete the work"
  "sev:3-wrong-visible|e58326|Wrong but visible and workaroundable"
  "sev:4-friction|fef2c0|Confusing, slow, or unclear"

  "needs-triage|ededed|Not yet sorted"
  "needs-info|f0ad4e|Waiting on the reporter"
  "blocked|000000|Waiting on something else"
  "good-first-issue|7057ff|Small and self-contained"
)

for repo in "${REPOS[@]}"; do
  echo "→ $repo"
  for entry in "${LABELS[@]}"; do
    IFS='|' read -r name colour desc <<< "$entry"
    if [ -n "$DRY" ]; then
      printf '   would create %-22s %s\n' "$name" "$desc"
      continue
    fi
    # --force so re-running updates colour/description instead of failing.
    gh label create "$name" --repo "$repo" --color "$colour" \
       --description "$desc" --force >/dev/null
    printf '   %-22s ok\n' "$name"
  done
done

echo
echo "Done. Next:"
echo "  1. Create an ORG-level project (not repo-level) so it spans both repos."
echo "  2. gh auth refresh -s project   # the current token lacks this scope"
