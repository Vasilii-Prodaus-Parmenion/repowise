"""VbSidecarClient registry: the run-scoped per-repo singleton (D6).

The subprocess handshake itself is exercised end-to-end against a real
built sidecar in tests/integration (skipped without the SDK) — these tests
only cover the pure Python bookkeeping: singleton identity and a shutdown
that never started a process is a no-op, not an error.
"""

from __future__ import annotations

from pathlib import Path

from repowise.core.ingestion.vb.sidecar import (
    VbSyncBridge,
    get_sidecar,
    parse_batch,
    shutdown_sidecar,
)


def test_get_sidecar_returns_singleton_per_repo(tmp_path: Path) -> None:
    a = get_sidecar(tmp_path)
    b = get_sidecar(tmp_path)

    assert a is b


def test_get_sidecar_distinguishes_repos(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    repo_a.mkdir()
    repo_b.mkdir()

    assert get_sidecar(repo_a) is not get_sidecar(repo_b)


def test_get_sidecar_resolves_equivalent_paths(tmp_path: Path) -> None:
    """``./repo`` and ``repo/.`` must land on the same client."""
    repo = tmp_path / "repo"
    repo.mkdir()

    assert get_sidecar(repo / ".") is get_sidecar(repo)


async def test_shutdown_sidecar_with_no_process_is_a_noop(tmp_path: Path) -> None:
    client = get_sidecar(tmp_path)
    assert client._proc is None

    await shutdown_sidecar(tmp_path)  # must not raise


async def test_shutdown_sidecar_unknown_repo_is_a_noop(tmp_path: Path) -> None:
    await shutdown_sidecar(tmp_path / "never-requested")  # must not raise


class _FakeFileInfo:
    def __init__(self, path: str, abs_path: str) -> None:
        self.path = path
        self.abs_path = abs_path


class _FakeClient:
    """Captures the payload parse_batch would have sent, without a subprocess."""

    def __init__(self) -> None:
        self.payload: dict | None = None

    async def request(self, payload: dict, timeout: float) -> dict:
        self.payload = payload
        return {"ok": True, "results": []}


async def test_parse_batch_sends_per_file_root_namespaces() -> None:
    """A solution with several VB projects has one RootNamespace per file
    (vb-support.md §4.2) — the batch request must not flatten that to one
    value for the whole chunk."""
    client = _FakeClient()
    files = [
        _FakeFileInfo("A/Foo.vb", "/repo/A/Foo.vb"),
        _FakeFileInfo("B/Bar.vb", "/repo/B/Bar.vb"),
    ]

    await parse_batch(client, files, root_namespaces={"A/Foo.vb": "Acme.A", "B/Bar.vb": "Acme.B"})

    assert client.payload is not None
    by_path = {f["path"]: f["rootNamespace"] for f in client.payload["files"]}
    assert by_path == {"A/Foo.vb": "Acme.A", "B/Bar.vb": "Acme.B"}


async def test_parse_batch_falls_back_to_single_root_namespace() -> None:
    """VbSyncBridge parses one file at a time and already knows its project,
    so it passes the singular kwarg rather than building a one-entry dict."""
    client = _FakeClient()
    files = [_FakeFileInfo("Foo.vb", "/repo/Foo.vb")]

    await parse_batch(client, files, root_namespace="Acme.Legacy")

    assert client.payload["files"][0]["rootNamespace"] == "Acme.Legacy"


async def test_parse_batch_root_namespaces_takes_precedence_for_known_paths() -> None:
    client = _FakeClient()
    files = [_FakeFileInfo("Foo.vb", "/repo/Foo.vb")]

    await parse_batch(
        client, files, root_namespace="Fallback", root_namespaces={"Foo.vb": "Acme.Real"}
    )

    assert client.payload["files"][0]["rootNamespace"] == "Acme.Real"


def test_vb_sync_bridge_root_namespace_lookup_is_cached(tmp_path: Path) -> None:
    (tmp_path / "Legacy").mkdir()
    (tmp_path / "Legacy" / "Legacy.vbproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<RootNamespace>Acme.Legacy</RootNamespace></PropertyGroup></Project>"
    )
    bridge = VbSyncBridge(tmp_path)
    try:
        file_info = _FakeFileInfo("Legacy/Foo.vb", str(tmp_path / "Legacy" / "Foo.vb"))
        assert bridge._root_namespace_for(file_info) == "Acme.Legacy"
        assert bridge._vb_project_namespaces is not None
        # Second call must not re-scan the .vbproj tree.
        (tmp_path / "Legacy" / "Legacy.vbproj").unlink()
        assert bridge._root_namespace_for(file_info) == "Acme.Legacy"
    finally:
        bridge.close()
