"""VB.NET import resolution (vb-support.md D7, §7).

Deliberately thin: the ``DotNetProjectIndex`` (project graph, namespace map,
project-level usings) is built once per repo and shared with C# — the
generalisation of ``msbuild.py``/``solution.py``/``index.py`` to
``.vbproj`` is what makes that index already contain every VB project and
VB-declared namespace by the time this module runs. This mirrors
``resolvers/csharp.py`` almost exactly; the two things that differ are:

- Namespace lookup tries an exact-case match first, then a casefolded one
  (VB matches identifiers — including namespaces — case-insensitively,
  D8), so an ``Imports`` statement whose casing drifts from the file that
  declared the namespace still resolves.
- The legacy stem-match fallback (for repos with loose ``.vb`` files and
  no ``.vbproj``) matches ``.vb`` files, not ``.cs``.

Resolution algorithm (in priority order), identical to C#'s:

1. Build (and cache) the shared ``DotNetProjectIndex`` for the repo.
2. Locate the project enclosing the importer file. If unknown, fall back
   to the legacy stem-match resolver.
3. Look up the ``Imports`` namespace in the namespace map:
       a. Prefer files inside the same project.
       b. Then files inside any directly-referenced project
          (ProjectReference — this is what lets a ``ProjectReference``
          cross the C#/VB boundary in a migrating codebase).
       c. Otherwise pick the first match anywhere in the repo.
4. If no match is found and the namespace prefix matches a declared
   ``<PackageReference>``, register an external NuGet node.
5. Final fallback: register a generic external node.
"""

from __future__ import annotations

from pathlib import Path

from .context import ResolverContext
from .csharp import (
    _matches_package_prefix,
    _repo_root_resolved,
    _resolve_importer,
    _to_repo_relative,
)
from .dotnet import get_or_build_index


def _legacy_stem_resolve_vb(module_path: str, ctx: ResolverContext) -> str | None:
    """Stem-match fallback for VB repos with no ``.vbproj`` on disk."""
    parts = module_path.split(".")
    local = parts[-1]
    result = ctx.stem_lookup(local.lower())
    if result and result.endswith(".vb"):
        return result
    if len(parts) > 1:
        dir_suffix = "/".join(parts)
        for p in ctx.sorted_paths:
            if p.endswith(".vb") and dir_suffix.lower() in p.lower():
                return p
    return None


def resolve_vbnet_import(module_path: str, importer_path: str, ctx: ResolverContext) -> str | None:
    """Resolve a VB.NET ``Imports`` statement to a repo-relative file path or external key."""
    index = get_or_build_index(ctx)
    if index is None or not ctx.repo_path:
        legacy = _legacy_stem_resolve_vb(module_path, ctx)
        return legacy if legacy else ctx.add_external_node(module_path)

    importer_abs = _resolve_importer(index, ctx.repo_path, importer_path)
    importer_csproj = index.file_to_project.get(importer_abs)
    importer_proj = index.projects.get(importer_csproj) if importer_csproj else None

    # Exact case first (covers a VB file importing a C#-declared namespace,
    # or a VB import whose casing matches the declaring file); casefolded
    # fallback covers VB's own case-insensitive namespace matching (D8).
    candidates = index.files_for_namespace(module_path)
    if not candidates:
        candidates = index.files_for_namespace(module_path.casefold())
    repo_root_resolved = _repo_root_resolved(index, ctx.repo_path)

    if candidates:
        # Rank: same project, then referenced projects, then anywhere.
        same_project: list[Path] = []
        referenced: list[Path] = []
        other: list[Path] = []

        if importer_proj is not None:
            ref_csprojs = index.referenced_projects(importer_proj.path)
            for cand in candidates:
                cand_proj_path = index.file_to_project.get(cand)
                if cand_proj_path == importer_proj.path:
                    same_project.append(cand)
                elif cand_proj_path in ref_csprojs:
                    referenced.append(cand)
                else:
                    other.append(cand)
            ordered = same_project or referenced or other
        else:
            ordered = candidates

        chosen = ordered[0]
        rel = _to_repo_relative(chosen, repo_root_resolved)
        if rel and rel in ctx.path_set:
            return rel

    # No file declares this namespace — could be a BCL/NuGet namespace or a
    # sibling project's public API surface. If a package reference matches,
    # mark external NuGet.
    if importer_proj is not None:
        pkgs = index.package_refs.get(importer_proj.path, set())
        if _matches_package_prefix(module_path, pkgs):
            return ctx.add_external_node(f"nuget:{module_path}")

    # Last resort: legacy stem-match (catches repos with no .vbproj).
    legacy = _legacy_stem_resolve_vb(module_path, ctx)
    if legacy:
        return legacy

    return ctx.add_external_node(module_path)
