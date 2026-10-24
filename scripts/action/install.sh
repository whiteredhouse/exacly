#!/usr/bin/env bash
set -euo pipefail
case "$MODE" in
  check | plan) ;;
  *) echo "::error::mode is check or plan, not $MODE"; exit 1 ;;
esac
uv sync --project "$ACTION_PATH" --locked --no-dev --quiet
