from types import SimpleNamespace

from swarm.config import load_config
from swarm.lightverify import choose_verify, glob_match

LIGHT = ["docs/**", "eval/**", "tests/**", "experiments/**", "*.md"]


def vc(light_paths=LIGHT, protected=None, light_command="scripts/verify_fast.sh", fast="scripts/verify_fast.sh"):
    return SimpleNamespace(light_paths=light_paths, protected_paths=protected, light_command=light_command, fast=fast)


def test_all_docs_and_eval_diff_is_light():
    c = choose_verify(vc(), ["docs/research/x.md", "eval/prep_a.py", "tests/test_prep_a.py",
                             "eval/manifests/a/b.json", "README.md"], "high", "scripts/verify_full.sh")
    assert c.light and c.command == "scripts/verify_fast.sh" and c.reason == "5 files, docs/eval only"


def test_one_file_under_hearing_is_full():
    c = choose_verify(vc(), ["docs/a.md", "hearing/engine.py"], "normal", "scripts/verify_full.sh")
    assert not c.light and c.command == "scripts/verify_full.sh" and c.reason.startswith("hearing/engine.py")


def test_critical_is_full():
    c = choose_verify(vc(), ["docs/a.md"], "critical", "scripts/verify_full.sh")
    assert not c.light and c.reason == "critical task"


def test_protected_path_wins_over_a_light_glob():
    # scripts/ is protected even when a project lists it (or a catch-all) as light
    c = choose_verify(vc(light_paths=LIGHT + ["scripts/**", "**/*.md"]), ["eval/a.py", "scripts/x"], "normal", "F")
    assert not c.light and c.reason.startswith("scripts/x (protected")
    for p in ("scripts/notes.md", "web/judge/README.md", ".swarm/config.yaml", ".github/workflows/ci.yml",
              "pyproject.toml", "requirements-dev.txt", "eval/requirements.txt", "config/demo.md"):
        assert not choose_verify(vc(light_paths=["**"]), [p], "low", "F").light, p


def test_protected_paths_config_extends_the_list():
    assert choose_verify(vc(), ["eval/prep.py"], "normal", "F").light
    c = choose_verify(vc(protected=["eval/live_*.py"]), ["eval/prep.py", "eval/live_demo.py"], "normal", "F")
    assert not c.light and "eval/live_demo.py (protected: eval/live_*.py)" == c.reason


def test_empty_or_absent_light_paths_is_always_full():
    for lp in (None, []):
        c = choose_verify(vc(light_paths=lp), ["docs/a.md"], "low", "scripts/verify_full.sh")
        assert not c.light and c.command == "scripts/verify_full.sh"


def test_empty_diff_is_full_and_light_command_defaults_to_fast():
    assert not choose_verify(vc(), [], "low", "F").light
    c = choose_verify(vc(light_command=None, fast="scripts/verify_fast.sh"), ["docs/a.md"], "low", "F")
    assert c.light and c.command == "scripts/verify_fast.sh"


def test_glob_semantics():
    assert glob_match("docs/research/deep/x.md", "docs/**")
    assert glob_match("README.md", "*.md") and glob_match("eval/notes.md", "*.md")
    assert not glob_match("hearing/x.py", "eval/**") and not glob_match("evaluation/x.py", "eval/**")
    assert glob_match("eval/prep_a.py", "eval/prep_*.py") and not glob_match("eval/sub/prep_a.py", "eval/prep_*.py")
    assert glob_match("a/b/c.py", "**/c.py") and glob_match("c.py", "**/c.py")


def test_config_reads_the_light_keys(project_dir, sample_config_dict):
    import yaml
    d = dict(sample_config_dict)
    d["verify"] = {**d["verify"], "light_paths": LIGHT, "light_command": "scripts/verify_fast.sh",
                   "protected_paths": ["eval/live_*.py"]}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.verify.light_paths == LIGHT and cfg.verify.light_command == "scripts/verify_fast.sh"
    assert cfg.verify.protected_paths == ["eval/live_*.py"]
    d["verify"] = {k: v for k, v in d["verify"].items() if not k.startswith(("light", "protected"))}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.verify.light_paths is None and cfg.verify.light_command is None


def test_quick_slot_wait_for_web_and_docs_only():
    from swarm.lightverify import quick_slot_wait
    cfg = SimpleNamespace(quick_paths=None, quick_wait_seconds=60.0)
    assert quick_slot_wait(cfg, ["web/app/judge.js", "docs/x.md", "README.md"]) == 60.0
    assert quick_slot_wait(cfg, ["web/app/judge.js", "hearing/engine.py"]) is None
    assert quick_slot_wait(cfg, ["eval/measure.py"]) is None
    assert quick_slot_wait(cfg, []) is None
    custom = SimpleNamespace(quick_paths=["web/**", "scripts/demo.sh"], quick_wait_seconds=30)
    assert quick_slot_wait(custom, ["scripts/demo.sh", "web/a.css"]) == 30.0
    assert quick_slot_wait(custom, ["scripts/verify_fast.sh"]) is None
    assert quick_slot_wait(SimpleNamespace(quick_paths=[], quick_wait_seconds=60), ["web/a.js"]) is None
    assert quick_slot_wait(SimpleNamespace(), ["docs/a.md"]) == 60.0     # older config objects: defaults


WEB = ["web/**", "tests/test_app_*", "web/app/tests/shots/**", "docs/**", "*.md"]


def wvc(web_paths=WEB, web_command="scripts/verify_web.sh", **kw):
    v = vc(**kw)
    v.web_paths, v.web_command = web_paths, web_command
    return v


def test_web_only_diff_gets_the_web_command_without_a_slot_even_when_critical():
    from swarm.lightverify import web_choice
    files = ["web/app/app.js", "web/app/tests/shots/T-366/a.jpg", "tests/test_app_static.py", "docs/a.md", "README.md"]
    for imp in ("low", "critical"):
        c = choose_verify(wvc(), files, imp, "scripts/verify_full.sh")
        assert c.web and c.light and not c.slot and c.command == "scripts/verify_web.sh"
        assert c.reason == "5 files, web only"
        assert c.commit_line() == "Verify: web, scripts/verify_web.sh (5 files, web only)"
        assert "web verify" in c.log_line("T-1") and "no verify slot" in c.log_line("T-1")
    assert web_choice(wvc(), files).web


def test_one_non_web_file_falls_back_to_light_or_full():
    c = choose_verify(wvc(), ["web/app/app.js", "hearing/engine.py"], "normal", "F")
    assert not c.web and not c.light and c.slot and c.command == "F"
    c = choose_verify(wvc(), ["docs/a.md", "eval/x.py"], "normal", "F")      # docs + eval: the light rules
    assert not c.web and c.light and c.command == "scripts/verify_fast.sh"
    c = choose_verify(wvc(), ["tests/test_server.py"], "normal", "F")      # tests/ outside test_app_*: light
    assert not c.web and c.light


def test_web_verify_off_without_paths_command_or_files():
    from swarm.lightverify import web_choice
    assert web_choice(wvc(web_paths=None), ["web/a.js"]) is None
    assert web_choice(wvc(web_paths=[]), ["web/a.js"]) is None
    assert web_choice(wvc(web_command=None), ["web/a.js"]) is None
    assert web_choice(wvc(), []) is None
    assert web_choice(vc(), ["web/a.js"]) is None                          # older config objects: off
    assert not choose_verify(vc(), ["web/a.js"], "low", "F").web


def test_web_paths_and_command_load_from_config(project_dir, sample_config_dict):
    import yaml
    from swarm.lightverify import logs_choice
    d = dict(sample_config_dict)
    d["verify"] = {**d["verify"], "web_paths": WEB, "web_command": "scripts/verify_web.sh"}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.verify.web_paths == WEB and cfg.verify.web_command == "scripts/verify_web.sh"
    assert logs_choice(cfg.verify)
    d["verify"] = {k: v for k, v in d["verify"].items() if not k.startswith(("web", "light"))}
    (project_dir / ".swarm" / "config.yaml").write_text(yaml.safe_dump(d))
    cfg = load_config(project_dir / ".swarm" / "config.yaml")
    assert cfg.verify.web_paths is None and cfg.verify.web_command is None and not logs_choice(cfg.verify)
