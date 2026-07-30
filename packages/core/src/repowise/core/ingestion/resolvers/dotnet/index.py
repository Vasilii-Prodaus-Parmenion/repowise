"""Repo-scoped .NET project index — built once per resolver run, cached on the context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from repowise.core.fs_walk import iter_glob

from .global_usings import collect_project_global_usings
from .msbuild import (
    MSBuildProject,
    find_project_files,
    parse_project,
    path_has_dotnet_scan_skip_dir,
)
from .namespace_map import build_namespace_map, declared_namespaces_vb
from .solution import find_sln_files, parse_sln

if TYPE_CHECKING:
    from ..context import ResolverContext

log = structlog.get_logger(__name__)


@dataclass
class DotNetProjectIndex:
    """Cached view of every .NET project in a single repo."""

    repo_path: Path
    projects: dict[Path, MSBuildProject] = field(default_factory=dict)
    """Keyed by absolute .csproj path."""

    namespace_map: dict[str, list[Path]] = field(default_factory=dict)
    """Maps a fully-qualified namespace to the set of .cs/.vb files declaring
    it. VB entries are RootNamespace-prefixed and additionally keyed by their
    casefolded form (VB matches namespaces case-insensitively) — see
    ``build_index``."""

    type_map: dict[str, list[Path]] = field(default_factory=dict)
    """Maps an unqualified type name (e.g. ``IBasketService``) to defining files.

    A type name can appear in multiple files (partial types, distinct types
    with the same simple name in different namespaces). Callers rank the
    candidates by project enclosure — see ``rank_type_candidates`` below."""

    partial_types: dict[str, list[Path]] = field(default_factory=dict)
    """Maps a fully-qualified ``partial`` type name → files carrying a
    fragment. Co-fragments of one FQN are literally one class — the
    graph links them bidirectionally."""

    project_globals: dict[Path, set[str]] = field(default_factory=dict)
    """Maps a project's directory → global+implicit using namespaces."""

    file_to_project: dict[Path, Path] = field(default_factory=dict)
    """Maps a .cs/.vb file's absolute path → enclosing project's
    .csproj/.vbproj path."""

    project_refs_by_proj: dict[Path, set[Path]] = field(default_factory=dict)
    """Maps a .csproj path → set of referenced .csproj paths (transitive=False)."""

    package_refs: dict[Path, set[str]] = field(default_factory=dict)
    """Maps a .csproj path → declared NuGet package ids."""

    sln_paths: list[Path] = field(default_factory=list)

    # Cache for per-from_file project resolution. Stores
    # ``input Path → (resolved Path, enclosing csproj or None)``.
    # ``rank_type_candidates`` is called once per ``TypeReference`` —
    # dozens per file across thousands of files in a real C# monorepo —
    # and each call previously paid a fresh ``Path.resolve()`` (a stat
    # per component on Windows). Memoising collapses that to one
    # resolve per unique source file across the entire indexing run.
    _from_proj_cache: dict[Path, tuple[Path, Path | None]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------------
    # Lookups
    # ------------------------------------------------------------------

    def project_for_file(self, file_abs: Path) -> MSBuildProject | None:
        """Return the project enclosing *file_abs*, or None."""
        csproj = self.file_to_project.get(file_abs.resolve())
        return self.projects.get(csproj) if csproj else None

    def _resolve_from_file(self, from_file: Path) -> tuple[Path, Path | None]:
        """Memoised resolve + project lookup for repeated callers."""
        cached = self._from_proj_cache.get(from_file)
        if cached is not None:
            return cached
        try:
            resolved = from_file.resolve()
        except OSError:
            resolved = from_file
        entry = (resolved, self.file_to_project.get(resolved))
        self._from_proj_cache[from_file] = entry
        return entry

    def referenced_projects(self, csproj: Path) -> set[Path]:
        """Return the direct ProjectReference set for *csproj*."""
        return self.project_refs_by_proj.get(csproj, set())

    def files_for_namespace(self, ns: str) -> list[Path]:
        return self.namespace_map.get(ns, [])

    def files_for_type(self, type_name: str) -> list[Path]:
        return self.type_map.get(type_name, [])

    def rank_type_candidates(
        self,
        type_name: str,
        from_file: Path,
    ) -> list[Path]:
        """Return defining files for *type_name*, ranked by project enclosure.

        Ranking matches ``resolve_csharp_import`` for namespace lookups:
            1. Same project as *from_file*
            2. Projects referenced by from_file's project (transitive=1)
            3. Anywhere else in the workspace

        Same-named partial-type fragments collapse: each unique file
        appears once. ``from_file`` is excluded so a class that only
        names its own types doesn't get a self-edge.
        """
        candidates = self.type_map.get(type_name)
        if not candidates:
            return []

        from_resolved, from_proj = self._resolve_from_file(from_file)
        ref_projs = self.project_refs_by_proj.get(from_proj, set()) if from_proj else set()

        same_proj: list[Path] = []
        ref_proj: list[Path] = []
        repo_wide: list[Path] = []
        seen: set[Path] = set()
        for cand in candidates:
            if cand in seen or cand == from_resolved:
                continue
            seen.add(cand)
            cand_proj = self.file_to_project.get(cand)
            if cand_proj is not None and cand_proj == from_proj:
                same_proj.append(cand)
            elif cand_proj is not None and cand_proj in ref_projs:
                ref_proj.append(cand)
            else:
                repo_wide.append(cand)
        return same_proj + ref_proj + repo_wide

    def globals_for_project(self, csproj: Path) -> set[str]:
        proj = self.projects.get(csproj)
        if not proj:
            return set()
        return self.project_globals.get(proj.project_dir, set())

    def package_for(self, csproj: Path, package_id: str) -> bool:
        return package_id in self.package_refs.get(csproj, set())


def _walk_repo_ext_files(repo_path: Path, ext: str, *, prune_nested_git: bool = True) -> list[Path]:
    """Single repo-wide rglob for files matching *ext* (e.g. ``".cs"``).

    Dedup by resolved path. Lives at module scope (not nested inside
    ``build_index``) so it's independently testable and so the skip-list is
    shared with the XAML extractor's walk above. Parameterised on extension
    (vb-support.md §7) so the same walk + skip-list serves both the C# and
    VB source trees without duplicating this logic.
    """
    seen: set[Path] = set()
    out: list[Path] = []
    for f in iter_glob(repo_path, f"*{ext}", prune_nested_git=prune_nested_git):
        if path_has_dotnet_scan_skip_dir(f, repo_path):
            continue
        try:
            resolved = f.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _bucket_files_by_project(
    cs_files: list[Path],
    project_dirs: list[tuple[Path, Path]],
) -> dict[Path, Path]:
    """Map each resolved source file (.cs or .vb) → enclosing project path via longest-prefix.

    Walks each file's parent chain ONCE against a precomputed
    ``{project_dir → csproj}`` dict, so the cost is O(N x depth)
    rather than the previous O(N x M_projects x depth). Because we
    walk parents from deepest to shallowest, the first hit is by
    construction the most specific project — no separate
    descending-depth sort of projects required.

    *project_dirs* is retained as a sequence (deterministic order)
    only to seed the dict; the lookup itself is dict-only.
    """
    dir_to_proj: dict[Path, Path] = {}
    for proj_dir, csproj in project_dirs:
        # If two projects somehow share a directory, the first wins
        # (callers pass a stable-ordered iterable). Nested layouts are
        # disambiguated by the parent walk below, not by this dict.
        dir_to_proj.setdefault(proj_dir, csproj)

    out: dict[Path, Path] = {}
    for f in cs_files:
        # Walk parents from immediate dir outward; first match wins,
        # which is always the most deeply-nested enclosing project.
        for parent in f.parents:
            csproj = dir_to_proj.get(parent)
            if csproj is not None:
                out[f] = csproj
                break
    return out


def build_index(repo_path: Path, *, prune_nested_git: bool = True) -> DotNetProjectIndex:
    """Walk *repo_path* and construct a fully-populated DotNetProjectIndex.

    Performance note: a previous version of this function walked the
    ``*.cs`` tree three times — once for ``_gather_project_files``,
    once inside ``build_namespace_map`` (per-file ``read_text``), and a
    third time inside ``collect_project_global_usings`` (rglob +
    read_text per project, with heavy overlap on shared parent dirs).
    On NTFS with Defender that pattern dominates indexing time
    (40+ minutes on PowerToys-scale repos). The current shape does
    ONE master walk, reads each file ONCE, and dispatches the cached
    texts to both the namespace-map pass and per-project global-usings
    collection. No data-quality loss — same regexes, same outputs.

    ``.vb`` files join the same master walk and file-to-project bucketing
    (vb-support.md §7) so a VB file's enclosing project — and therefore its
    RootNamespace and referenced projects — resolves the same way a .cs
    file's does. They do NOT go through ``build_namespace_map`` (that
    function's regexes are C#-syntax-specific); instead their declared
    namespaces are scanned separately below with the VB scanner and merged
    into ``namespace_map``, RootNamespace-prefixed and additionally keyed by
    casefolded form since VB matches namespaces case-insensitively (D8).
    """
    repo_path = repo_path.resolve()
    index = DotNetProjectIndex(repo_path=repo_path)

    # ---- 1. Parse every .csproj/.vbproj ----
    for project_path in find_project_files(repo_path, prune_nested_git=prune_nested_git):
        proj = parse_project(project_path)
        if proj is None:
            continue
        index.projects[proj.path] = proj
        index.project_refs_by_proj[proj.path] = set(proj.project_references)
        index.package_refs[proj.path] = set(proj.package_references)

    # ---- 2. Walk .sln files (informational; surfaces orphaned .csprojs) ----
    index.sln_paths = find_sln_files(repo_path, prune_nested_git=prune_nested_git)
    for sln in index.sln_paths:
        for entry in parse_sln(sln):
            if entry.csproj not in index.projects:
                # Solution references a project we didn't pick up — try parsing it.
                proj = parse_project(entry.csproj)
                if proj is not None:
                    index.projects[proj.path] = proj
                    index.project_refs_by_proj.setdefault(proj.path, set()).update(
                        proj.project_references
                    )
                    index.package_refs.setdefault(proj.path, set()).update(proj.package_references)

    # ---- 3. Single master walk: enumerate .cs/.vb files & read each once ----
    cs_files = _walk_repo_ext_files(repo_path, ".cs", prune_nested_git=prune_nested_git)
    vb_files = _walk_repo_ext_files(repo_path, ".vb", prune_nested_git=prune_nested_git)
    all_source_files = cs_files + vb_files
    source_texts: dict[Path, str] = {}
    for f in all_source_files:
        try:
            source_texts[f] = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue

    # Build the file → project map by longest-prefix match. The bucketer
    # walks each file's parent chain (deepest-first) against a dict of
    # project dirs, so nested projects bind naturally without a pre-sort.
    project_dirs: list[tuple[Path, Path]] = [
        (proj.project_dir.resolve(), proj.path) for proj in index.projects.values()
    ]
    index.file_to_project = _bucket_files_by_project(all_source_files, project_dirs)

    # ---- 4. Namespace + type + partial maps from cached texts (C# only) ----
    index.namespace_map, index.type_map, index.partial_types = build_namespace_map(
        cs_files, texts=source_texts
    )

    # ---- 4b. VB namespaces, RootNamespace-prefixed, merged into the same map ----
    for f in vb_files:
        text = source_texts.get(f)
        if text is None:
            continue
        vb_csproj = index.file_to_project.get(f)
        proj = index.projects.get(vb_csproj) if vb_csproj else None
        root_ns = (proj.root_namespace if proj else None) or ""
        declared = declared_namespaces_vb(text)
        if not declared:
            # No Namespace block — RootNamespace itself is the effective
            # namespace for every type in the file (empty if the project
            # declares none either, in which case there is nothing to index).
            effective_namespaces = {root_ns} if root_ns else set()
        else:
            effective_namespaces = (
                {f"{root_ns}.{ns}" for ns in declared} if root_ns else set(declared)
            )
        for ns in effective_namespaces:
            if not ns:
                continue
            index.namespace_map.setdefault(ns, []).append(f)
            ns_casefolded = ns.casefold()
            if ns_casefolded != ns:
                index.namespace_map.setdefault(ns_casefolded, []).append(f)

    # ---- 5. Per-project global+implicit usings from cached texts ----
    # Bucket the cached texts by project once so each project's call to
    # ``collect_project_global_usings`` is O(files in that project). VB
    # projects have no "global using" directives to scan (harmless no-op
    # over .vb text) — their project-wide imports come entirely from
    # ``proj.project_usings`` (<Import Include=...>, parsed in msbuild.py),
    # merged below same as C#'s <Using Include=...>.
    texts_by_proj: dict[Path, dict[Path, str]] = {}
    for f, csproj in index.file_to_project.items():
        text = source_texts.get(f)
        if text is None:
            continue
        texts_by_proj.setdefault(csproj, {})[f] = text

    for proj in index.projects.values():
        # Heuristic: presence of any AspNetCore PackageReference flags the
        # web SDK's expanded implicit-using set.
        sdk_is_web = any(pkg.startswith("Microsoft.AspNetCore") for pkg in proj.package_references)
        globals_set = collect_project_global_usings(
            proj.project_dir,
            proj.implicit_usings,
            sdk_is_web=sdk_is_web,
            project_texts=texts_by_proj.get(proj.path, {}),
        )
        # Honour <Using Include="X"/> (C#) / <Import Include="X"/> (VB)
        # ItemGroup entries on top of file scans.
        globals_set.update(proj.project_usings)
        index.project_globals[proj.project_dir] = globals_set

    log.info(
        "DotNetProjectIndex built",
        repo=str(repo_path),
        projects=len(index.projects),
        cs_files=len(cs_files),
        vb_files=len(vb_files),
        namespaces=len(index.namespace_map),
        types=len(index.type_map),
        sln=len(index.sln_paths),
    )
    return index


# Stash key on the ResolverContext (reuses a generic cache slot to avoid
# adding a typed field for every language plugin).
_INDEX_KEY = "_dotnet_index"


def get_or_build_index(ctx: ResolverContext) -> DotNetProjectIndex | None:
    """Return the cached DotNetProjectIndex, building it on first access."""
    if not ctx.repo_path:
        return None
    cached = getattr(ctx, _INDEX_KEY, None)
    if cached is not None:
        return cached
    index = build_index(ctx.repo_path, prune_nested_git=ctx.prune_nested_git)
    setattr(ctx, _INDEX_KEY, index)
    return index
