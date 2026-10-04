import json

import pytest
from typer.testing import CliRunner

from swarm.cli import app
from swarm.doctor import Check, smoke_agent
from swarm.models import RunResult
from swarm.preflight import check_headless
from swarm.workspace import CmdResult


@pytest.mark.parametrize('provider,binary', [('claude', 'claude'), ('codex', 'codex'), ('gemini', 'gemini'),
                                            ('antigravity', 'agy'), ('grok', 'grok')])
def test_preflight_rejects_incompatible_cli_flags(cfg, provider, binary):
    cfg.agents['codex-a'].provider = provider
    calls = []
    def help_only(argv, **kw):
        calls.append(argv)
        return CmdResult(0, 'Usage: --help --version', '')
    check = check_headless(cfg, 'codex-a', run=help_only, which=lambda name: '/bin/' + name)
    assert not check.ok and 'missing adapter flags' in check.detail
    assert calls == ([[binary, 'exec', '--help']] if provider == 'codex' else [[binary, '--help']])


def test_preflight_accepts_compatible_codex_without_model_call(cfg):
    flags = '--json --sandbox --output-schema -C -m -c'
    check = check_headless(cfg, 'codex-a', run=lambda *a, **kw: CmdResult(0, flags, ''), which=lambda n: '/bin/' + n)
    assert check.ok
    # --json-schema must not be mistaken for --json.
    bad = check_headless(cfg, 'codex-a', run=lambda *a, **kw: CmdResult(0, flags.replace('--json ', '--json-schema '), ''),
                         which=lambda n: '/bin/' + n)
    assert not bad.ok and '--json' in bad.detail


def test_preflight_handles_unknown_missing_and_broken_help(cfg):
    assert not check_headless(cfg, 'unknown').ok
    assert not check_headless(cfg, 'codex-a', which=lambda n: None).ok
    result = check_headless(cfg, 'codex-a', run=lambda *a, **kw: CmdResult(1, '', 'help failed'), which=lambda n: '/bin/' + n)
    assert not result.ok and 'help failed' in result.detail


def test_generic_preflight_validates_template_and_executable(cfg):
    agent = cfg.agents['fake-b']
    agent.command_template = 'missing-custom-cli {prompt_file}'
    assert not check_headless(cfg, agent.name, which=lambda n: None).ok
    agent.command_template = 'bash {unsupported}'
    assert not check_headless(cfg, agent.name, which=lambda n: '/bin/' + n).ok
    agent.command_template = 'bash {prompt_file}'
    assert check_headless(cfg, agent.name, which=lambda n: '/bin/' + n).ok


@pytest.mark.parametrize('payload', [None, {}, {'status': 'failed', 'summary': 'smoke ok'},
                                    {'status': 'done'}, {'status': 'done', 'summary': 'unrelated'}])
def test_smoke_rejects_invalid_structured_report(cfg, tmp_path, monkeypatch, payload):
    class Agent:
        def run(self, spec):
            return RunResult(ok=True, exit_code=0, stdout='', stderr='', structured_output=payload)
    monkeypatch.setattr('swarm.adapters.get_adapter', lambda a: Agent())
    check = smoke_agent(cfg, 'fake-b', tmp_path)
    assert not check.ok and 'expected report' in check.detail


@pytest.mark.parametrize('content,expected', [('not json', False), ('[]', False),
                                             ('{"status":"blocked","summary":"smoke ok"}', False),
                                             ('{"status":"done","summary":"smoke ok"}', True)])
def test_smoke_validates_fallback_report(cfg, tmp_path, monkeypatch, content, expected):
    class Agent:
        def run(self, spec):
            (spec.cwd / '.swarm-run/report.json').write_text(content)
            return RunResult(ok=True, exit_code=0, stdout='', stderr='')
    monkeypatch.setattr('swarm.adapters.get_adapter', lambda a: Agent())
    assert smoke_agent(cfg, 'fake-b', tmp_path).ok is expected


def test_smoke_rejects_stale_report_and_accepts_valid_current_report(cfg, tmp_path, monkeypatch):
    report = tmp_path / '.swarm-run/report.json'
    report.parent.mkdir()
    report.write_text(json.dumps({'status': 'done', 'summary': 'smoke ok'}))
    class Agent:
        result = None
        def run(self, spec):
            assert not report.exists()
            return RunResult(ok=True, exit_code=0, stdout='', stderr='', structured_output=self.result)
    agent = Agent()
    monkeypatch.setattr('swarm.adapters.get_adapter', lambda a: agent)
    assert not smoke_agent(cfg, 'fake-b', tmp_path).ok
    agent.result = {'status': 'done', 'summary': 'smoke ok'}
    assert smoke_agent(cfg, 'fake-b', tmp_path).ok


@pytest.mark.parametrize('failed,offline', [(True, False), (False, True)])
def test_doctor_does_not_call_model_after_failed_checks_or_in_offline_mode(project_dir, monkeypatch, failed, offline):
    monkeypatch.setattr('swarm.doctor.run_checks', lambda *a, **kw: [Check('setup', not failed, 'fixture')])
    monkeypatch.setattr('swarm.preflight.check_headless', lambda *a, **kw: Check('headless', True, 'fixture'))
    def unexpected(*a, **kw):
        raise AssertionError('no model call expected')
    monkeypatch.setattr('swarm.doctor.smoke_agent', unexpected)
    args = ['--config', str(project_dir / '.swarm/config.yaml'), 'doctor', '--smoke', 'codex-a']
    if offline:
        args.append('--offline')
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1 and 'not run' in result.output


def test_run_rejects_incompatible_cli_before_reading_or_claiming_board(project_dir, monkeypatch):
    monkeypatch.setattr('swarm.preflight.check_headless', lambda *a, **kw: Check('headless', False, 'incompatible CLI'))
    def unexpected(*a, **kw):
        raise AssertionError('no board access or task claims expected')
    monkeypatch.setattr('swarm.cli.make_board', unexpected)
    result = CliRunner().invoke(app, ['--config', str(project_dir / '.swarm/config.yaml'), '--host', 'host-a',
                                     'run', '--agent', 'codex-a', '--once'])
    assert result.exit_code == 1 and 'incompatible CLI' in result.output
