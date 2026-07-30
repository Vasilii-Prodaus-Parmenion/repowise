"""Sidecar JSON -> ParsedFile mapping (vb-support.md §5.5, §5.3).

No SDK or subprocess needed: these feed recorded-shape sidecar response
dicts (exactly what ``VbSidecarClient.request`` would hand back after JSON
decoding) straight through ``vb/parse.py``.
"""

from __future__ import annotations

from datetime import datetime

from repowise.core.ingestion.models import FileInfo
from repowise.core.ingestion.vb.parse import (
    empty_parsed_file,
    sidecar_result_to_parsed_file,
)


def _file_info(path: str = "src/Forms/MainForm.vb") -> FileInfo:
    return FileInfo(
        path=path,
        abs_path=f"C:/repo/{path}",
        language="vbnet",
        size_bytes=100,
        git_hash="",
        last_modified=datetime.now(),
        is_test=False,
        is_config=False,
        is_api_contract=False,
        is_entry_point=False,
    )


def test_symbol_mapping_matches_id_and_kind_conventions() -> None:
    fi = _file_info()
    result = {
        "path": fi.path,
        "symbols": [
            {
                "name": "Calculator",
                "qualifiedName": "Acme.App.Calculator",
                "kind": "class",
                "signature": "Public Class Calculator",
                "startLine": 3,
                "endLine": 20,
                "docstring": None,
                "visibility": "public",
                "isAsync": False,
                "parentName": None,
            },
            {
                "name": "Add",
                "qualifiedName": "Acme.App.Calculator.Add",
                "kind": "method",
                "signature": "Public Function Add(a As Integer) As Integer",
                "startLine": 5,
                "endLine": 8,
                "docstring": "Adds a number.",
                "visibility": "public",
                "isAsync": False,
                "parentName": "Calculator",
            },
        ],
        "imports": [],
        "calls": [],
        "heritage": [],
        "docstring": None,
        "parseErrors": [],
    }

    parsed = sidecar_result_to_parsed_file(fi, b"source", result)

    class_sym, method_sym = parsed.symbols
    assert class_sym.id == "src/Forms/MainForm.vb::Calculator"
    assert class_sym.parent_name is None
    assert method_sym.id == "src/Forms/MainForm.vb::Calculator::Add"
    assert method_sym.parent_name == "Calculator"
    assert method_sym.docstring == "Adds a number."
    assert method_sym.language == "vbnet"
    assert parsed.exports == ["Calculator", "Add"]


def test_module_level_sub_is_kind_function_not_method() -> None:
    """Per the mapping table: Sub/Function in a Module -> "function", not "method"."""
    fi = _file_info()
    result = {
        "path": fi.path,
        "symbols": [
            {
                "name": "Helpers",
                "qualifiedName": "Acme.Helpers",
                "kind": "module",
                "signature": "Public Module Helpers",
                "startLine": 1,
                "endLine": 5,
                "visibility": "public",
                "parentName": None,
            },
            {
                "name": "DoWork",
                "qualifiedName": "Acme.Helpers.DoWork",
                "kind": "function",
                "signature": "Public Sub DoWork()",
                "startLine": 2,
                "endLine": 4,
                "visibility": "public",
                "parentName": "Helpers",
            },
        ],
    }

    parsed = sidecar_result_to_parsed_file(fi, b"source", result)
    do_work = next(s for s in parsed.symbols if s.name == "DoWork")
    assert do_work.kind == "function"


def test_import_binding_with_alias() -> None:
    fi = _file_info()
    result = {
        "path": fi.path,
        "imports": [
            {
                "rawStatement": "Imports IO = System.IO",
                "modulePath": "System.IO",
                "importedNames": ["*"],
                "bindings": [{"localName": "IO", "exportedName": None, "isModuleAlias": True}],
            }
        ],
    }

    parsed = sidecar_result_to_parsed_file(fi, b"source", result)
    (imp,) = parsed.imports
    assert imp.module_path == "System.IO"
    assert imp.bindings[0].local_name == "IO"
    assert imp.bindings[0].is_module_alias is True


def test_heritage_extends_and_implements() -> None:
    fi = _file_info()
    result = {
        "path": fi.path,
        "heritage": [
            {"childName": "Calculator", "parentName": "BaseCalc", "kind": "extends", "line": 4},
            {"childName": "Calculator", "parentName": "ICalc", "kind": "implements", "line": 5},
        ],
    }

    parsed = sidecar_result_to_parsed_file(fi, b"source", result)
    kinds = {(h.parent_name, h.kind) for h in parsed.heritage}
    assert kinds == {("BaseCalc", "extends"), ("ICalc", "implements")}


def test_call_caller_symbol_id_resolved_by_enclosing_method_range() -> None:
    """The sidecar reports flat (line, target) calls; vb/parse.py must find
    the innermost enclosing method/function by line-range containment."""
    fi = _file_info()
    result = {
        "path": fi.path,
        "symbols": [
            {
                "name": "Add",
                "qualifiedName": "Acme.Calculator.Add",
                "kind": "method",
                "signature": "Public Function Add() As Integer",
                "startLine": 5,
                "endLine": 9,
                "visibility": "public",
                "parentName": "Calculator",
            },
        ],
        "calls": [
            {"targetName": "WriteLine", "receiverName": "Console", "line": 7, "argumentCount": 1},
            {"targetName": "TopLevelCall", "receiverName": None, "line": 20, "argumentCount": 0},
        ],
    }

    parsed = sidecar_result_to_parsed_file(fi, b"source", result)
    in_method = next(c for c in parsed.calls if c.target_name == "WriteLine")
    outside = next(c for c in parsed.calls if c.target_name == "TopLevelCall")

    assert in_method.caller_symbol_id == "src/Forms/MainForm.vb::Calculator::Add"
    assert outside.caller_symbol_id is None


def test_empty_parsed_file_degrades_to_nothing_on_sidecar_miss() -> None:
    fi = _file_info()
    parsed = empty_parsed_file(fi, b"source", parse_errors=["boom"])
    assert parsed.symbols == []
    assert parsed.imports == []
    assert parsed.exports == []
    assert parsed.parse_errors == ["boom"]
    assert parsed.content_hash  # still computed from the real bytes


def test_vbnet_is_a_registered_language_tag() -> None:
    from repowise.core.ingestion.models import EXTENSION_TO_LANGUAGE, LanguageTag

    assert "vbnet" in LanguageTag.__args__
    assert EXTENSION_TO_LANGUAGE.get(".vb") == "vbnet"


def test_vbnet_spec_has_no_tree_sitter_grammar() -> None:
    from repowise.core.ingestion.languages.specs import ALL_SPECS

    spec = next(s for s in ALL_SPECS if s.tag == "vbnet")
    assert spec.grammar_package is None
    assert spec.scm_file is None
    assert spec.is_passthrough is False
    assert ".vb" in spec.extensions
