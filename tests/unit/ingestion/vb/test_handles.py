"""``eventWiring`` -> ``DynamicEdge`` mapping (D8, phase 4, vb-support.md §6.1).

No SDK or subprocess needed: feeds recorded-shape sidecar "parse" result
dicts straight into ``VbHandlesDynamicHints.extract`` via a fake client
standing in for the run-scoped singleton (monkeypatched ``get_sidecar``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from repowise.core.ingestion.vb import handles as handles_mod


class _FakeClient:
    def __init__(self, file_results: dict[str, dict[str, Any]]) -> None:
        self.file_results = file_results


def _patch_client(monkeypatch: pytest.MonkeyPatch, file_results: dict[str, dict[str, Any]]) -> None:
    client = _FakeClient(file_results)
    monkeypatch.setattr(handles_mod, "get_sidecar", lambda repo_root: client)


def test_no_file_results_yields_no_edges(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _patch_client(monkeypatch, {})
    edges = handles_mod.VbHandlesDynamicHints().extract(tmp_path)
    assert edges == []


def test_handles_clause_targets_the_handler_symbol_in_the_same_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The handler's own declaration line is the eventWiring line — no
    cross-file resolution needed to find it (§6.1)."""
    abs_path = str(tmp_path / "Form1.vb")
    file_results = {
        abs_path: {
            "symbols": [
                {
                    "name": "Button1_Click",
                    "kind": "method",
                    "startLine": 10,
                    "parentName": "Form1",
                },
            ],
            "eventWiring": [
                {"kind": "handles", "line": 10, "withEventsName": "Button1", "eventName": "Click"},
            ],
        }
    }
    _patch_client(monkeypatch, file_results)

    edges = handles_mod.VbHandlesDynamicHints().extract(tmp_path)

    assert len(edges) == 1
    edge = edges[0]
    assert edge.source == "Form1.vb"
    assert edge.target == "Form1.vb::Form1::Button1_Click"
    assert edge.edge_type == "dynamic"
    assert edge.hint_source == "vb_handles"


def test_add_handler_resolves_addressof_target_cross_file_case_insensitively(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wiring_path = str(tmp_path / "Wiring.vb")
    handler_path = str(tmp_path / "Handlers.vb")
    file_results = {
        wiring_path: {
            "symbols": [],
            "eventWiring": [
                {"kind": "add_handler", "line": 5, "targetName": "onclick"},
            ],
        },
        handler_path: {
            "symbols": [
                {"name": "OnClick", "kind": "method", "startLine": 3, "parentName": "Handlers"},
            ],
            "eventWiring": [],
        },
    }
    _patch_client(monkeypatch, file_results)

    edges = handles_mod.VbHandlesDynamicHints().extract(tmp_path)

    assert len(edges) == 1
    edge = edges[0]
    assert edge.source == "Wiring.vb"
    assert edge.target == "Handlers.vb::Handlers::OnClick"


def test_add_handler_with_unresolvable_target_yields_no_edge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    abs_path = str(tmp_path / "Form1.vb")
    file_results = {
        abs_path: {
            "symbols": [],
            "eventWiring": [{"kind": "add_handler", "line": 5, "targetName": "GhostMethod"}],
        }
    }
    _patch_client(monkeypatch, file_results)

    edges = handles_mod.VbHandlesDynamicHints().extract(tmp_path)

    assert edges == []


def test_ambiguous_addressof_name_fans_out_to_every_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Over-emission beats under-emission for a dead-code rescue signal —
    mirrors dynamic_hints/dotnet.py's ambiguous type-name handling."""
    wiring_path = str(tmp_path / "Wiring.vb")
    a_path = str(tmp_path / "A.vb")
    b_path = str(tmp_path / "B.vb")
    file_results = {
        wiring_path: {
            "symbols": [],
            "eventWiring": [{"kind": "add_handler", "line": 5, "targetName": "Handle"}],
        },
        a_path: {
            "symbols": [{"name": "Handle", "kind": "method", "startLine": 1, "parentName": "A"}],
            "eventWiring": [],
        },
        b_path: {
            "symbols": [{"name": "Handle", "kind": "method", "startLine": 1, "parentName": "B"}],
            "eventWiring": [],
        },
    }
    _patch_client(monkeypatch, file_results)

    edges = handles_mod.VbHandlesDynamicHints().extract(tmp_path)

    targets = {e.target for e in edges}
    assert targets == {"A.vb::A::Handle", "B.vb::B::Handle"}
