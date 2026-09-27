#!/usr/bin/env bash
# Fake coding agent for tests. usage: fake_agent.sh <prompt_file> <model> <cwd>
# Behaviour is keyed off the task title found in the prompt:
#   *BLOCKME*  -> writes a blocked report with a blocking question (until a human answer appears in the prompt)
#   *FAILME*   -> exits 1 the first time (per task id, tracked in $FAKE_STATE_DIR), succeeds after
#   otherwise  -> creates src/<id>.txt and a done report
# When the prompt is a review, writes an approve verdict. When it is a planning request, writes no tasks.
set -e
prompt="$1"; model="$2"; cwd="$3"
cd "$cwd"
mkdir -p .swarm-run
if grep -q "^# Review of" "$prompt"; then
  echo '{"verdict":"approve","summary":"fake approve","findings":[]}' > .swarm-run/review.json
  exit 0
fi
if grep -q "^# Planning request" "$prompt"; then
  echo '{"tasks":[]}' > .swarm-run/plan.json
  exit 0
fi
title=$(grep -m1 '^- Title:' "$prompt" | sed 's/^- Title: //')
id=$(grep -m1 '^- ID:' "$prompt" | sed 's/^- ID: //')
state="${FAKE_STATE_DIR:-/tmp}/fake-$id"
case "$title" in
  *BLOCKME*)
    if ! grep -q "Human answer" "$prompt"; then
      cat > .swarm-run/report.json <<EOF
{"status":"blocked","summary":"need input","question":{"kind":"blocking","text":"which way?","options":["a","b"]}}
EOF
      exit 0
    fi;;
  *FAILME*)
    if [ ! -f "$state" ]; then touch "$state"; echo "boom" >&2; exit 1; fi;;
esac
mkdir -p src
echo "// $id $title ($model)" >> "src/${id}.txt"
cat > .swarm-run/report.json <<EOF
{"status":"done","summary":"implemented $title","files_changed":["src/${id}.txt"],
 "decisions":[{"decision":"fake decision for $id","why":"test","impact":"low"}]}
EOF
