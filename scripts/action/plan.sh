#!/usr/bin/env bash
set -euo pipefail
if [ -z "$BASE" ]; then
  echo "::error::plan needs a base branch: run it on pull_request, or set the base input"
  exit 1
fi
# The base branch as it is now, even from a shallow checkout.
git fetch --no-tags --depth=1 origin "+refs/heads/$BASE:refs/remotes/origin/$BASE"
# Split on spaces and new lines only. exacly expands patterns such as spec/exacly/*.yaml itself, so that
# the plan matches them against each revision's files, including files the change deleted.
set -f
# shellcheck disable=SC2206
paths=($MODEL)
models=()
for path in "${paths[@]}"; do models+=(-m "$path"); done
uv run --project "$ACTION_PATH" --no-dev --no-sync \
  exacly view plan --base "origin/$BASE" "${models[@]}" > "$RUNNER_TEMP/exacly-plan.md"
cat "$RUNNER_TEMP/exacly-plan.md" >> "$GITHUB_STEP_SUMMARY"
