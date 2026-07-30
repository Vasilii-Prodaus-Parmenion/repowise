"""VB.NET same-namespace + project-Import implicit reference resolution.

Mirrors :mod:`.csharp_same_namespace` for VB.NET (vb-support.md §7): VB
needs no ``Imports`` statement for types declared in the same effective
namespace, and a project-level ``<Import Include="X"/>`` item (D7) makes a
whole namespace visible to every file in the project with no per-file
``Imports`` at all. Both cases produce no edge under plain import
resolution, which is exactly the gap the C# pass exists to close — this
closes the same gap for VB.

Two things differ from the C# pass:

- Every namespace compared here is first made "effective" — VB's
  ``RootNamespace`` is prepended to a file's declared ``Namespace`` block
  (and *is* the namespace when a file declares none at all,
  vb-support.md §5.5) — then casefolded, because VB matches namespaces
  and type names case-insensitively (D8). The C# pass compares raw,
  case-sensitive namespace strings; this one cannot.
- The project-wide tier reads VB's ``<Import Include="X"/>`` items
  (already merged into ``DotNetProjectIndex.project_globals`` alongside
  C#'s ``<Using Include="X"/>`` by ``index.py`` — see msbuild.py) rather
  than scanning source for ``global using``/``Imports`` directives, since
  VB has no per-file "global" directive equivalent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .csharp_same_namespace import _BCL_COMMON_TYPES

if TYPE_CHECKING:
    import networkx as nx

    from ..resolvers.dotnet.index import DotNetProjectIndex

# Capitalized identifier — candidate type reference. VB codebases follow
# PascalCase for types by convention even though the language itself is
# case-insensitive; matching is still done casefolded below.
_TYPE_IDENT_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")

# ``Imports Foo.Bar`` — plain namespace imports. Deliberately excludes the
# alias form (``Imports Alias = Foo.Bar``) and XML-namespace imports
# (``Imports <xmlns:...>``): both fail the ``\s*$`` anchor since something
# other than whitespace follows the captured identifier.
_IMPORTS_NS_RE = re.compile(r"^[ \t]*Imports\s+([A-Za-z_][\w.]*)\s*$", re.MULTILINE | re.IGNORECASE)

# ``Imports Alias = Foo.Bar.Baz`` — the alias name shadows bare identifiers.
_IMPORTS_ALIAS_RE = re.compile(r"^[ \t]*Imports\s+([A-Za-z_]\w*)\s*=", re.MULTILINE | re.IGNORECASE)

_SAME_NAMESPACE_HINT = "same_namespace"
_PROJECT_IMPORT_HINT = "global_using"


def collect_vb_source_texts(parsed_files: dict[str, Any]) -> dict[str, str]:
    """Read each parsed VB file's source from disk, keyed by repo path."""
    out: dict[str, str] = {}
    for path, parsed in parsed_files.items():
        if parsed.file_info.language != "vbnet":
            continue
        try:
            out[path] = Path(parsed.file_info.abs_path).read_text(
                encoding="utf-8-sig", errors="ignore"
            )
        except OSError:
            continue
    return out


def _effective_namespace(declared_ns: str, root_namespace: str) -> str:
    """Prepend *root_namespace* the way VB's compiler does (vb-support.md §5.5)."""
    if not root_namespace:
        return declared_ns
    if not declared_ns:
        return root_namespace
    return f"{root_namespace}.{declared_ns}"


def build_namespace_type_index_vb(
    vb_texts: dict[str, str], root_namespaces: dict[str, str]
) -> dict[str, dict[str, list[str]]]:
    """Build ``casefolded effective namespace -> {casefolded type name: [paths]}``.

    *root_namespaces* maps repo-relative path → owning project's
    RootNamespace (``""`` for files with no/unnamespaced project).
    """
    from ..resolvers.dotnet.namespace_map import scan_type_declarations_vb

    ns_types: dict[str, dict[str, list[str]]] = {}
    for path in sorted(vb_texts):
        root_ns = root_namespaces.get(path, "")
        for decl in scan_type_declarations_vb(vb_texts[path]):
            ns_key = _effective_namespace(decl.namespace, root_ns).casefold()
            bucket = ns_types.setdefault(ns_key, {})
            files = bucket.setdefault(decl.name.casefold(), [])
            if path not in files:
                files.append(path)
    return ns_types


def resolve_vb_same_namespace_refs(
    graph: nx.DiGraph,
    dotnet_index: DotNetProjectIndex | None,
    vb_texts: dict[str, str],
    repo_path: Path | None,
) -> int:
    """Emit same-namespace / project-Import ``imports`` edges for VB files.

    *vb_texts* maps repo-relative path → source text. Returns the number
    of edges added.
    """
    from ..resolvers.dotnet.namespace_map import declared_namespaces_vb

    root_namespaces: dict[str, str] = {}
    if dotnet_index is not None and repo_path is not None:
        for path in vb_texts:
            proj = dotnet_index.project_for_file((repo_path / path).resolve())
            root_namespaces[path] = (proj.root_namespace if proj else None) or ""

    ns_types = build_namespace_type_index_vb(vb_texts, root_namespaces)

    count = 0
    for path in sorted(vb_texts):
        text = vb_texts[path]
        root_ns = root_namespaces.get(path, "")
        declared_raw = list(dict.fromkeys(declared_namespaces_vb(text)))
        if declared_raw:
            own_namespaces = [_effective_namespace(ns, root_ns).casefold() for ns in declared_raw]
        else:
            own_namespaces = [root_ns.casefold()] if root_ns else []
        explicit_ns = {m.group(1).casefold() for m in _IMPORTS_NS_RE.finditer(text)}
        alias_names = {m.group(1) for m in _IMPORTS_ALIAS_RE.finditer(text)}

        # Project-wide visible namespaces (VB's <Import Include=...>
        # ItemGroup entries, D7), restricted to namespaces that actually
        # exist locally.
        project_ns: list[str] = []
        if dotnet_index is not None and repo_path is not None:
            csproj = dotnet_index.file_to_project.get((repo_path / path).resolve())
            if csproj is not None:
                project_ns = sorted(
                    {
                        ns.casefold()
                        for ns in dotnet_index.globals_for_project(csproj)
                        if ns.casefold() in ns_types and ns.casefold() not in own_namespaces
                    }
                )

        if not own_namespaces and not project_ns:
            continue

        # Types declared in namespaces this file explicitly ``Imports``s —
        # those resolve through the normal import path and shadow the
        # project-wide tier.
        explicit_types: set[str] = set()
        for ns in explicit_ns:
            explicit_types.update(ns_types.get(ns, ()))

        # target file → (referenced names, hint source)
        found: dict[str, tuple[list[str], str]] = {}
        for ident in sorted(set(_TYPE_IDENT_RE.findall(text))):
            if ident in _BCL_COMMON_TYPES or ident in alias_names:
                continue
            ident_cf = ident.casefold()
            target: str | None = None
            hint = _SAME_NAMESPACE_HINT
            # Tier 1: the file's own effective namespace(s).
            declaring: set[str] = set()
            for ns in own_namespaces:
                declaring.update(ns_types.get(ns, {}).get(ident_cf, ()))
            if declaring:
                if len(declaring) != 1:
                    continue  # ambiguous — no edge to anyone
                target = next(iter(declaring))
            elif project_ns:
                if ident_cf in explicit_types:
                    continue  # explicit Imports already resolved it
                hint = _PROJECT_IMPORT_HINT
                declaring = set()
                for ns in project_ns:
                    declaring.update(ns_types.get(ns, {}).get(ident_cf, ()))
                if len(declaring) != 1:
                    continue
                target = next(iter(declaring))
            if target is None or target == path:
                continue
            names, _ = found.setdefault(target, ([], hint))
            names.append(ident)

        for target, (names, hint) in sorted(found.items()):
            if not graph.has_node(path) or not graph.has_node(target):
                continue
            if graph.has_edge(path, target):
                continue  # a real import (or stronger evidence) wins
            graph.add_edge(
                path,
                target,
                edge_type="imports",
                imported_names=names,
                hint_source=hint,
            )
            count += 1

    return count
