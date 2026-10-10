from swarm.feedback import (DEFAULT_PLACEHOLDER_FILES, DEFAULT_PLACEHOLDER_PATTERNS, fenced_lines, placeholder_feedback,
                            placeholder_hits, reconcile_sync_feedback, split_verify_output, strip_legacy_rebase,
                            verify_feedback)

PROGRESS = "\n".join("." * 72 + f" [{p:3d}%]" for p in range(4, 101, 4))
PYTEST_PASS = (PROGRESS + "\n=============================== warnings summary ===============================\n"
               "../.venv/lib/python3.12/site-packages/fastapi/testclient.py:1\n  StarletteDeprecationWarning: ...\n\n"
               "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
               "834 passed, 34 deselected, 1 warning in 50.08s")
RUFF = ("verify_fast: /x/.venv/bin/python\nruff: FAIL\n"
        "hearing/server/session.py:412:5: F841 Local variable `spec` is assigned to but never used\n"
        "eval/synth_feed_bench.py:3:8: F401 [*] `os` imported but unused\nFound 2 errors.")


def test_verify_feedback_puts_the_failing_lint_step_before_a_passing_pytest_tail():
    """Q-168 (T-074): the feedback was the pytest tail only ("834 passed"); the ruff errors were cut off."""
    out = RUFF + "\n" + PYTEST_PASS
    assert len(out) > 1800                     # the old [-1800:] cut lost the ruff lines
    fb = verify_feedback(out, script="scripts/verify_fast.sh", code=1)
    assert fb.startswith("scripts/verify_fast.sh failed (exit 1).")
    assert "F841 Local variable `spec`" in fb and "F401 [*] `os` imported but unused" in fb
    assert "The tests passed" in fb
    assert fb.index("F841") < fb.index("834 passed")
    assert len(fb) < 2000                      # the passing pytest section is capped on its own


def test_verify_feedback_shows_a_pytest_failure_first_and_keeps_the_lint_section():
    failing = ("..F\n=================================== FAILURES ===================================\n"
               "____ test_feed ____\n    assert feed == 'focus'\nE   AssertionError\n"
               "=========================== short test summary info ============================\n"
               "FAILED tests/test_feed.py::test_feed - AssertionError\n1 failed, 2 passed in 0.31s")
    other, pytest_part, passed = split_verify_output(RUFF + "\n" + failing)
    assert passed is False and "ruff: FAIL" in other and "FAILED tests/test_feed.py" in pytest_part
    fb = verify_feedback(RUFF + "\n" + failing, code=1)
    assert fb.index("pytest failed:") < fb.index("F841")
    assert "AssertionError" in fb


def test_verify_feedback_without_pytest_keeps_head_and_tail():
    out = "step one FAIL: bad thing\n" + ("noise\n" * 2000) + "exit summary"
    fb = verify_feedback(out, code=2)
    assert "step one FAIL: bad thing" in fb and "exit summary" in fb and "characters cut" in fb


OLD_MERGER = ("Main moved and now conflicts with this branch in: docs/DEMO.md (main changed them in: a6b7f60 T-073 · "
              "Demo runbook refresh (#65)). Before your next run the harness merges origin/main into your branch and "
              "leaves conflict markers in those files. Resolve them keeping main's intent and this task's change, "
              "`git add` them and `git commit`. Do not run `git rebase`, `git fetch` or `git merge` yourself.")
OLD_REBASE = ("Rebase onto main conflicted in: docs/DEMO.md (main changed them in: a6b7f60 T-073 · Demo runbook "
              "refresh (#65)). First run `git fetch origin && git rebase origin/main`, resolve every conflict keeping "
              "main's intent, `git rebase --continue`, then do the task.")
REVIEW = ("Reviewer requested changes: synth_feed can raise KeyError.\n"
          "- [high] hearing/server/session.py: guard the lookup. → Only run it in SYNTH_MODES.\n"
          "- [low] hearing/synth/tts.py: add a test.")


def test_clean_merge_drops_stale_conflict_and_rebase_instructions():
    """Q-164/Q-166 (T-070): the prompt held both the merger's "markers will be left" and an older "First run
    `git fetch origin && git rebase origin/main`"; the worktree was clean, so neither was true."""
    fb = reconcile_sync_feedback("\n\n".join([REVIEW, OLD_MERGER, OLD_REBASE]), synced=True, conflicts=[], markers=[])
    assert "git rebase origin/main" not in fb and "git fetch origin &&" not in fb and "rebase --continue" not in fb
    assert "Main moved and now conflicts" not in fb and "Rebase onto main conflicted" not in fb
    assert "without conflicts; there are no conflict markers" in fb
    assert REVIEW in fb                         # the reviewer's list keeps its line breaks


def test_conflicted_merge_keeps_one_instruction_the_prompt_section():
    fb = reconcile_sync_feedback(OLD_MERGER + "\n\n" + OLD_REBASE, synced=True, conflicts=["docs/DEMO.md"],
                                 markers=["docs/DEMO.md"])
    assert fb == ""                             # the Merge conflicts section of the prompt says what to do


def test_without_a_sync_only_legacy_rebase_sentences_go():
    relay = ("Message from T-061 (claude-a): I changed tiers.py. First run `git fetch origin && git rebase "
             "origin/main` to pick it up.")
    fb = reconcile_sync_feedback(OLD_MERGER + "\n\n" + relay, synced=False, conflicts=[], markers=[])
    assert OLD_MERGER in fb                     # we did not merge, so we cannot say it is stale
    assert "Message from T-061 (claude-a): I changed tiers.py." in fb and "git rebase origin" not in fb
    assert strip_legacy_rebase(REVIEW) == REVIEW
    assert reconcile_sync_feedback(REVIEW, synced=True, conflicts=[], markers=[]) == REVIEW


def test_runner_marker_note_survives_only_while_markers_remain():
    note = "Conflict markers are still in: src/a.py. Edit each file ..."
    assert reconcile_sync_feedback(note, synced=True, conflicts=[], markers=["src/a.py"]) == note
    assert "Conflict markers are still in" not in reconcile_sync_feedback(note, synced=True, conflicts=[], markers=[])


def test_placeholders_are_found_in_added_doc_lines_only():
    added = {"docs/BENCHMARKS.md": [(10, "| natural | TBD ms |"), (11, "Use `{{feed}}` in the URL"),
                                    (12, "{{p50}}"), (13, "ok line")],
             "README.md": [(3, "Lorem ipsum dolor")],
             "hearing/x.py": [(5, "# TODO: speed up"), (6, "x = 'TBD'")],
             "notes.txt": [(1, "see <fill in later>")]}
    fenced = {"docs/BENCHMARKS.md": {12}}
    hits = placeholder_hits(added, patterns=DEFAULT_PLACEHOLDER_PATTERNS, files=DEFAULT_PLACEHOLDER_FILES,
                            fenced=fenced)
    assert [(f, n) for f, n, _ in hits] == [("README.md", 3), ("docs/BENCHMARKS.md", 10), ("notes.txt", 1)]
    # a project may opt code files in
    code = placeholder_hits(added, patterns=[r"\bTBD\b"], files=["**/*.py"])
    assert [(f, n) for f, n, _ in code] == [("hearing/x.py", 6)]
    assert placeholder_hits(added, patterns=[], files=DEFAULT_PLACEHOLDER_FILES) == []
    fb = placeholder_feedback(hits)
    assert "- docs/BENCHMARKS.md:10: | natural | TBD ms |" in fb


def test_fenced_lines():
    text = "a\n```bash\n{{x}}\n```\nb"
    assert fenced_lines(text) == {2, 3, 4}


def test_verify_feedback_says_when_no_step_printed_a_failure():
    """Q-172/Q-173 (T-080): the feedback was a green pytest tail with no reason for the failure."""
    from swarm.feedback import verify_feedback
    out = "verify_fast: python\n" + "." * 60 + " [100%]\n855 passed, 34 deselected, 1 warning in 54.93s\n"
    fb = verify_feedback(out, script="scripts/verify_fast.sh", code=1)
    assert "(exit 1)" in fb and "no other step printed a failure" in fb and "855 passed" in fb


def test_verify_feedback_names_a_timeout():
    from swarm.feedback import verify_feedback
    out = "." * 40 + " [ 50%]\n\n[timeout after 900s]"
    fb = verify_feedback(out, script="scripts/verify_fast.sh", code=-1)
    assert "stopped after 900 s" in fb and "no other step printed" not in fb


def test_notes_files_status_lists_results_a_cut_off_attempt_left(tmp_path):
    """Q-193 (T-085): attempt 1's evals finished after it was cut off; attempt 2 was not told the JSONs existed."""
    import json
    import os
    import time
    from swarm.feedback import note_paths, notes_files_status
    results = tmp_path / "shared" / "results"
    results.mkdir(parents=True)
    wt = tmp_path / "wt"
    (wt / "eval").mkdir(parents=True)
    ended = time.time() - 600
    (results / "assoc_pair1.json").write_text(json.dumps({"ok": 1}))
    (results / "assoc_pair2.json").write_text('{"partial": ')
    (wt / "eval" / "runs.jsonl").write_text('{"a":1}\n{"a":2}\n')
    os.utime(wt / "eval" / "runs.jsonl", (ended - 60, ended - 60))
    notes = ("- started `python -m eval.assoc --pair 1 > $HEARING_RESULTS_DIR/assoc_pair1.json` (PID 4242)\n"
             "- pair 2 → ${HEARING_RESULTS_DIR}/assoc_pair2.json, pair 3 → $HEARING_RESULTS_DIR/assoc_pair3.json\n"
             "- runs in eval/runs.jsonl; table goes to docs/EVAL.md")
    assert "$HEARING_RESULTS_DIR/assoc_pair1.json" in note_paths(notes)
    out = notes_files_status(notes, [wt], {"HEARING_RESULTS_DIR": str(results)}, ended_at=ended)
    lines = out.splitlines()
    assert len(lines) == 3                               # pair 3 and docs/EVAL.md do not exist: not listed
    assert "assoc_pair1.json" in lines[0] and "valid JSON" in lines[0] and "after that attempt ended" in lines[0]
    assert "NOT valid JSON" in lines[1]
    assert "2 lines" in lines[2] and "after that attempt ended" not in lines[2]
    assert notes_files_status("nothing here", [wt], {}) == ""


def test_a_project_step_that_failed_after_green_pytest_is_named(cfg=None):
    """Q-244 (T-099): verify_full ended with "demo replay regressed or failed" after two green pytest runs; the
    note said "no failure recognised" and its context was cut before that line."""
    from swarm.feedback import failing_step_line, verify_feedback
    out = ("verify_fast: /x/.venv/bin/python\n" + "\n".join(f"0.{i}s call tests/t.py::t{i}" for i in range(40))
           + "\n938 passed, 44 deselected, 1 warning in 42.22s\nverify_fast: PASS\n"
           + "..........                                       [100%]\n28 passed, 952 deselected in 455.65s\n"
           + "| stages | n | latency_ms |\n" * 30 + "focus_hq SI-SDRi 2.1 dB (median 3.4)\ndemo replay regressed or failed\n")
    assert failing_step_line(out) == "demo replay regressed or failed"
    fb = verify_feedback(out, script="scripts/verify_full.sh", code=1)
    assert "the failing step is in this output" in fb and "demo replay regressed or failed" in fb
    assert failing_step_line("verify_fast: PASS\n3 passed in 1.0s\n") == ""


def test_lorem_is_flagged_only_as_lorem_ipsum():
    # 'lorem-like copy' is a real phrase in a design doc; only the filler text itself is a placeholder (Oct 10)
    added = {"docs/DESIGN.md": [(1, "Avoid lorem-like copy in empty states."), (2, "Lorem Ipsum dolor sit amet"),
                                (3, "lorem  ipsum"), (4, "the word lorem alone")]}
    hits = placeholder_hits(added, patterns=DEFAULT_PLACEHOLDER_PATTERNS, files=DEFAULT_PLACEHOLDER_FILES)
    assert [n for _, n, _ in hits] == [2, 3]
