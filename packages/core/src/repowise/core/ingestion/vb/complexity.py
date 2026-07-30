"""Sidecar JSON -> ``FileComplexity`` (D5, phase 5, vb-support.md §8).

``HealthAnalyzer._walk`` already branches for a language with no
tree-sitter grammar (see ``sql_complexity.py``); VB slots in identically —
``walk_vb_file`` is the branch target, and ``sql_complexity.py`` is the
shape this module follows.

The difference: ``walk_sql_file`` computes its metrics itself from source
text, whereas VB's come from the sidecar during the parse phase, and by the
time health runs that phase is long over. The parse phase stashed each
file's raw "parse" result (including the ``complexity`` payload) on the
run-scoped sidecar client singleton (see ``vb/sidecar.py``'s
``file_results`` / ``lookup_file_result``); this module is a pure lookup +
JSON-to-dataclass conversion, no computation of its own.

A miss — a VB file health sees but the parse phase didn't populate (a
parse-cache hit that reused a prior run's ``ParsedFile`` without calling the
sidecar this run, or a run where the sidecar never started) — degrades to
an empty ``FileComplexity``, exactly like ``walk_file`` already does for
any language with no complexity signal. Never a crash, never a fabricated
metric.
"""

from __future__ import annotations

from typing import Any

from repowise.core.analysis.health.complexity.models import (
    ClassComplexity,
    ConditionComplexity,
    ErrorHandlingHit,
    FileComplexity,
    FunctionComplexity,
    PerfHit,
)
from repowise.core.ingestion.vb.sidecar import lookup_file_result


def _condition_from_dto(dto: dict[str, Any]) -> ConditionComplexity:
    return ConditionComplexity(
        line=dto.get("line", 1),
        operator_count=dto.get("operatorCount", 0),
        enclosing_construct=dto.get("enclosingConstruct", ""),
    )


def _function_from_dto(dto: dict[str, Any]) -> FunctionComplexity:
    start_line = dto.get("startLine", 1)
    return FunctionComplexity(
        name=dto.get("name", ""),
        start_line=start_line,
        end_line=dto.get("endLine", start_line),
        ccn=dto.get("ccn", 1),
        max_nesting=dto.get("maxNesting", 0),
        cognitive=dto.get("cognitive", 0),
        nloc=dto.get("nloc", 0),
        bumps=dto.get("bumps", 0),
        param_count=dto.get("paramCount", 0),
        complex_conditions=[_condition_from_dto(c) for c in dto.get("complexConditions") or []],
    )


def _class_from_dto(dto: dict[str, Any]) -> ClassComplexity:
    start_line = dto.get("startLine", 1)
    return ClassComplexity(
        name=dto.get("name", ""),
        start_line=start_line,
        end_line=dto.get("endLine", start_line),
        method_count=dto.get("methodCount", 0),
        total_nloc=dto.get("totalNloc", 0),
        methods=[_function_from_dto(m) for m in dto.get("methods") or []],
        lcom4=dto.get("lcom4", 1),
        max_method_ccn=dto.get("maxMethodCcn", 0),
        field_count=dto.get("fieldCount", 0),
        # `components` (CohesionGroup breakdown) isn't computed by the
        # sidecar — the Extract Class detector that reads it treats VB
        # class findings as lower-confidence per vb-support.md §8/§11
        # regardless, and an empty list is the same "no signal" shape
        # every other language's safety valve already returns.
        tcc=dto.get("tcc", 1.0),
    )


def _error_handling_from_dto(dto: dict[str, Any]) -> ErrorHandlingHit:
    return ErrorHandlingHit(kind=dto.get("kind", ""), line=dto.get("line", 1))


def _perf_from_dto(dto: dict[str, Any]) -> PerfHit:
    return PerfHit(
        kind=dto.get("kind", ""),
        line=dto.get("line", 1),
        function=dto.get("function"),
        detail=dto.get("detail", ""),
    )


def walk_vb_file(file_info: Any, source: bytes) -> FileComplexity:
    """Health-walk one ``.vb`` file. *source* is unused (metrics were already
    computed by the sidecar during parsing) but kept in the signature to
    match ``walk_sql_file``'s ``(file_info, source)`` shape, which
    ``HealthAnalyzer._walk`` calls uniformly."""
    del source
    result = lookup_file_result(file_info.abs_path)
    complexity = (result or {}).get("complexity") if result else None
    if not complexity:
        return FileComplexity(functions=[], classes=[])

    return FileComplexity(
        functions=[_function_from_dto(f) for f in complexity.get("functions") or []],
        classes=[_class_from_dto(c) for c in complexity.get("classes") or []],
        file_nloc=complexity.get("fileNloc", 0),
        error_handling_hits=[
            _error_handling_from_dto(h) for h in complexity.get("errorHandlingHits") or []
        ],
        perf_hits=[_perf_from_dto(h) for h in complexity.get("perfHits") or []],
    )
