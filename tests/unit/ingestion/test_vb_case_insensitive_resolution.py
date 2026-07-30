"""VB.NET is case-insensitive; C# (and everything else) stays case-sensitive.

vb-support.md §5.5: "Symbol IDs and the import/call/heritage name index must
be matched case-insensitively for VB, while Symbol.name keeps the
declaration's original casing for display." These pin that behavior directly
against ``CallResolver``/``HeritageResolver`` with synthetic ``ParsedFile``
fixtures — no sidecar or tree-sitter parse needed.
"""

from __future__ import annotations

from datetime import datetime

from repowise.core.ingestion.call_resolver import CallResolver
from repowise.core.ingestion.heritage_resolver import HeritageResolver
from repowise.core.ingestion.models import (
    CallSite,
    FileInfo,
    HeritageRelation,
    ParsedFile,
    Symbol,
)


def _file_info(path: str, lang: str) -> FileInfo:
    return FileInfo(
        path=path,
        abs_path=f"C:/repo/{path}",
        language=lang,  # type: ignore[arg-type]
        size_bytes=1,
        git_hash="",
        last_modified=datetime.now(),
        is_test=False,
        is_config=False,
        is_api_contract=False,
        is_entry_point=False,
    )


def _symbol(path: str, name: str, kind: str, *, parent_name: str | None = None) -> Symbol:
    sym_id = f"{path}::{parent_name}::{name}" if parent_name else f"{path}::{name}"
    return Symbol(
        id=sym_id,
        name=name,
        qualified_name=name,
        kind=kind,
        signature=name,
        start_line=1,
        end_line=5,
        docstring=None,
        parent_name=parent_name,
    )


class TestCallResolutionCaseSensitivity:
    def test_vb_same_file_call_resolves_with_different_casing(self) -> None:
        path = "src/Calc.vb"
        fi = _file_info(path, "vbnet")
        add = _symbol(path, "Add", "method", parent_name="Calculator")
        call = CallSite(target_name="add", receiver_name=None, caller_symbol_id=None, line=3, argument_count=0)
        parsed = {path: ParsedFile(file_info=fi, symbols=[add], imports=[], exports=[], calls=[call])}

        resolver = CallResolver(parsed, {})
        resolved = resolver.resolve_file(path, parsed[path].calls)

        assert any(rc.callee_id == add.id for rc in resolved), resolved

    def test_vb_global_unique_match_ignores_case(self) -> None:
        defining_path = "src/Helper.vb"
        caller_path = "src/Caller.vb"
        helper = _symbol(defining_path, "Helper", "function")
        call = CallSite(
            target_name="HELPER", receiver_name=None, caller_symbol_id=None, line=1, argument_count=0
        )
        parsed = {
            defining_path: ParsedFile(
                file_info=_file_info(defining_path, "vbnet"),
                symbols=[helper],
                imports=[],
                exports=[],
            ),
            caller_path: ParsedFile(
                file_info=_file_info(caller_path, "vbnet"),
                symbols=[],
                imports=[],
                exports=[],
                calls=[call],
            ),
        }

        resolver = CallResolver(parsed, {})
        resolved = resolver.resolve_file(caller_path, parsed[caller_path].calls)

        assert any(rc.callee_id == helper.id for rc in resolved), resolved

    def test_csharp_call_stays_case_sensitive(self) -> None:
        """Regression guard: the VB casefold path must not leak into C#."""
        path = "src/Calc.cs"
        fi = _file_info(path, "csharp")
        add = _symbol(path, "Add", "method", parent_name="Calculator")
        call = CallSite(target_name="add", receiver_name=None, caller_symbol_id=None, line=3, argument_count=0)
        parsed = {path: ParsedFile(file_info=fi, symbols=[add], imports=[], exports=[], calls=[call])}

        resolver = CallResolver(parsed, {})
        resolved = resolver.resolve_file(path, parsed[path].calls)

        assert resolved == [], f"C# must stay case-sensitive: {resolved}"


class TestHeritageResolutionCaseSensitivity:
    def test_vb_inherits_resolves_with_different_casing(self) -> None:
        path = "src/Calc.vb"
        base = _symbol(path, "BaseCalc", "class")
        rel = HeritageRelation(child_name="Calculator", parent_name="basecalc", kind="extends", line=2)
        parsed = {
            path: ParsedFile(
                file_info=_file_info(path, "vbnet"),
                symbols=[base],
                imports=[],
                exports=[],
                heritage=[rel],
            )
        }

        resolver = HeritageResolver(parsed, {})
        resolved = resolver.resolve_file(path, parsed[path].heritage)

        assert any(rh.parent_id == base.id for rh in resolved), resolved

    def test_csharp_inherits_stays_case_sensitive(self) -> None:
        path = "src/Calc.cs"
        base = _symbol(path, "BaseCalc", "class")
        rel = HeritageRelation(child_name="Calculator", parent_name="basecalc", kind="extends", line=2)
        parsed = {
            path: ParsedFile(
                file_info=_file_info(path, "csharp"),
                symbols=[base],
                imports=[],
                exports=[],
                heritage=[rel],
            )
        }

        resolver = HeritageResolver(parsed, {})
        resolved = resolver.resolve_file(path, parsed[path].heritage)

        assert resolved == [], f"C# must stay case-sensitive: {resolved}"
