"""The last shell commands of a cut-off Claude run, read from its session transcript, for the next attempt.

Q-183 (T-075, Oct 6 2026): attempt 1 hit max_turns without writing `.swarm-run/notes.md`, so its iLab measurements
(job id, RTT, remote replay timings) were lost; only commit messages survived. Claude Code keeps every session as
JSON lines under `<config dir>/projects/<cwd with / and . as ->/<session id>.jsonl`; when the notes are empty the
runner keeps the tail of that transcript (each Bash command with the end of its output) beside the attempt's logs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DIGEST_COMMANDS = 25
OUTPUT_TAIL = 400
DIGEST_CAP = 6000


def project_dir_name(cwd: Path | str) -> str:
    return "".join(c if c.isalnum() or c == "-" else "-" for c in str(cwd))


def find_transcript(session_id: str, cwd: Path | str, config_dirs: list[Path] | None = None) -> Path | None:
    if not session_id:
        return None
    dirs = list(config_dirs or [])
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        dirs.append(Path(os.environ["CLAUDE_CONFIG_DIR"]))
    dirs.append(Path.home() / ".claude")
    for base in dict.fromkeys(dirs):
        direct = base / "projects" / project_dir_name(cwd) / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
        projects = base / "projects"
        if projects.is_dir():
            for hit in projects.glob(f"*/{session_id}.jsonl"):
                return hit
    return None


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def bash_digest(path: Path, *, last: int = DIGEST_COMMANDS, tail: int = OUTPUT_TAIL, cap: int = DIGEST_CAP) -> str:
    """Markdown list of the last `last` Bash commands in the transcript, each with the tail of its output."""
    uses: dict[str, str] = {}
    order: list[str] = []
    results: dict[str, str] = {}
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                content = (d.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") == "Bash":
                        uses[b.get("id", "")] = str((b.get("input") or {}).get("command", ""))
                        order.append(b.get("id", ""))
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                        results[b["tool_use_id"]] = _result_text(b.get("content"))
    except OSError:
        return ""
    parts = []
    for uid in order[-last:]:
        cmd = uses[uid].strip()
        out = results.get(uid, "(no output recorded: still running when the run stopped?)").strip()
        if len(out) > tail:
            out = "…" + out[-tail:]
        parts.append(f"$ {cmd[:600]}\n{out}")
    text = "\n\n".join(parts)
    if len(text) > cap:
        text = "…\n" + text[-cap:]
    return text
