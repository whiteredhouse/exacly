#!/usr/bin/env bash
set -euo pipefail
# Split on spaces and new lines only. exacly expands patterns such as spec/exacly/*.yaml itself, so that
# the plan matches them against each revision's files, including files the change deleted.
set -f
# shellcheck disable=SC2206
paths=($MODEL)
report="$RUNNER_TEMP/exacly-report.txt"
status=0
uv run --project "$ACTION_PATH" --no-dev --no-sync exacly validate "${paths[@]}" > "$report" || status=$?
cat "$report"
{ echo '### Exacly validation'; echo; echo '```'; cat "$report"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
exit "$status"
