"""``eventWiring`` facts -> ``DynamicEdge``s (D8, phase 4, vb-support.md §6.1).

VB's ``Handles`` clause wires an event handler to its source with no call
site anywhere in the source text — the compiler emits the subscription.
``AddHandler``/``RemoveHandler`` are the explicit-statement form of the same
idea. Without a graph edge standing in for that implicit wiring, every
handler in a VB codebase reads as an uncalled private method to
``DeadCodeAnalyzer`` (see ``analysis/dead_code/analyzer.py``).

The sidecar's ``HandlesExtractor`` (``sidecar_src/HandlesExtractor.cs``)
already reports these facts per file as the ``eventWiring`` field on each
"parse" response entry; this module is the Python-side consumer, run as a
:class:`~repowise.core.ingestion.dynamic_hints.base.DynamicHintExtractor`
so it slots into the same ``HintRegistry`` every other language's dynamic
hints go through.

Resolution
----------
- ``"handles"`` entries carry the *handler method's own declaration line*
  (``HandlesExtractor.cs`` reports ``LineOf(node.SpanStart)`` for the
  ``MethodStatementSyntax`` itself) — the handler is always in the same
  file as its own ``Handles`` clause, so no cross-file lookup is needed to
  find it. What *may* live in a sibling ``.Designer.vb`` partial is the
  ``WithEvents`` field being handled (§6.1's cross-file case) — but that
  fact isn't needed to mark the handler reached, only to explain *why* it
  fires, so it is not resolved here.
- ``"add_handler"``/``"remove_handler"`` entries carry only the
  ``AddressOf`` target's bare name (Roslyn gives us no semantic model per
  D1, so there is no qualified name to resolve). Matched case-insensitively
  (VB identifiers, D8) against every VB method/function symbol parsed this
  run, preferring a same-file match; an ambiguous bare name outside its own
  file fans out to every candidate rather than guessing — over-emission is
  the safer failure mode for a dead-code rescue signal (same tradeoff
  ``dynamic_hints/dotnet.py`` makes for its type-name lookups).

Both edge shapes point at the *symbol* node, not just the file (``source``
is the file containing the wiring statement, ``target`` is the handler's
symbol id) — a plain file-level edge would only rescue the file's public
exports (``DeadCodeAnalyzer._detect_unused_exports``'s file-dynamic check),
not the private handlers ``_detect_unused_internals`` flags on its own,
narrower, per-symbol "calls" check. See ``analysis/dead_code/analyzer.py``
for the ``"dynamic"`` edge-type case that check now also honors.

Only files the sidecar actually parsed *this run* carry an entry in
``client.file_results`` (a parse-cache hit reuses last run's ``ParsedFile``
and never calls the sidecar) — a miss degrades to silence, matching the
documented contract in vb-support.md §8.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from repowise.core.ingestion.vb.parse import vb_symbol_id
from repowise.core.ingestion.vb.sidecar import get_sidecar

from ..dynamic_hints.base import DynamicEdge, DynamicHintExtractor

_CALLABLE_KINDS = frozenset({"method", "function"})


def _rel_path(abs_path: str, repo_root_resolved: Path) -> str | None:
    try:
        return Path(abs_path).resolve().relative_to(repo_root_resolved).as_posix()
    except ValueError:
        return None


def _find_handler_symbol_id(rel: str, symbols: list[dict[str, Any]], line: int) -> str | None:
    """Locate the callable symbol whose declaration starts at *line*.

    ``HandlesExtractor.cs`` reports the ``Handles`` clause's line as the
    method statement's own ``SpanStart`` — an exact match, not a containment
    search like call-site resolution needs.
    """
    for sym in symbols:
        if sym.get("kind") not in _CALLABLE_KINDS:
            continue
        if sym.get("startLine") == line:
            return vb_symbol_id(rel, sym.get("parentName"), sym.get("name", ""))
    return None


class VbHandlesDynamicHints(DynamicHintExtractor):
    """Emit ``dynamic`` edges from VB ``Handles``/``AddHandler`` wiring."""

    name = "vb_handles"

    def extract(self, repo_root: Path) -> list[DynamicEdge]:
        client = get_sidecar(repo_root)
        file_results = client.file_results
        if not file_results:
            return []

        repo_root_resolved = repo_root.resolve()

        rel_by_abs: dict[str, str] = {}
        for abs_path in file_results:
            rel = _rel_path(abs_path, repo_root_resolved)
            if rel is not None:
                rel_by_abs[abs_path] = rel
        if not rel_by_abs:
            return []

        # Every VB method/function symbol parsed this run, indexed
        # case-insensitively by bare name (D8) for AddressOf resolution.
        symbols_by_rel: dict[str, list[dict[str, Any]]] = {}
        name_index: dict[str, list[tuple[str, str]]] = {}
        for abs_path, rel in rel_by_abs.items():
            syms = file_results[abs_path].get("symbols") or []
            symbols_by_rel[rel] = syms
            for sym in syms:
                if sym.get("kind") not in _CALLABLE_KINDS:
                    continue
                name = sym.get("name") or ""
                if not name:
                    continue
                sym_id = vb_symbol_id(rel, sym.get("parentName"), name)
                name_index.setdefault(name.casefold(), []).append((rel, sym_id))

        edges: list[DynamicEdge] = []
        for abs_path, rel in rel_by_abs.items():
            wiring = file_results[abs_path].get("eventWiring") or []
            if not wiring:
                continue
            own_symbols = symbols_by_rel.get(rel, [])
            for entry in wiring:
                kind = entry.get("kind")
                if kind == "handles":
                    line = entry.get("line")
                    if line is None:
                        continue
                    target_id = _find_handler_symbol_id(rel, own_symbols, line)
                    if target_id is not None:
                        edges.append(
                            DynamicEdge(
                                source=rel,
                                target=target_id,
                                edge_type="dynamic",
                                hint_source=self.name,
                            )
                        )
                elif kind in ("add_handler", "remove_handler"):
                    target_name = entry.get("targetName")
                    if not target_name:
                        continue
                    candidates = name_index.get(target_name.casefold())
                    if not candidates:
                        continue
                    same_file = [c for c in candidates if c[0] == rel]
                    for _cand_rel, sym_id in same_file or candidates:
                        edges.append(
                            DynamicEdge(
                                source=rel,
                                target=sym_id,
                                edge_type="dynamic",
                                hint_source=self.name,
                            )
                        )
        return edges
