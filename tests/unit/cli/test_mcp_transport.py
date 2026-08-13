"""MCP transport CLI and server dispatch tests."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from repowise.cli.main import cli
from repowise.core.workspace.config import RepoEntry, WorkspaceConfig


def test_mcp_help_lists_streamable_http_transport() -> None:
    result = CliRunner().invoke(cli, ["mcp", "--help"])

    assert result.exit_code == 0
    assert "streamable-http" in result.output
    assert "HTTP/SSE" in result.output


def test_mcp_cli_passes_tools_override(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".repowise").mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "repowise.server.mcp_server.run_mcp", lambda **kw: captured.update(kw)
    )

    result = CliRunner().invoke(
        cli, ["mcp", str(tmp_path), "--tools", "+get_execution_flows,-get_dead_code"]
    )

    assert result.exit_code == 0
    assert captured["tools"] == "+get_execution_flows,-get_dead_code"
    assert captured["host"] == "127.0.0.1"


def test_mcp_cli_all_flag_overrides_tools(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / ".repowise").mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "repowise.server.mcp_server.run_mcp", lambda **kw: captured.update(kw)
    )

    result = CliRunner().invoke(cli, ["mcp", str(tmp_path), "--all", "--tools", "get_answer"])

    assert result.exit_code == 0
    assert captured["tools"] == "all"
    assert captured["host"] == "127.0.0.1"


def test_mcp_cli_accepts_streamable_http_transport(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".repowise").mkdir()
    captured: dict[str, object] = {}

    def fake_run_mcp(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", fake_run_mcp)

    result = CliRunner().invoke(
        cli,
        [
            "mcp",
            str(tmp_path),
            "--transport",
            "streamable-http",
            "--port",
            "7339",
        ],
    )

    assert result.exit_code == 0
    assert "streamable HTTP" in result.output
    assert captured == {
        "transport": "streamable-http",
        "repo_path": str(tmp_path.resolve()),
        "host": "127.0.0.1",
        "port": 7339,
        "tools": None,
    }


def test_mcp_cli_streamable_http_prints_workspace_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    api = workspace / "services" / "api"
    web = workspace / "apps" / "web"
    api.mkdir(parents=True)
    web.mkdir(parents=True)
    WorkspaceConfig(
        repos=[
            RepoEntry(path="services/api", alias="api", is_primary=True),
            RepoEntry(path="apps/web", alias="web"),
        ],
        default_repo="api",
    ).save(workspace)

    captured: dict[str, object] = {}

    def fake_run_mcp(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", fake_run_mcp)

    result = CliRunner().invoke(
        cli,
        [
            "mcp",
            str(workspace),
            "--transport",
            "streamable-http",
            "--port",
            "7341",
        ],
    )

    assert result.exit_code == 0
    output = result.output.replace("\n", "")
    assert "URL: http://127.0.0.1:7341/mcp" in result.output
    assert f"Workspace: {workspace.resolve()}" in output
    assert "Default repo: api" in result.output
    assert "Repos: api, web" in result.output
    assert "Warning: No .repowise directory" not in result.output
    assert captured == {
        "transport": "streamable-http",
        "repo_path": str(workspace.resolve()),
        "host": "127.0.0.1",
        "port": 7341,
        "tools": None,
    }


def test_run_mcp_dispatches_streamable_http(monkeypatch) -> None:
    from repowise.server.mcp_server import _server

    calls: list[dict[str, str]] = []
    watchdog_started = False

    def fake_run(**kwargs):
        calls.append(kwargs)

    def fake_watchdog():
        nonlocal watchdog_started
        watchdog_started = True

    monkeypatch.setattr(_server.mcp, "run", fake_run)
    monkeypatch.setattr(
        "repowise.server.mcp_server._watchdog.start_parent_watchdog",
        fake_watchdog,
    )

    _server.run_mcp(transport="streamable-http", repo_path="/tmp/repo", port=7340)

    assert _server.mcp.settings.host == "127.0.0.1"
    assert _server.mcp.settings.port == 7340
    assert calls == [{"transport": "streamable-http"}]
    assert watchdog_started is False


def test_run_mcp_keeps_existing_stdio_and_sse_dispatch(monkeypatch) -> None:
    from repowise.server.mcp_server import _server

    calls: list[dict[str, str]] = []
    watchdog_calls = 0

    def fake_run(**kwargs):
        calls.append(kwargs)

    def fake_watchdog():
        nonlocal watchdog_calls
        watchdog_calls += 1

    monkeypatch.setattr(_server.mcp, "run", fake_run)
    monkeypatch.setattr(
        "repowise.server.mcp_server._watchdog.start_parent_watchdog",
        fake_watchdog,
    )

    _server.run_mcp(transport="sse", port=7338)
    _server.run_mcp(transport="stdio", port=9999)

    assert calls == [{"transport": "sse"}, {"transport": "stdio"}]
    assert watchdog_calls == 1


# ---------------------------------------------------------------------------
# New tests: --host flag, REPOWISE_HOST env, security warnings
# ---------------------------------------------------------------------------


def test_mcp_cli_passes_host_to_run_mcp(monkeypatch, tmp_path: Path) -> None:
    """--host 0.0.0.0 is forwarded to run_mcp."""
    (tmp_path / ".repowise").mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", lambda **kw: captured.update(kw))
    monkeypatch.delenv("REPOWISE_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        ["mcp", str(tmp_path), "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "7342"],
    )

    assert result.exit_code == 0
    assert captured["host"] == "0.0.0.0"


def test_mcp_cli_defaults_host_to_loopback(monkeypatch, tmp_path: Path) -> None:
    """No --host flag → run_mcp receives host='127.0.0.1'."""
    (tmp_path / ".repowise").mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", lambda **kw: captured.update(kw))
    monkeypatch.delenv("REPOWISE_HOST", raising=False)

    result = CliRunner().invoke(
        cli,
        ["mcp", str(tmp_path), "--transport", "streamable-http", "--port", "7342"],
    )

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"


def test_mcp_cli_inherits_repowise_host_env(monkeypatch, tmp_path: Path) -> None:
    """REPOWISE_HOST=0.0.0.0 (no --host flag) → run_mcp receives host='0.0.0.0'."""
    (tmp_path / ".repowise").mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", lambda **kw: captured.update(kw))
    monkeypatch.setenv("REPOWISE_HOST", "0.0.0.0")
    monkeypatch.delenv("REPOWISE_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        ["mcp", str(tmp_path), "--transport", "streamable-http", "--port", "7342"],
    )

    assert result.exit_code == 0
    assert captured["host"] == "0.0.0.0"


def test_mcp_cli_prints_security_warning_on_wide_bind(monkeypatch, tmp_path: Path) -> None:
    """--host 0.0.0.0 without REPOWISE_API_KEY → security warning in output."""
    (tmp_path / ".repowise").mkdir()
    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", lambda **kw: None)
    monkeypatch.delenv("REPOWISE_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        ["mcp", str(tmp_path), "--transport", "streamable-http", "--host", "0.0.0.0"],
    )

    assert result.exit_code == 0
    assert "SECURITY WARNING" in result.output
    assert "0.0.0.0" in result.output
    assert "REPOWISE_API_KEY" in result.output


def test_mcp_cli_no_warning_with_api_key(monkeypatch, tmp_path: Path) -> None:
    """--host 0.0.0.0 WITH REPOWISE_API_KEY → no security warning."""
    (tmp_path / ".repowise").mkdir()
    monkeypatch.setattr("repowise.server.mcp_server.run_mcp", lambda **kw: None)
    monkeypatch.setenv("REPOWISE_API_KEY", "some-key")

    result = CliRunner().invoke(
        cli,
        ["mcp", str(tmp_path), "--transport", "streamable-http", "--host", "0.0.0.0"],
    )

    assert result.exit_code == 0
    assert "SECURITY WARNING" not in result.output


def test_run_mcp_sets_host_on_settings(monkeypatch) -> None:
    """run_mcp sets mcp.settings.host (parallel to existing port test)."""
    from repowise.server.mcp_server import _server

    calls: list[dict] = []
    monkeypatch.setattr(_server.mcp, "run", lambda **kw: calls.append(kw))
    monkeypatch.delenv("REPOWISE_API_KEY", raising=False)

    _server.run_mcp(transport="streamable-http", host="0.0.0.0", port=7343)

    assert _server.mcp.settings.host == "0.0.0.0"
    assert _server.mcp.settings.port == 7343
    assert calls == [{"transport": "streamable-http"}]
