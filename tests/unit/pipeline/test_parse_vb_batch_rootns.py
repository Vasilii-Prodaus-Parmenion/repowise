"""RootNamespace threading into VB sidecar parse requests (vb-support.md
Phase 3, §4.2/§5.3): a solution with several VB projects must send each
file its own project's RootNamespace, not one value for the whole batch.

The sidecar itself is faked out (monkeypatched) — these tests only cover
the Python-side per-file lookup and payload assembly, not the Roslyn
process.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from repowise.core.ingestion.models import FileInfo
from repowise.core.pipeline.phases.ingestion import _parse_vb_batch


def _vb_fi(rel: str, abs_: Path) -> FileInfo:
    return FileInfo(
        path=rel,
        abs_path=str(abs_),
        language="vbnet",  # type: ignore[arg-type]
        size_bytes=0,
        git_hash="",
        last_modified=datetime.now(),
        is_test=False,
        is_config=False,
        is_api_contract=False,
        is_entry_point=False,
    )


class _FakeClient:
    pass


async def test_parse_vb_batch_threads_per_project_root_namespace(tmp_path, monkeypatch) -> None:
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "A.vbproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<RootNamespace>Acme.A</RootNamespace></PropertyGroup></Project>"
    )
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "B.vbproj").write_text(
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup>'
        "<RootNamespace>Acme.B</RootNamespace></PropertyGroup></Project>"
    )

    fi_a = _vb_fi("A/Foo.vb", tmp_path / "A" / "Foo.vb")
    fi_b = _vb_fi("B/Bar.vb", tmp_path / "B" / "Bar.vb")

    captured: dict = {}

    async def _fake_parse_batch(client, files, *, root_namespace="", root_namespaces=None):
        captured["root_namespaces"] = dict(root_namespaces or {})
        return {}

    monkeypatch.setattr(
        "repowise.core.ingestion.vb.sidecar.get_sidecar", lambda repo_path: _FakeClient()
    )
    monkeypatch.setattr("repowise.core.ingestion.vb.sidecar.parse_batch", _fake_parse_batch)

    vb_misses = [
        (0, (fi_a, b"Public Class Foo\nEnd Class\n"), "hashA"),
        (1, (fi_b, b"Public Class Bar\nEnd Class\n"), "hashB"),
    ]
    results = await _parse_vb_batch(tmp_path, vb_misses, progress=None)

    assert captured["root_namespaces"] == {"A/Foo.vb": "Acme.A", "B/Bar.vb": "Acme.B"}
    # A sidecar miss (empty dict returned) degrades to an empty ParsedFile
    # per file, not a crash (D3 — the batch call itself succeeded).
    assert set(results.keys()) == {0, 1}


async def test_parse_vb_batch_empty_input_short_circuits(tmp_path) -> None:
    assert await _parse_vb_batch(tmp_path, [], progress=None) == {}
