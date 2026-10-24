#!/usr/bin/env bash
set -euo pipefail
plan="$RUNNER_TEMP/exacly-plan.md"
comment="$(gh api --paginate "repos/$GITHUB_REPOSITORY/issues/$PR/comments" \
  --jq '.[] | select(.user.login == "github-actions[bot]" and (.body | startswith("<!-- exacly-plan -->"))) | .id' \
  | head -n 1)"
if [ -n "$comment" ]; then
  gh api -X PATCH "repos/$GITHUB_REPOSITORY/issues/comments/$comment" -F "body=@$plan" > /dev/null
else
  gh api "repos/$GITHUB_REPOSITORY/issues/$PR/comments" -F "body=@$plan" > /dev/null
fi
