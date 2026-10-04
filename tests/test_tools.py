import json

from swarm.tools import PluginInfo, ensure_plugins, installed_plugins, plugin_mcp_servers
from swarm.workspace import CmdResult

LISTING = [{"id": "context7@claude-plugins-official", "version": "1", "scope": "user", "enabled": True,
            "installPath": "/cache/context7/1"},
           {"id": "playwright@claude-plugins-official", "version": "1", "scope": "user", "enabled": False,
            "installPath": "/cache/playwright/1"}]


def test_installed_plugins_parses_json_listing():
    calls = []

    def run(args, cwd=None, timeout=60):
        calls.append(args)
        return CmdResult(0, json.dumps(LISTING), "")

    plugins = installed_plugins(run=run)
    assert calls == [["claude", "plugin", "list", "--json"]]
    assert plugins["context7"] == PluginInfo(name="context7", id="context7@claude-plugins-official", enabled=True,
                                             install_path="/cache/context7/1")
    assert plugins["playwright"].enabled is False


def test_installed_plugins_survives_a_broken_cli():
    assert installed_plugins(run=lambda a, cwd=None, timeout=60: CmdResult(1, "", "no such command")) == {}
    assert installed_plugins(run=lambda a, cwd=None, timeout=60: CmdResult(0, "not json", "")) == {}


def test_plugin_mcp_servers_reads_manifests_and_expands_env(tmp_path, monkeypatch):
    c7 = tmp_path / "context7"
    c7.mkdir()
    (c7 / ".mcp.json").write_text(json.dumps({"mcpServers": {"context7": {
        "type": "http", "url": "https://mcp.context7.com/mcp", "headers": {"Authorization": "${C7_KEY:-}"}}}}))
    pw = tmp_path / "playwright"
    (pw / ".claude-plugin").mkdir(parents=True)
    (pw / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "playwright", "mcpServers": {
        "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"], "env": {"HOME": "${HOME}"}}}}))
    monkeypatch.setenv("C7_KEY", "secret")
    monkeypatch.setenv("HOME", "/home/x")
    plugins = {"context7": PluginInfo("context7", "context7@m", True, str(c7)),
               "playwright": PluginInfo("playwright", "playwright@m", True, str(pw)),
               "gone": PluginInfo("gone", "gone@m", True, str(tmp_path / "missing"))}
    servers = plugin_mcp_servers(plugins)
    assert servers["context7"]["headers"]["Authorization"] == "secret"
    assert servers["playwright"]["env"]["HOME"] == "/home/x"
    assert set(servers) == {"context7", "playwright"}  # missing install dirs are skipped
    monkeypatch.delenv("C7_KEY")
    assert plugin_mcp_servers(plugins)["context7"]["headers"]["Authorization"] == ""


def test_ensure_plugins_installs_missing_and_enables_disabled():
    calls = []
    state = {"listing": list(LISTING)}

    def run(args, cwd=None, timeout=60):
        calls.append(args)
        if args[:3] == ["claude", "plugin", "list"]:
            return CmdResult(0, json.dumps(state["listing"]), "")
        if args[:3] == ["claude", "plugin", "install"]:
            name = args[3].split("@")[0]
            if name == "broken":
                return CmdResult(1, "", "not found in any marketplace")
            state["listing"].append({"id": args[3] if "@" in args[3] else f"{name}@claude-plugins-official",
                                     "enabled": True, "installPath": f"/cache/{name}"})
            return CmdResult(0, "installed", "")
        if args[:3] == ["claude", "plugin", "enable"]:
            for p in state["listing"]:
                if p["id"].split("@")[0] == args[3]:
                    p["enabled"] = True
            return CmdResult(0, "", "")
        raise AssertionError(args)

    logs = []
    missing = ensure_plugins(["context7", "playwright", "frontend-design@claude-plugins-official", "broken"],
                             run=run, log=logs.append, which=lambda name: "/fake/claude")
    assert missing == ["broken"]
    assert ["claude", "plugin", "enable", "playwright"] in calls
    assert ["claude", "plugin", "install", "frontend-design@claude-plugins-official"] in calls
    assert ["claude", "plugin", "install", "broken@claude-plugins-official"] in calls
    assert not any(a[:3] == ["claude", "plugin", "install"] and a[3].startswith("context7") for a in calls)
    assert any("broken" in line for line in logs)
    assert ensure_plugins([], run=run) == []


def test_ensure_plugins_reports_all_missing_when_cli_absent():
    assert ensure_plugins(["a"], run=lambda a, cwd=None, timeout=60: CmdResult(127, "", "not found"),
                          which=lambda n: None) == ["a"]


def test_ensure_plugins_can_install_without_enabling():
    calls = []

    def run(args, cwd=None, timeout=60):
        calls.append(args)
        if args[:3] == ["claude", "plugin", "list"]:
            return CmdResult(0, json.dumps(LISTING), "")
        return CmdResult(0, "", "")

    assert ensure_plugins(["playwright", "hf"], run=run, enable=False, which=lambda name: "/fake/claude") == []
    assert ["claude", "plugin", "install", "hf@claude-plugins-official"] in calls
    assert not any(a[:3] == ["claude", "plugin", "enable"] for a in calls)


def test_plugin_dirs_for_disabled_plugins_only():
    from swarm.tools import plugin_dirs
    have = {"context7": PluginInfo("context7", "context7@m", True, "/c/context7"),
            "playwright": PluginInfo("playwright", "playwright@m", False, "/c/playwright")}
    assert plugin_dirs(["context7", "playwright", "absent"], have) == ["/c/playwright"]


def test_plugin_settings_switch_off_everything_not_allowed():
    from swarm.tools import plugin_settings
    have = {"superpowers": PluginInfo("superpowers", "superpowers@claude-plugins-official", True, "/a"),
            "frontend-design": PluginInfo("frontend-design", "frontend-design@claude-plugins-official", True, "/b"),
            "hf": PluginInfo("hf", "hf@m", False, "/c")}
    assert plugin_settings(have, ["frontend-design", "hf@m"]) == {
        "enabledPlugins": {"superpowers@claude-plugins-official": False}}
    assert plugin_settings(have, ["superpowers", "frontend-design"]) is None
    assert plugin_settings({}, []) is None


def test_ensure_playwright_browser_runs_installer_once():
    from swarm.tools import ensure_playwright_browser
    calls = []
    run = lambda args, cwd=None, timeout=60: calls.append(args) or CmdResult(0, "", "")  # noqa: E731
    assert ensure_playwright_browser(run=run) is True
    assert calls == [["npx", "--yes", "playwright", "install", "chromium"]]


def test_plugin_mcp_servers_accepts_flat_manifest(tmp_path):
    pw = tmp_path / "playwright"
    pw.mkdir()
    (pw / ".mcp.json").write_text(json.dumps({"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}))
    servers = plugin_mcp_servers({"playwright": PluginInfo("playwright", "playwright@m", True, str(pw))})
    assert servers["playwright"]["command"] == "npx"
