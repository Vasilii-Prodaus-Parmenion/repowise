"""Unit tests for VB.NET project awareness (vb-support.md Phase 3).

Mirrors ``test_csharp_resolver.py``'s shape: small on-disk repos under
``tmp_path`` so the index parses real MSBuild XML and .sln text. No .NET
SDK or Roslyn sidecar needed — these tests only exercise the Python-side
project index, namespace scanning, and import resolver.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from repowise.core.ingestion.resolvers.context import ResolverContext
from repowise.core.ingestion.resolvers.dotnet.index import build_index
from repowise.core.ingestion.resolvers.dotnet.msbuild import (
    find_project_files,
    parse_project,
)
from repowise.core.ingestion.resolvers.dotnet.namespace_map import (
    declared_namespaces_vb,
    scan_type_declarations_vb,
)
from repowise.core.ingestion.resolvers.dotnet.solution import parse_sln
from repowise.core.ingestion.resolvers.vbnet import resolve_vbnet_import
from repowise.core.ingestion.vb.rootns import (
    build_vb_project_namespaces,
    root_namespace_for_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vbproj(
    deps: list[str] = (),
    packages: list[tuple[str, str]] = (),
    root_namespace: str | None = None,
    imports: list[str] = (),
) -> str:
    refs = "\n".join(f'    <ProjectReference Include="{p}" />' for p in deps)
    pkgs = "\n".join(
        f'    <PackageReference Include="{name}" Version="{ver}" />' for name, ver in packages
    )
    imps = "\n".join(f'    <Import Include="{ns}" />' for ns in imports)
    ns_elem = f"    <RootNamespace>{root_namespace}</RootNamespace>\n" if root_namespace else ""
    return f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
{ns_elem}  </PropertyGroup>
  <ItemGroup>
{refs}
{pkgs}
{imps}
  </ItemGroup>
</Project>
"""


def _ctx_for(repo: Path) -> ResolverContext:
    """Build a ResolverContext rooted at *repo* with .cs and .vb files indexed."""
    source_files = list(repo.rglob("*.cs")) + list(repo.rglob("*.vb"))
    path_set: set[str] = set()
    stem_map: dict[str, list[str]] = {}
    for f in source_files:
        rel = f.resolve().relative_to(repo.resolve()).as_posix()
        path_set.add(rel)
        stem = f.stem.lower()
        stem_map.setdefault(stem, []).append(rel)
    return ResolverContext(
        path_set=path_set,
        stem_map=stem_map,
        graph=nx.DiGraph(),
        repo_path=repo,
    )


# ---------------------------------------------------------------------------
# .vbproj parsing
# ---------------------------------------------------------------------------


class TestVbprojParsing:
    def test_extracts_root_namespace_and_references(self, tmp_path: Path) -> None:
        vbproj_path = tmp_path / "Foo.vbproj"
        vbproj_path.write_text(
            _vbproj(
                deps=[r"..\Bar\Bar.vbproj"],
                packages=[("Newtonsoft.Json", "13.0.1")],
                root_namespace="Acme.Foo",
                imports=["System", "System.Linq"],
            )
        )
        proj = parse_project(vbproj_path)
        assert proj is not None
        assert proj.root_namespace == "Acme.Foo"
        assert proj.package_references == {"Newtonsoft.Json"}
        assert proj.project_usings == {"System", "System.Linq"}
        assert any(p.name == "Bar.vbproj" for p in proj.project_references)

    def test_find_project_files_returns_both_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "A.csproj").write_text(_vbproj())
        (tmp_path / "B.vbproj").write_text(_vbproj())
        found = {p.name for p in find_project_files(tmp_path)}
        assert found == {"A.csproj", "B.vbproj"}

    def test_parse_csproj_alias_still_works(self, tmp_path: Path) -> None:
        from repowise.core.ingestion.resolvers.dotnet.msbuild import parse_csproj

        vbproj_path = tmp_path / "Foo.vbproj"
        vbproj_path.write_text(_vbproj(root_namespace="Acme.Foo"))
        proj = parse_csproj(vbproj_path)  # back-compat alias, extension-agnostic
        assert proj is not None
        assert proj.root_namespace == "Acme.Foo"


# ---------------------------------------------------------------------------
# .sln parsing
# ---------------------------------------------------------------------------


class TestSlnParsingVb:
    def test_extracts_mixed_csproj_and_vbproj_entries(self, tmp_path: Path) -> None:
        (tmp_path / "Api").mkdir()
        (tmp_path / "Api" / "Api.csproj").write_text(_vbproj())
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj())

        sln = tmp_path / "Mixed.sln"
        sln.write_text(
            """Microsoft Visual Studio Solution File, Format Version 12.00
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Api", "Api\\Api.csproj", "{AAAAAAAA-1111-2222-3333-444444444444}"
EndProject
Project("{F184B08F-C81C-45F6-A57F-5ABD9991F28F}") = "Legacy", "Legacy\\Legacy.vbproj", "{BBBBBBBB-1111-2222-3333-444444444444}"
EndProject
"""
        )
        entries = parse_sln(sln)
        names = {e.name for e in entries}
        assert names == {"Api", "Legacy"}
        assert any(e.csproj.name == "Legacy.vbproj" for e in entries)


# ---------------------------------------------------------------------------
# VB namespace/type declaration scanning
# ---------------------------------------------------------------------------


class TestVbNamespaceScanning:
    def test_single_namespace_block(self) -> None:
        text = "Namespace Acme.App\nPublic Class Foo\nEnd Class\nEnd Namespace\n"
        assert declared_namespaces_vb(text) == ["Acme.App"]

    def test_case_insensitive_keyword(self) -> None:
        text = "namespace Acme.App\nPublic Class Foo\nEnd Class\nend namespace\n"
        assert declared_namespaces_vb(text) == ["Acme.App"]

    def test_no_namespace_block(self) -> None:
        assert declared_namespaces_vb("Public Class Foo\nEnd Class\n") == []

    def test_type_declarations_module_class_structure(self) -> None:
        text = (
            "Namespace Acme.App\n"
            "Public Class Foo\n"
            "End Class\n"
            "Friend Module Helpers\n"
            "End Module\n"
            "Public Structure Point\n"
            "End Structure\n"
            "End Namespace\n"
        )
        decls = scan_type_declarations_vb(text)
        names = {d.name for d in decls}
        assert names == {"Foo", "Helpers", "Point"}
        assert all(d.namespace == "Acme.App" for d in decls)

    def test_partial_modifier_detected(self) -> None:
        text = "Partial Public Class Foo\nEnd Class\n"
        decls = scan_type_declarations_vb(text)
        assert len(decls) == 1
        assert decls[0].is_partial is True

    def test_type_before_any_namespace_has_empty_namespace(self) -> None:
        text = "Public Class Loose\nEnd Class\n"
        decls = scan_type_declarations_vb(text)
        assert decls[0].namespace == ""


# ---------------------------------------------------------------------------
# DotNetProjectIndex widened to VB
# ---------------------------------------------------------------------------


class TestIndexVbAwareness:
    def test_vb_file_bucketed_to_owning_vbproj(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        (tmp_path / "Legacy" / "Foo.vb").write_text("Public Class Foo\nEnd Class\n")
        index = build_index(tmp_path)
        vb_file = (tmp_path / "Legacy" / "Foo.vb").resolve()
        proj = index.project_for_file(vb_file)
        assert proj is not None
        assert proj.name == "Legacy"
        assert proj.root_namespace == "Acme.Legacy"

    def test_namespace_map_has_root_namespace_prefixed_entry(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        (tmp_path / "Legacy" / "Foo.vb").write_text(
            "Namespace Services\nPublic Class Foo\nEnd Class\nEnd Namespace\n"
        )
        index = build_index(tmp_path)
        assert "Acme.Legacy.Services" in index.namespace_map

    def test_file_with_no_namespace_block_uses_bare_root_namespace(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        (tmp_path / "Legacy" / "Foo.vb").write_text("Public Class Foo\nEnd Class\n")
        index = build_index(tmp_path)
        assert "Acme.Legacy" in index.namespace_map

    def test_namespace_map_also_keyed_casefolded(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        (tmp_path / "Legacy" / "Foo.vb").write_text(
            "Namespace Services\nPublic Class Foo\nEnd Class\nEnd Namespace\n"
        )
        index = build_index(tmp_path)
        assert "acme.legacy.services" in index.namespace_map

    def test_vb_project_level_import_merged_into_project_globals(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(
            _vbproj(root_namespace="Acme.Legacy", imports=["System", "System.Linq"])
        )
        (tmp_path / "Legacy" / "Foo.vb").write_text("Public Class Foo\nEnd Class\n")
        index = build_index(tmp_path)
        proj = next(iter(index.projects.values()))
        assert {"System", "System.Linq"} <= index.globals_for_project(proj.path)


# ---------------------------------------------------------------------------
# End-to-end resolver
# ---------------------------------------------------------------------------


class TestVbnetResolverEndToEnd:
    def _make_solution(self, repo: Path) -> None:
        """A 2-project VB solution: Api references Domain."""
        (repo / "src" / "Api").mkdir(parents=True)
        (repo / "src" / "Domain").mkdir(parents=True)

        (repo / "src" / "Api" / "Api.vbproj").write_text(
            _vbproj(
                deps=[r"..\Domain\Domain.vbproj"],
                packages=[("Newtonsoft.Json", "13.0.1")],
                root_namespace="Acme.Api",
            )
        )
        (repo / "src" / "Domain" / "Domain.vbproj").write_text(
            _vbproj(root_namespace="Acme.Domain")
        )

        (repo / "src" / "Domain" / "User.vb").write_text("Public Class User\nEnd Class\n")
        (repo / "src" / "Api" / "UsersController.vb").write_text(
            "Imports Acme.Domain\nPublic Class UsersController\nEnd Class\n"
        )

    def test_resolves_cross_project_via_namespace(self, tmp_path: Path) -> None:
        self._make_solution(tmp_path)
        ctx = _ctx_for(tmp_path)
        importer = "src/Api/UsersController.vb"
        result = resolve_vbnet_import("Acme.Domain", importer, ctx)
        assert result == "src/Domain/User.vb"

    def test_resolves_case_insensitive_import(self, tmp_path: Path) -> None:
        self._make_solution(tmp_path)
        ctx = _ctx_for(tmp_path)
        importer = "src/Api/UsersController.vb"
        # VB matches namespaces case-insensitively (D8) — a differently
        # cased Imports statement must still resolve.
        result = resolve_vbnet_import("acme.domain", importer, ctx)
        assert result == "src/Domain/User.vb"

    def test_external_nuget_package(self, tmp_path: Path) -> None:
        self._make_solution(tmp_path)
        ctx = _ctx_for(tmp_path)
        importer = "src/Api/UsersController.vb"
        result = resolve_vbnet_import("Newtonsoft.Json", importer, ctx)
        assert result is not None and result.startswith("external:nuget:")

    def test_unknown_namespace_falls_to_external(self, tmp_path: Path) -> None:
        self._make_solution(tmp_path)
        ctx = _ctx_for(tmp_path)
        result = resolve_vbnet_import("Totally.Unknown.Thing", "src/Api/UsersController.vb", ctx)
        assert result is not None and result.startswith("external:")

    def test_no_repo_path_falls_back_to_stem_match(self, tmp_path: Path) -> None:
        (tmp_path / "loose.vb").write_text("Public Class Loose\nEnd Class\n")
        path_set = {"loose.vb"}
        stem_map = {"loose": ["loose.vb"]}
        ctx = ResolverContext(
            path_set=path_set, stem_map=stem_map, graph=nx.DiGraph(), repo_path=None
        )
        assert resolve_vbnet_import("Loose", "other.vb", ctx) == "loose.vb"

    def test_crosses_csharp_vb_project_reference_boundary(self, tmp_path: Path) -> None:
        """A migrating codebase: a VB file imports a namespace declared in a
        referenced C# project — ProjectReference must cross the language
        boundary (vb-support.md §7)."""
        (tmp_path / "src" / "NewApi").mkdir(parents=True)
        (tmp_path / "src" / "OldDomain").mkdir(parents=True)

        (tmp_path / "src" / "NewApi" / "NewApi.vbproj").write_text(
            _vbproj(deps=[r"..\OldDomain\OldDomain.csproj"], root_namespace="Acme.NewApi")
        )
        (tmp_path / "src" / "OldDomain" / "OldDomain.csproj").write_text(_vbproj())

        (tmp_path / "src" / "OldDomain" / "User.cs").write_text(
            "namespace Acme.Domain;\npublic class User {}\n"
        )
        (tmp_path / "src" / "NewApi" / "Controller.vb").write_text(
            "Imports Acme.Domain\nPublic Class Controller\nEnd Class\n"
        )

        ctx = _ctx_for(tmp_path)
        result = resolve_vbnet_import("Acme.Domain", "src/NewApi/Controller.vb", ctx)
        assert result == "src/OldDomain/User.cs"


# ---------------------------------------------------------------------------
# Per-file RootNamespace lookup (sidecar parse-request threading)
# ---------------------------------------------------------------------------


class TestRootNamespaceLookup:
    def test_maps_project_dir_to_root_namespace(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        project_dirs = build_vb_project_namespaces(tmp_path)
        assert project_dirs[(tmp_path / "Legacy").resolve()] == "Acme.Legacy"

    def test_root_namespace_for_file_longest_prefix(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        (tmp_path / "Legacy" / "Sub").mkdir()
        project_dirs = build_vb_project_namespaces(tmp_path)
        file_abs = (tmp_path / "Legacy" / "Sub" / "Foo.vb").resolve()
        assert root_namespace_for_file(project_dirs, file_abs) == "Acme.Legacy"

    def test_no_matching_project_returns_empty_string(self, tmp_path: Path) -> None:
        (tmp_path / "Legacy").mkdir()
        (tmp_path / "Legacy" / "Legacy.vbproj").write_text(_vbproj(root_namespace="Acme.Legacy"))
        project_dirs = build_vb_project_namespaces(tmp_path)
        other = (tmp_path / "Elsewhere" / "Foo.vb").resolve()
        assert root_namespace_for_file(project_dirs, other) == ""

    def test_no_declared_root_namespace_yields_empty_string(self, tmp_path: Path) -> None:
        (tmp_path / "Bare").mkdir()
        (tmp_path / "Bare" / "Bare.vbproj").write_text(_vbproj())
        project_dirs = build_vb_project_namespaces(tmp_path)
        assert project_dirs[(tmp_path / "Bare").resolve()] == ""

    def test_file_outside_any_project_returns_empty_string(self, tmp_path: Path) -> None:
        project_dirs: dict[Path, str] = {}
        assert root_namespace_for_file(project_dirs, tmp_path / "Loose.vb") == ""

    def test_two_projects_different_root_namespaces(self, tmp_path: Path) -> None:
        (tmp_path / "A").mkdir()
        (tmp_path / "A" / "A.vbproj").write_text(_vbproj(root_namespace="Acme.A"))
        (tmp_path / "B").mkdir()
        (tmp_path / "B" / "B.vbproj").write_text(_vbproj(root_namespace="Acme.B"))
        project_dirs = build_vb_project_namespaces(tmp_path)
        assert (
            root_namespace_for_file(project_dirs, (tmp_path / "A" / "Foo.vb").resolve()) == "Acme.A"
        )
        assert (
            root_namespace_for_file(project_dirs, (tmp_path / "B" / "Bar.vb").resolve()) == "Acme.B"
        )
