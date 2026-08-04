#!/usr/bin/env bash
set -euo pipefail

label="${1:-checkpoint}"
state_path="${2:-state/state.json}"

git add -- "$state_path"
if git diff --cached --quiet; then
  exit 0
fi

git commit -m "state: ${label} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
for attempt in 1 2 3; do
  if git push origin HEAD:main; then
    echo "state checkpoint pushed on attempt ${attempt}"
    exit 0
  fi
  git fetch origin main
  if ! git rebase origin/main; then
    git rebase --abort || true
    echo "state conflict: refusing to overwrite remote history" >&2
    exit 1
  fi
done

echo "state push failed after 3 attempts" >&2
exit 1
