"""Parse MSBuild project files (.csproj, .vbproj, Directory.Build.props/targets).

Only the fields repowise actually uses are extracted: ProjectReference,
PackageReference, RootNamespace, AssemblyName, ImplicitUsings, and
project-level <Using Include=...> (C#) / <Import Include=...> (VB) items.
The parser tolerates both SDK-style and legacy XML
(``<Project ToolsVersion="...">``).

``.vbproj`` support (vb-support.md D7, §7): VB has no ``global using``
syntax, so its project-wide-visible namespaces are declared entirely as
``<Import Include="System.Linq" />`` ItemGroup entries rather than scanned
from source. Those share the ``project_usings`` field with C#'s
``<Using Include=...>`` items — same consumer (``global_usings.py``),
different MSBuild item name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import structlog

from repowise.core.fs_walk import iter_glob

log = structlog.get_logger(__name__)


# Directory basenames the .NET / Unity resolver should never scan for
# projects or source. These are intentionally scoped to the dotnet path,
# not shared global fs_walk pruning, because names like Library/ or Logs/
# can be legitimate source trees in non-Unity repos.
DOTNET_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "bin",
        "obj",
        ".vs",
        "node_modules",
        ".git",
        "packages",
        "TestResults",
        "Library",
        "Temp",
        "Logs",
        "UserSettings",
        "MemoryCaptures",
        "Builds",
    }
)
_DOTNET_SCAN_SKIP_DIRS_CASEFOLDED = frozenset(part.casefold() for part in DOTNET_SCAN_SKIP_DIRS)

# MSBuild project file extensions this module understands. MSBuildProject is
# extension-agnostic in shape (D7) — a .vbproj parses into the same
# dataclass a .csproj does.
PROJECT_EXTENSIONS: frozenset[str] = frozenset({".csproj", ".vbproj"})


@dataclass
class MSBuildProject:
    """Parsed MSBuild project file."""

    path: Path  # absolute path to the .csproj or .vbproj
    project_dir: Path  # directory containing the .csproj
    root_namespace: str | None = None
    assembly_name: str | None = None
    implicit_usings: bool = False
    project_references: list[Path] = field(
        default_factory=list
    )  # absolute paths to referenced .csproj
    package_references: set[str] = field(default_factory=set)  # NuGet package ids
    project_usings: set[str] = field(default_factory=set)  # <Using Include="X"/> namespaces

    @property
    def name(self) -> str:
        """Display name — the .csproj filename without extension."""
        return self.path.stem


# Strip XML namespace prefix from a tag — MSBuild docs say the namespace
# is optional in SDK-style projects but legacy projects use
# ``http://schemas.microsoft.com/developer/msbuild/2003``.
def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in ("true", "enable", "1")


def parse_project(project_path: Path) -> MSBuildProject | None:
    """Parse a single .csproj/.vbproj file. Returns None on parse failure."""
    try:
        tree = ET.parse(project_path)
    except (ET.ParseError, OSError) as exc:
        log.debug("Failed to parse project file", path=str(project_path), error=str(exc))
        return None

    project = MSBuildProject(path=project_path.resolve(), project_dir=project_path.parent.resolve())
    root = tree.getroot()

    for elem in root.iter():
        tag = _local(elem.tag)

        if tag == "RootNamespace" and elem.text:
            project.root_namespace = elem.text.strip()
        elif tag == "AssemblyName" and elem.text:
            project.assembly_name = elem.text.strip()
        elif tag == "ImplicitUsings" and elem.text:
            project.implicit_usings = _bool(elem.text)
        elif tag == "ProjectReference":
            include = elem.get("Include")
            if include:
                # ProjectReference paths use Windows-style backslashes by
                # convention; normalise and resolve relative to the .csproj.
                rel = include.replace("\\", "/")
                target = (project.project_dir / rel).resolve()
                project.project_references.append(target)
        elif tag == "PackageReference":
            pkg = elem.get("Include")
            if pkg:
                project.package_references.add(pkg.strip())
        elif tag == "Using":
            ns = elem.get("Include")
            if ns:
                project.project_usings.add(ns.strip())
        elif tag == "Import":
            # VB's ItemGroup-level project-wide import ("Imports" without a
            # per-file directive, D7): <Import Include="System.Linq" />.
            # Distinguished from the unrelated top-level
            # <Import Project="Foo.props" /> (which pulls in another
            # MSBuild file) by attribute name — that one never sets
            # Include.
            ns = elem.get("Include")
            if ns:
                project.project_usings.add(ns.strip())

    return project


# Back-compat alias — every prior caller (index.py, tests) imports this name.
parse_csproj = parse_project


def find_project_files(repo_path: Path, *, prune_nested_git: bool = True) -> list[Path]:
    """Return all .csproj/.vbproj files under *repo_path*, skipping bin/obj output."""
    out: list[Path] = []
    for ext in sorted(PROJECT_EXTENSIONS):
        for proj in iter_glob(repo_path, f"*{ext}", prune_nested_git=prune_nested_git):
            if path_has_dotnet_scan_skip_dir(proj, repo_path):
                continue
            out.append(proj)
    return out


# Back-compat alias — every prior caller (index.py, tests) imports this name.
find_csproj_files = find_project_files


def path_has_dotnet_scan_skip_dir(path: Path, repo_root: Path) -> bool:
    """Return True when *path* lives under a dotnet-skip directory in *repo_root*."""
    try:
        parts = path.relative_to(repo_root).parts
    except ValueError:
        parts = path.parts
    return any(part.casefold() in _DOTNET_SCAN_SKIP_DIRS_CASEFOLDED for part in parts)
