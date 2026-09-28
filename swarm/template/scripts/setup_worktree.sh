#!/usr/bin/env bash
# Called once per fresh worktree. Link heavy dependencies from the main checkout instead of reinstalling.
set -euo pipefail
cd "$(dirname "$0")/.."
MAIN="$(git rev-parse --path-format=absolute --git-common-dir)/.."
for d in frontend/node_modules backend/node_modules .venv; do
  if [ -d "$MAIN/$d" ] && [ ! -e "$d" ]; then
    mkdir -p "$(dirname "$d")" && ln -s "$MAIN/$d" "$d"
  fi
done
