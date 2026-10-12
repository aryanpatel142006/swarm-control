"""Git hooks the harness installs: a commit-msg hook that strips AI attribution trailers and an opt-in pre-push gate
for the main branch.

Why (HackRU, Oct 11-12 2026): workers and sub-agents kept writing `Claude-Session:` and `Co-Authored-By: Claude`
trailers although every prompt said not to (selective-hearing main 943a7157, af8074a0); the runner's scrub only
covered branches the harness pushes, never commits made in the main checkout. And direct pushes to main broke ruff
and the tests (RUF046 from aa36b460, test_judge_profile after 943a7157), which then failed every task's verify.

- `commit-msg` (installed into the repo's hooks dir, which every worktree shares) rewrites the message without AI
  attribution lines. It acts in linked worktrees (task, review and dispatch worktrees) always, and in the main
  checkout only when `git config swarm.stripTrailers.main true` (`swarm hooks install --main`). An existing hook is
  kept as `commit-msg.pre-swarm` and still runs after ours. `SWARM_KEEP_TRAILERS=1` turns it off for one commit.
- `pre-push` (opt-in: `swarm hooks install --pre-push [--verify]` or config `hooks.pre_push_verify`): a push to the
  main branch is refused when a pushed commit carries an AI trailer, or (with verify) when the configured fast verify
  fails on the pushed commit in a throwaway worktree. Task branches are never checked. `SWARM_SKIP_PREPUSH=1` or
  `git push --no-verify` skip it for an emergency fix.

The hooks are standalone Python 3 scripts with no import of this package: a Codex sandbox or a teammate's laptop
runs them without the harness on its path.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

HOOK_MARK = "swarm-control managed hook"
HOOK_VERSION = 1
CHAIN_SUFFIX = ".pre-swarm"

# Names of coding assistants whose co-author/assist lines are attribution, not people.
AI_NAMES = (r"claude|anthropic|openai|codex|chatgpt|gpt-\d|copilot|gemini|antigravity|cursor(?:agent)?|devin|"
            r"aider|windsurf|perplexity")
# One line each; matched against whole lines (the subject line is never removed).
AI_LINE_PATTERN = (
    r"[ \t]*(?:"
    rf"co-authored-by:[^\n]*(?:{AI_NAMES})[^\n]*"
    r"|(?:claude|codex|ai|agent)[- ]session(?:[- ]id)?:[^\n]*"
    rf"|(?:assisted|generated|written|created)-by:[^\n]*(?:{AI_NAMES})[^\n]*"
    r"|(?:\U0001f916[ \t]*)?generated (?:with|by) \[?(?:claude(?: code)?|codex|copilot|gemini|chatgpt)\b[^\n]*"
    r"|https://claude\.ai/code/session_\S*"
    r")[ \t]*")
_AI_LINE_RE = re.compile(AI_LINE_PATTERN, re.I)


def is_ai_line(line: str) -> bool:
    return bool(_AI_LINE_RE.fullmatch(line))


def strip_ai_attribution(text: str) -> str:
    """The message without AI co-author, session and "Generated with" lines; people's trailers stay. The first line
    (the subject) is never removed."""
    lines = (text or "").splitlines()
    kept = [ln.rstrip() for i, ln in enumerate(lines) if i == 0 or not is_ai_line(ln)]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept))
    return out.strip()


def has_ai_attribution(text: str) -> bool:
    return any(is_ai_line(ln) for ln in (text or "").splitlines()[1:])


# ----- hook scripts -----
_COMMON = r'''
import os, re, subprocess, sys
RX = re.compile(@@PATTERN@@, re.I)


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def strip(text):
    lines = text.splitlines()
    kept = [ln.rstrip() for i, ln in enumerate(lines) if i == 0 or not RX.fullmatch(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


def chain(name, argv, stdin=None):
    nxt = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + "@@CHAIN@@")
    if os.path.isfile(nxt) and os.access(nxt, os.X_OK):
        return subprocess.run([nxt, *argv], input=stdin, text=True).returncode
    return 0
'''

COMMIT_MSG_HOOK = r'''#!/usr/bin/env python3
# @@MARK@@ v@@VERSION@@: commit-msg. Strips AI attribution trailers (Claude-Session, AI Co-Authored-By,
# "Generated with ...") in linked worktrees, and in the main checkout when `git config swarm.stripTrailers.main true`.
# Rewritten by `swarm hooks install`; SWARM_KEEP_TRAILERS=1 skips it for one commit.
''' + _COMMON + r'''

def applies():
    if os.environ.get("SWARM_KEEP_TRAILERS") == "1":
        return False
    gd = git("rev-parse", "--absolute-git-dir")
    cd = git("rev-parse", "--git-common-dir")
    if gd and cd:
        cd = cd if os.path.isabs(cd) else os.path.join(os.getcwd(), cd)
        if os.path.realpath(gd) != os.path.realpath(cd):
            return True          # a linked worktree: task, review and dispatch worktrees
    return git("config", "--bool", "swarm.stripTrailers.main") == "true"


def main():
    path = sys.argv[1]
    if applies():
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        clean = strip(text)
        if clean.strip() != text.strip():
            with open(path, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(clean)
            sys.stderr.write("swarm: removed AI attribution lines from the commit message\n")
    return chain("commit-msg", sys.argv[1:])


sys.exit(main())
'''

PRE_PUSH_HOOK = r'''#!/usr/bin/env python3
# @@MARK@@ v@@VERSION@@: pre-push. A push to the main branch is refused when a pushed commit carries an AI
# attribution trailer or (when VERIFY is set) the fast verify fails on the pushed commit in a throwaway worktree.
# Rewritten by `swarm hooks install`; SWARM_SKIP_PREPUSH=1 (or git push --no-verify) skips it.
''' + _COMMON + r'''
import shutil, tempfile
MAIN = @@MAIN@@
VERIFY = @@VERIFY@@
SETUP = @@SETUP@@
TIMEOUT = @@TIMEOUT@@


def clean_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def pushed_range(local_sha, remote_sha):
    if set(remote_sha) != {"0"} and subprocess.run(["git", "cat-file", "-e", remote_sha + "^{commit}"],
                                                   capture_output=True).returncode == 0:
        return [f"{remote_sha}..{local_sha}"]
    return [local_sha, "--not", "--remotes"]


def trailer_hits(local_sha, remote_sha):
    out = subprocess.run(["git", "log", "--format=%H%x1f%B%x1e", *pushed_range(local_sha, remote_sha)],
                         capture_output=True, text=True).stdout
    hits = []
    for rec in out.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, body = rec.strip("\n").split("\x1f", 1)
        lines = body.splitlines()
        if any(RX.fullmatch(ln) for ln in lines[1:]):
            hits.append(f"{sha[:10]} {lines[0] if lines else ''}")
    return hits


def run_verify(sha):
    tmp = tempfile.mkdtemp(prefix="swarm-prepush-")
    wt = os.path.join(tmp, "wt")
    try:
        r = subprocess.run(["git", "worktree", "add", "-q", "--detach", wt, sha], capture_output=True, text=True)
        if r.returncode != 0:
            return 1, "could not create a worktree for the check: " + (r.stderr or r.stdout)
        env = clean_env()
        log = []
        for script in [s for s in (SETUP, VERIFY) if s]:
            if not os.path.exists(os.path.join(wt, script)):
                if script == VERIFY:
                    return 0, f"{VERIFY} is not in the pushed commit; nothing to check"
                continue
            try:
                p = subprocess.run(["bash", script], cwd=wt, env=env, capture_output=True, text=True,
                                   timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                return 1, f"{script} took longer than {TIMEOUT} s"
            log.append(p.stdout + p.stderr)
            if p.returncode != 0:
                return p.returncode, "\n".join(log)
        return 0, "\n".join(log)
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", wt], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    stdin = sys.stdin.read()
    if os.environ.get("SWARM_SKIP_PREPUSH") == "1":
        sys.stderr.write("swarm pre-push: skipped (SWARM_SKIP_PREPUSH=1)\n")
        return chain("pre-push", sys.argv[1:], stdin)
    for line in stdin.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref != "refs/heads/" + MAIN or set(local_sha) == {"0"}:
            continue
        hits = trailer_hits(local_sha, remote_sha)
        if hits:
            sys.stderr.write("swarm pre-push: refusing to push AI attribution trailers to " + MAIN + ":\n  "
                             + "\n  ".join(hits[:10]) + "\nReword them (`git rebase -i` and edit, or "
                             "`git commit --amend` for the last one); SWARM_SKIP_PREPUSH=1 overrides.\n")
            return 1
        if VERIFY:
            sys.stderr.write(f"swarm pre-push: running {VERIFY} on {local_sha[:10]} before it reaches {MAIN}...\n")
            rc, out = run_verify(local_sha)
            if rc != 0:
                tail = "\n".join(out.splitlines()[-40:])
                sys.stderr.write(f"{tail}\nswarm pre-push: {VERIFY} failed (rc={rc}) on {local_sha[:10]}; main would "
                                 "go red for every task. Fix it first; SWARM_SKIP_PREPUSH=1 overrides.\n")
                return 1
    return chain("pre-push", sys.argv[1:], stdin)


sys.exit(main())
'''


def _render(template: str, **values) -> str:
    out = (template.replace("@@MARK@@", HOOK_MARK).replace("@@VERSION@@", str(HOOK_VERSION))
           .replace("@@PATTERN@@", repr(AI_LINE_PATTERN)).replace("@@CHAIN@@", CHAIN_SUFFIX))
    for k, v in values.items():
        out = out.replace(f"@@{k}@@", repr(v))
    return out


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def hooks_dir(repo: Path | str) -> Path:
    """The directory git runs hooks from for this repo and all its worktrees: core.hooksPath when set, else
    <common git dir>/hooks."""
    repo = Path(repo)
    custom = _git(repo, "config", "core.hooksPath").stdout.strip()
    if custom:
        p = Path(os.path.expanduser(custom))
        top = _git(repo, "rev-parse", "--show-toplevel").stdout.strip() or str(repo)
        return p if p.is_absolute() else Path(top) / p
    common = _git(repo, "rev-parse", "--git-common-dir").stdout.strip()
    if not common:
        raise RuntimeError(f"{repo} is not a git repository")
    cp = Path(common)
    return (cp if cp.is_absolute() else (repo / cp)).resolve() / "hooks"


def _write_hook(d: Path, name: str, text: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    if path.exists() and HOOK_MARK not in path.read_text(errors="ignore"):
        chained = d / (name + CHAIN_SUFFIX)
        if not chained.exists():
            path.rename(chained)            # keep the project's own hook; ours runs it after
        else:
            raise RuntimeError(f"{path} is not ours and {chained.name} already exists; merge them by hand")
    if path.exists() and path.read_text(errors="ignore") == text:
        return
    tmp = d / f".{name}.swarm-tmp"
    tmp.write_text(text)
    tmp.chmod(0o755)
    tmp.replace(path)


def install_hooks(repo: Path | str, *, main: bool | None = None, pre_push: bool = False,
                  pre_push_verify: str | None = None, setup: str | None = None, main_branch: str = "main",
                  timeout_s: int = 1800) -> Path:
    """Install (or refresh) the commit-msg hook; `main` sets whether it also acts in the main checkout (None leaves
    the setting as it is). `pre_push` or a `pre_push_verify` command installs the pre-push gate for `main_branch`.
    Idempotent; returns the hooks dir."""
    repo = Path(repo)
    d = hooks_dir(repo)
    _write_hook(d, "commit-msg", _render(COMMIT_MSG_HOOK))
    if main is not None:
        _git(repo, "config", "swarm.stripTrailers.main", "true" if main else "false")
    if pre_push or pre_push_verify:
        _write_hook(d, "pre-push", _render(PRE_PUSH_HOOK, MAIN=main_branch, VERIFY=pre_push_verify or "",
                                           SETUP=setup or "", TIMEOUT=int(timeout_s)))
    return d


_installed: set[str] = set()


def ensure_hooks(repo: Path | str, **kwargs) -> bool:
    """install_hooks once per process and repo (the runner calls it before every worktree); never raises."""
    key = str(Path(repo).resolve())
    if key in _installed:
        return True
    try:
        install_hooks(repo, **kwargs)
    except Exception:  # noqa: BLE001 - a hook problem must never stop a run; doctor reports it
        return False
    _installed.add(key)
    return True


def hook_status(repo: Path | str) -> dict:
    """Which of our hooks are installed and current: {"dir", "commit-msg", "pre-push", "pre-push-verify", "main"}."""
    d = hooks_dir(repo)

    def ours(name: str) -> bool:
        p = d / name
        try:
            return p.is_file() and f"{HOOK_MARK} v{HOOK_VERSION}" in p.read_text(errors="ignore") and \
                os.access(p, os.X_OK)
        except OSError:
            return False
    pp = d / "pre-push"
    verify = ""
    if ours("pre-push"):
        m = re.search(r"^VERIFY = (.+)$", pp.read_text(errors="ignore"), re.M)
        verify = m.group(1).strip().strip("'\"") if m else ""
    return {"dir": str(d), "commit-msg": ours("commit-msg"), "pre-push": ours("pre-push"),
            "pre-push-verify": verify,
            "main": _git(Path(repo), "config", "--bool", "swarm.stripTrailers.main").stdout.strip() == "true"}


def trailer_commits(repo: Path | str, rev_range: str, cap: int = 200) -> list[tuple[str, str]]:
    """(short sha, subject) of commits in `rev_range` whose message carries AI attribution lines."""
    r = _git(Path(repo), "log", f"-{cap}", "--format=%H%x1f%B%x1e", rev_range)
    hits = []
    for rec in r.stdout.split("\x1e"):
        if "\x1f" not in rec:
            continue
        sha, body = rec.strip("\n").split("\x1f", 1)
        if has_ai_attribution(body):
            hits.append((sha[:10], (body.splitlines() or [""])[0]))
    return hits
