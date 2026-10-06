from pathlib import Path

from swarm.selfupdate import check_and_update


class R:
    def __init__(self, ok=True, out=""):
        self.ok, self.out, self.err = ok, out, ""


def _runner(responses):
    calls = []

    def run(args, cwd, timeout=60):
        calls.append(args)
        key = " ".join(args[1:3])
        return responses.get(key, R())
    return run, calls


def test_updates_when_upstream_is_ahead(tmp_path):
    run, calls = _runner({"rev-parse HEAD": R(out="aaa\n"), "rev-parse @{u}": R(out="bbb\n"),
                          "merge-base --is-ancestor": R(ok=False), "rev-parse --short": R(out="bbb1234\n")})
    assert check_and_update(tmp_path, run=run) == "bbb1234"
    assert any(a[:3] == ["git", "merge", "--ff-only"] for a in calls)


def test_no_update_when_current_dirty_or_offline(tmp_path):
    run, _ = _runner({"rev-parse HEAD": R(out="aaa\n"), "rev-parse @{u}": R(out="aaa\n")})
    assert check_and_update(tmp_path, run=run) is None
    run, _ = _runner({"status --porcelain": R(out=" M swarm/x.py\n"), "rev-parse HEAD": R(out="aaa\n"),
                      "rev-parse @{u}": R(out="bbb\n")})
    assert check_and_update(tmp_path, run=run) is None
    run, _ = _runner({"fetch -q": R(ok=False)})
    assert check_and_update(tmp_path, run=run) is None
    assert check_and_update(None, run=run) is None or True   # no harness repo: never raises


def test_no_update_when_local_is_ahead_of_upstream(tmp_path):
    """A checkout with unpushed commits: `merge --ff-only @{u}` is a no-op that succeeds, and the runner used to
    re-exec itself every check (the test suite re-ran itself forever from such a worktree, Oct 6 2026)."""
    run, calls = _runner({"rev-parse HEAD": R(out="aaa\n"), "rev-parse @{u}": R(out="bbb\n"),
                          "merge-base --is-ancestor": R(ok=True)})
    assert check_and_update(tmp_path, run=run) is None
    assert not any(a[:3] == ["git", "merge", "--ff-only"] for a in calls)
