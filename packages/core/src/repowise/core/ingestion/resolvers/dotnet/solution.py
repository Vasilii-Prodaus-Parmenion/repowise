"""Parse Visual Studio .sln solution files.

The .sln format is line-oriented and not XML. Each project is declared as::

    Project("{<type-guid>}") = "Name", "relative\\path\\Name.csproj", "{<proj-guid>}"

Solution folders use a different type GUID and are skipped — repowise only
cares about real project entries that point to a .csproj/.vbproj on disk
(``PROJECT_EXTENSIONS``, vb-support.md §7) — a mixed C#/VB solution declares
both in the same .sln, and ProjectReference edges must cross that boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import structlog

from repowise.core.fs_walk import iter_glob

from .msbuild import PROJECT_EXTENSIONS, path_has_dotnet_scan_skip_dir

log = structlog.get_logger(__name__)

# Project("{TYPE}") = "Name", "rel\path.csproj", "{GUID}"
_PROJECT_RE = re.compile(
    r'^Project\("\{([^}]+)\}"\)\s*=\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"\{([^}]+)\}"',
    re.MULTILINE,
)

# Solution-folder type GUID — these have no .csproj and must be skipped.
_FOLDER_TYPE_GUID = "2150E333-8FDC-42A3-9474-1A3956D46DE8"


@dataclass(frozen=True)
class SolutionEntry:
    name: str
    csproj: Path  # absolute; .csproj or .vbproj despite the field name
    project_guid: str


def parse_sln(sln_path: Path) -> list[SolutionEntry]:
    """Return one SolutionEntry per .csproj/.vbproj declared in *sln_path*."""
    try:
        text = sln_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        log.debug("Failed to read sln", path=str(sln_path), error=str(exc))
        return []

    sln_dir = sln_path.parent
    out: list[SolutionEntry] = []
    for match in _PROJECT_RE.finditer(text):
        type_guid, name, rel_path, proj_guid = match.groups()
        if type_guid.upper() == _FOLDER_TYPE_GUID:
            continue
        if not any(rel_path.lower().endswith(ext) for ext in PROJECT_EXTENSIONS):
            continue
        csproj = (sln_dir / rel_path.replace("\\", "/")).resolve()
        out.append(SolutionEntry(name=name, csproj=csproj, project_guid=proj_guid))
    return out


def find_sln_files(repo_path: Path, *, prune_nested_git: bool = True) -> list[Path]:
    out: list[Path] = []
    for sln in iter_glob(repo_path, "*.sln", prune_nested_git=prune_nested_git):
        if path_has_dotnet_scan_skip_dir(sln, repo_path):
            continue
        out.append(sln)
    return out
