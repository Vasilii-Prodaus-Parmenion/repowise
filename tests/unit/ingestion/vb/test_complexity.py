"""Sidecar JSON -> ``FileComplexity`` mapping (D5, phase 5, vb-support.md §8).

No SDK or subprocess needed: feeds a recorded-shape sidecar "complexity"
payload straight through ``vb/complexity.py::walk_vb_file`` via a fake
client standing in for the run-scoped singleton (monkeypatched
``lookup_file_result``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from repowise.core.ingestion.vb import complexity as complexity_mod


class _FakeFileInfo:
    def __init__(self, abs_path: str) -> None:
        self.abs_path = abs_path


def _patch_lookup(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any] | None) -> None:
    monkeypatch.setattr(complexity_mod, "lookup_file_result", lambda abs_path: result)


def test_sidecar_miss_degrades_to_empty_file_complexity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_lookup(monkeypatch, None)
    fc = complexity_mod.walk_vb_file(_FakeFileInfo(str(tmp_path / "Foo.vb")), b"")
    assert fc.functions == []
    assert fc.classes == []
    assert fc.file_nloc == 0
    assert fc.error_handling_hits == []
    assert fc.perf_hits == []


def test_function_complexity_fields_map_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = {
        "complexity": {
            "functions": [
                {
                    "name": "Button1_Click",
                    "startLine": 9,
                    "endLine": 20,
                    "ccn": 5,
                    "maxNesting": 2,
                    "cognitive": 5,
                    "nloc": 12,
                    "bumps": 1,
                    "paramCount": 2,
                    "complexConditions": [
                        {"line": 13, "operatorCount": 1, "enclosingConstruct": "if"},
                    ],
                },
            ],
            "classes": [],
            "fileNloc": 28,
            "errorHandlingHits": [],
            "perfHits": [],
        }
    }
    _patch_lookup(monkeypatch, result)

    fc = complexity_mod.walk_vb_file(_FakeFileInfo(str(tmp_path / "Foo.vb")), b"")

    (fn,) = fc.functions
    assert fn.name == "Button1_Click"
    assert fn.ccn == 5
    assert fn.cognitive == 5
    assert fn.max_nesting == 2
    assert fn.nloc == 12
    assert fn.bumps == 1
    assert fn.param_count == 2
    assert len(fn.complex_conditions) == 1
    assert fn.complex_conditions[0].enclosing_construct == "if"
    assert fc.file_nloc == 28


def test_class_complexity_and_cohesion_fields_map_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = {
        "complexity": {
            "functions": [],
            "classes": [
                {
                    "name": "MainForm",
                    "startLine": 4,
                    "endLine": 31,
                    "methodCount": 3,
                    "totalNloc": 20,
                    "methods": [
                        {
                            "name": "Button1_Click",
                            "startLine": 9,
                            "endLine": 20,
                            "ccn": 5,
                            "maxNesting": 2,
                            "cognitive": 5,
                            "nloc": 12,
                        },
                    ],
                    "lcom4": 2,
                    "maxMethodCcn": 5,
                    "fieldCount": 3,
                    "tcc": 0.0,
                },
            ],
            "fileNloc": 28,
            "errorHandlingHits": [],
            "perfHits": [],
        }
    }
    _patch_lookup(monkeypatch, result)

    fc = complexity_mod.walk_vb_file(_FakeFileInfo(str(tmp_path / "Foo.vb")), b"")

    (cls,) = fc.classes
    assert cls.name == "MainForm"
    assert cls.method_count == 3
    assert cls.total_nloc == 20
    assert len(cls.methods) == 1
    assert cls.lcom4 == 2
    assert cls.max_method_ccn == 5
    assert cls.field_count == 3
    assert cls.tcc == 0.0
    # No CohesionGroup breakdown from the sidecar — degrades to the same
    # "no signal" empty list every other language's safety valve returns.
    assert cls.components == []


def test_error_handling_and_perf_hits_map_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = {
        "complexity": {
            "functions": [],
            "classes": [],
            "fileNloc": 5,
            "errorHandlingHits": [
                {"kind": "on_error_resume_next", "line": 10},
                {"kind": "swallowed_catch", "line": 25},
            ],
            "perfHits": [
                {
                    "kind": "string_concat_in_loop",
                    "line": 14,
                    "function": "Button1_Click",
                    "detail": "",
                },
                {
                    "kind": "blocking_sync_in_async",
                    "line": 30,
                    "function": "LoadAsync",
                    "detail": ".Result",
                },
            ],
        }
    }
    _patch_lookup(monkeypatch, result)

    fc = complexity_mod.walk_vb_file(_FakeFileInfo(str(tmp_path / "Foo.vb")), b"")

    kinds = {h.kind for h in fc.error_handling_hits}
    assert kinds == {"on_error_resume_next", "swallowed_catch"}
    perf_kinds = {h.kind for h in fc.perf_hits}
    assert perf_kinds == {"string_concat_in_loop", "blocking_sync_in_async"}
    blocking = next(h for h in fc.perf_hits if h.kind == "blocking_sync_in_async")
    assert blocking.detail == ".Result"
    assert blocking.function == "LoadAsync"


def test_engine_walk_dispatches_vbnet_to_walk_vb_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """HealthAnalyzer._walk must branch on language == "vbnet" exactly like
    it already does for "sql", rather than falling through to walk_file."""
    from repowise.core.analysis.health.complexity.models import FileComplexity
    from repowise.core.analysis.health.engine import HealthAnalyzer

    sentinel = FileComplexity(functions=[], classes=[], file_nloc=999)
    called_with: dict[str, Any] = {}

    def _fake_walk_vb_file(file_info: Any, source: bytes) -> FileComplexity:
        called_with["file_info"] = file_info
        return sentinel

    monkeypatch.setattr("repowise.core.ingestion.vb.complexity.walk_vb_file", _fake_walk_vb_file)

    analyzer = HealthAnalyzer(graph=None)

    file_info = _FakeFileInfo(__file__)  # any real file, so Path(...).read_bytes() succeeds
    file_info.language = "vbnet"

    class _PF:
        pass

    pf = _PF()
    pf.file_info = file_info

    result = analyzer._walk(pf)
    assert result is sentinel
    assert called_with["file_info"] is file_info
