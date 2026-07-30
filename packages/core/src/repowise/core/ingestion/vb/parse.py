"""Sidecar JSON -> ``ParsedFile`` (see vb-support.md §5.5, §5.3).

Translates one ``results[]`` entry from the sidecar's ``"parse"`` response
into the exact same ``ParsedFile``/``Symbol``/``Import``/``CallSite``/
``HeritageRelation`` shapes every tree-sitter language produces, so nothing
downstream (``GraphBuilder``, ``CallResolver``, ``HeritageResolver``, health)
needs to know VB came from a different process entirely.
"""

from __future__ import annotations

from typing import Any

from repowise.core.ingestion.models import (
    CallSite,
    FileInfo,
    HeritageRelation,
    Import,
    NamedBinding,
    ParsedFile,
    Symbol,
    compute_content_hash,
)

#: Symbol kinds that can contain a call site (used to find the "enclosing
#: symbol" for a flat (line, target) call the sidecar reports — see
#: ``_enclosing_symbol_id``).
_CALLABLE_KINDS = frozenset({"method", "function"})


def vb_symbol_id(path: str, parent_name: str | None, name: str) -> str:
    """Match the ``{path}::{name}`` / ``{path}::{parent}::{name}`` convention
    every other language's parser builds (see parser.py's symbol extraction)."""
    if parent_name:
        return f"{path}::{parent_name}::{name}"
    return f"{path}::{name}"


def _symbol_from_dto(path: str, dto: dict[str, Any]) -> Symbol:
    parent_name = dto.get("parentName")
    name = dto.get("name", "")
    start_line = dto.get("startLine", 1)
    return Symbol(
        id=vb_symbol_id(path, parent_name, name),
        name=name,
        qualified_name=dto.get("qualifiedName") or name,
        kind=dto.get("kind", "variable"),
        signature=dto.get("signature", ""),
        start_line=start_line,
        end_line=dto.get("endLine", start_line),
        docstring=dto.get("docstring"),
        visibility=dto.get("visibility", "public"),
        is_async=bool(dto.get("isAsync", False)),
        language="vbnet",
        parent_name=parent_name,
    )


def _binding_from_dto(dto: dict[str, Any]) -> NamedBinding:
    return NamedBinding(
        local_name=dto.get("localName", ""),
        exported_name=dto.get("exportedName"),
        source_file=None,  # resolved later, during graph build, like every other language
        is_module_alias=bool(dto.get("isModuleAlias", False)),
    )


def _import_from_dto(dto: dict[str, Any]) -> Import:
    return Import(
        raw_statement=dto.get("rawStatement", ""),
        module_path=dto.get("modulePath", ""),
        imported_names=list(dto.get("importedNames") or []),
        is_relative=False,
        resolved_file=None,
        bindings=[_binding_from_dto(b) for b in dto.get("bindings") or []],
    )


def _heritage_from_dto(dto: dict[str, Any]) -> HeritageRelation:
    return HeritageRelation(
        child_name=dto.get("childName", ""),
        parent_name=dto.get("parentName", ""),
        kind=dto.get("kind", "extends"),
        line=dto.get("line", 1),
    )


def _enclosing_symbol_id(symbols: list[Symbol], line: int) -> str | None:
    """Innermost callable symbol whose ``[start_line, end_line]`` contains *line*.

    The sidecar reports calls as flat ``(line, target)`` pairs — Roslyn's
    syntax tree has no notion of repowise's ``path::name`` id convention —
    so the enclosing method/function is recovered here by narrowest-span
    containment, standing in for what a tree-sitter walker gets for free
    from the AST itself. Calls outside any callable (module/top-level) fall
    back to ``None``, exactly like every other language's parser, and
    ``CallResolver.resolve_file`` assigns those to a synthetic
    ``__module__`` symbol.
    """
    best: Symbol | None = None
    for sym in symbols:
        if sym.kind not in _CALLABLE_KINDS:
            continue
        if sym.start_line <= line <= sym.end_line and (
            best is None or (sym.end_line - sym.start_line) < (best.end_line - best.start_line)
        ):
            best = sym
    return best.id if best else None


def _call_from_dto(dto: dict[str, Any], caller_symbol_id: str | None) -> CallSite:
    return CallSite(
        target_name=dto.get("targetName", ""),
        receiver_name=dto.get("receiverName"),
        caller_symbol_id=caller_symbol_id,
        line=dto.get("line", 1),
        argument_count=dto.get("argumentCount"),
    )


def sidecar_result_to_parsed_file(
    file_info: FileInfo, source: bytes, result: dict[str, Any]
) -> ParsedFile:
    """Convert one sidecar ``results[]`` entry into a :class:`ParsedFile`."""
    path = file_info.path
    symbols = [_symbol_from_dto(path, s) for s in result.get("symbols") or []]
    calls = [
        _call_from_dto(c, _enclosing_symbol_id(symbols, c.get("line", 1)))
        for c in result.get("calls") or []
    ]
    return ParsedFile(
        file_info=file_info,
        symbols=symbols,
        imports=[_import_from_dto(i) for i in result.get("imports") or []],
        exports=[s.name for s in symbols],
        calls=calls,
        heritage=[_heritage_from_dto(h) for h in result.get("heritage") or []],
        docstring=result.get("docstring"),
        parse_errors=list(result.get("parseErrors") or []),
        content_hash=compute_content_hash(source),
    )


def empty_parsed_file(
    file_info: FileInfo, source: bytes, *, parse_errors: list[str] | None = None
) -> ParsedFile:
    """Degrade-to-nothing shape for a VB file the sidecar failed to parse.

    Mirrors ``special_handlers.py``'s ``_empty()`` — a bad file never aborts
    the batch or the run, it just contributes no symbols.
    """
    return ParsedFile(
        file_info=file_info,
        symbols=[],
        imports=[],
        exports=[],
        parse_errors=parse_errors or [],
        content_hash=compute_content_hash(source),
    )
