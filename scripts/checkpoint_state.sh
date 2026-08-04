#!/usr/bin/env bash
set -euo pipefail

label="${1:-checkpoint}"
state_path="${2:-state/state.json}"
max_attempts="${CHECKPOINT_MAX_ATTEMPTS:-3}"
network_timeout="${CHECKPOINT_GIT_TIMEOUT_SECONDS:-12}"
backoff_seconds="${CHECKPOINT_BACKOFF_SECONDS:-1}"
git_command="${CHECKPOINT_GIT_COMMAND:-git}"

git_exec() {
  "$git_command" "$@"
}

git_dir="$(git_exec rev-parse --absolute-git-dir)"
basis_file="${git_dir}/wiesn-monitor-writer-base"

# The workflow supplies the checkout revision explicitly.  The fallback keeps
# the helper usable in a fresh local clone and is captured before we create the
# checkpoint commit.
if [ -f "$basis_file" ]; then
  writer_base="$(tr -d '\r\n' < "$basis_file")"
else
  writer_base="${MONITOR_WRITER_BASE:-$(git_exec rev-parse HEAD)}"
  printf '%s\n' "$writer_base" > "$basis_file"
fi

if ! git_exec cat-file -e "${writer_base}^{commit}" 2>/dev/null; then
  echo "invalid state-writer basis: ${writer_base}" >&2
  exit 1
fi

git_exec add -- "$state_path"
if git_exec diff --cached --quiet; then
  exit 0
fi

git_exec commit -m "state: ${label} $(date -u +%Y-%m-%dT%H:%M:%SZ)"

local_head="$(git_exec rev-parse HEAD)"
if ! git_exec merge-base --is-ancestor "$writer_base" "$local_head"; then
  echo "local checkpoint is not descended from writer basis; refusing push" >&2
  exit 1
fi

network_git() {
  # Both transport operations are bounded.  A timed-out push has an uncertain
  # result, so the following fetch checks whether that exact local commit was
  # accepted before doing anything else.
  timeout --signal=TERM --kill-after=1 "${network_timeout}s" "$git_command" "$@"
}

for attempt in $(seq 1 "$max_attempts"); do
  if ! network_git fetch --no-tags origin main; then
    echo "state checkpoint fetch failed on attempt ${attempt}" >&2
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep $(( backoff_seconds * attempt ))
    fi
    continue
  fi

  remote_head="$(git rev-parse FETCH_HEAD)"
  if [ "$remote_head" = "$local_head" ]; then
    printf '%s\n' "$local_head" > "$basis_file"
    echo "state checkpoint already present after uncertain push"
    exit 0
  fi

  if [ "$remote_head" != "$writer_base" ]; then
    echo "remote main changed since writer basis; refusing stale state push" >&2
    echo "writer_base=${writer_base} remote_head=${remote_head}" >&2
    exit 1
  fi

  if network_git push origin "${local_head}:main"; then
    printf '%s\n' "$local_head" > "$basis_file"
    echo "state checkpoint pushed on attempt ${attempt}"
    exit 0
  fi

  echo "state checkpoint push failed on attempt ${attempt}" >&2
  if [ "$attempt" -lt "$max_attempts" ]; then
    sleep $(( backoff_seconds * attempt ))
  fi
done

echo "state checkpoint failed after ${max_attempts} attempts" >&2
exit 1
