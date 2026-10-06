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
# Generated data must outlive the worktree and be shared by every task: fixtures took minutes to regenerate in
# each worktree and benchmark results vanished with the worktree that made them (selective-hearing, Oct 5 2026).
# Each dir lives in the main checkout and is linked here. List each one in .gitignore with AND without the
# trailing slash (the slash form does not match a symlink). Code that writes there should also honour an env var.
SHARED="${SWARM_SHARED_DIRS:-eval/fixtures/generated eval/results}"
for d in $SHARED; do
  mkdir -p "$MAIN/$d"
  if [ ! -e "$d" ]; then
    mkdir -p "$(dirname "$d")" && ln -s "$MAIN/$d" "$d"
  fi
done
