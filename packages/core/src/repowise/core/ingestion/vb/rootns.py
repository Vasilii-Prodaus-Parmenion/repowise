"""Per-file RootNamespace lookup for VB sidecar parse requests.

VB's ``RootNamespace`` is prepended to every declared namespace in a
project (vb-support.md D7, §5.5) — the sidecar needs it per file to emit
correct qualified names, and a solution can contain several VB projects
with different root namespaces (§4.2), so this is a project-directory ->
namespace lookup, not a single repo-wide string.

Deliberately independent of ``resolvers/dotnet/index.py``'s
``DotNetProjectIndex``: that index is built once per ``GraphBuilder.build()``
and cached on the ``ResolverContext``, which does not exist yet during the
parse phase. Building the full index this early would mean computing it
twice per run for no benefit — this only needs project directories and
their ``RootNamespace``, which is a handful of small XML parses.
"""

from __future__ import annotations

from pathlib import Path

from repowise.core.ingestion.resolvers.dotnet.msbuild import find_project_files, parse_project


def build_vb_project_namespaces(
    repo_path: Path, *, prune_nested_git: bool = True
) -> dict[Path, str]:
    """Return ``{.vbproj project directory: RootNamespace}`` for *repo_path*.

    Projects with no declared ``RootNamespace`` map to ``""`` — the sidecar
    treats that as "no prefix".
    """
    result: dict[Path, str] = {}
    for proj_path in find_project_files(repo_path, prune_nested_git=prune_nested_git):
        if proj_path.suffix.lower() != ".vbproj":
            continue
        proj = parse_project(proj_path)
        if proj is not None:
            result[proj.project_dir] = proj.root_namespace or ""
    return result


def root_namespace_for_file(project_dirs: dict[Path, str], file_abs: Path) -> str:
    """Return the RootNamespace of the project enclosing *file_abs*, or ``""``."""
    for parent in file_abs.parents:
        if parent in project_dirs:
            return project_dirs[parent]
    return ""
