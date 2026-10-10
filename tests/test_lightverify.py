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
