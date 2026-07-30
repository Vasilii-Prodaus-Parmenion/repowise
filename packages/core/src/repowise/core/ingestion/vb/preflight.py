"""Dotnet SDK discovery and the preflight abort for VB.NET support (D3).

Nothing about VB indexing should half-run: if a repo has ``.vb`` files and no
adequate SDK, the caller must find out before the parse phase, not partway
through it. This module only *discovers and decides*; wiring the abort into
``run_pipeline`` lands with the parse-phase partition (see vb-support.md
work breakdown, phase 2), since there is nothing to gate before VB is a
registered, traversed language.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: Minimum dotnet SDK major version the sidecar requires (it targets net8.0).
MIN_SDK_MAJOR = 8

_INSTALL_URL = "https://dotnet.microsoft.com/download"
_EXCLUDE_HINT = "repowise init -x '**/*.vb'"
_SDK_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

# Directories skipped by the cheap `.vb`-presence scan used by `doctor` and
# by callers that have not yet run a full traversal. Not the traversal's own
# ignore rules (pathspec/.repowiseIgnore) — just enough to keep this fast and
# to avoid reporting build output as evidence of a VB codebase.
_SCAN_SKIP_DIRS = frozenset({".git", ".repowise", "node_modules", "bin", "obj"})


@dataclass
class DotnetSdkCheck:
    """Result of probing the machine for a usable dotnet SDK."""

    dotnet_exe: str | None
    sdk_versions: list[str] = field(default_factory=list)

    @property
    def meets_minimum(self) -> bool:
        return any(_major_version(v) >= MIN_SDK_MAJOR for v in self.sdk_versions)


class DotnetSdkMissingError(RuntimeError):
    """Raised when a repo has ``.vb`` files but no adequate dotnet SDK exists.

    Carries what was looked for, what was found, how many ``.vb`` files
    triggered the check, and the two ways out (install, or exclude VB).
    """

    def __init__(self, *, vb_file_count: int, check: DotnetSdkCheck) -> None:
        self.vb_file_count = vb_file_count
        self.check = check
        super().__init__(_format_message(vb_file_count, check))


def _major_version(version: str) -> int:
    m = _SDK_VERSION_RE.match(version)
    return int(m.group(1)) if m else -1


def _windows_default_dotnet() -> Path | None:
    """Probe ``%ProgramFiles%\\dotnet\\dotnet.exe`` — the SDK can be installed
    and simply not on ``PATH``."""
    if os.name != "nt":
        return None
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    candidate = Path(program_files) / "dotnet" / "dotnet.exe"
    return candidate if candidate.exists() else None


def find_dotnet_executable() -> str | None:
    """Locate the ``dotnet`` executable.

    Checks ``DOTNET_ROOT`` first (the SDK's own override), then ``PATH``,
    then — on Windows only — the default per-machine install location.
    """
    dotnet_root = os.environ.get("DOTNET_ROOT")
    if dotnet_root:
        exe_name = "dotnet.exe" if os.name == "nt" else "dotnet"
        candidate = Path(dotnet_root) / exe_name
        if candidate.exists():
            return str(candidate)

    on_path = shutil.which("dotnet")
    if on_path:
        return on_path

    default_win = _windows_default_dotnet()
    if default_win is not None:
        return str(default_win)

    return None


def check_dotnet_sdk() -> DotnetSdkCheck:
    """Discover the installed dotnet SDK(s), if any. Never raises."""
    dotnet_exe = find_dotnet_executable()
    if dotnet_exe is None:
        return DotnetSdkCheck(dotnet_exe=None, sdk_versions=[])

    try:
        result = subprocess.run(
            [dotnet_exe, "--list-sdks"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("vb_preflight.list_sdks_failed", error=str(exc))
        return DotnetSdkCheck(dotnet_exe=dotnet_exe, sdk_versions=[])

    versions = [
        line.split(" ", 1)[0]
        for line in result.stdout.splitlines()
        if _SDK_VERSION_RE.match(line.strip())
    ]
    return DotnetSdkCheck(dotnet_exe=dotnet_exe, sdk_versions=versions)


def _format_message(vb_file_count: int, check: DotnetSdkCheck) -> str:
    if check.sdk_versions:
        found = f"found {', '.join(check.sdk_versions)} (via {check.dotnet_exe})"
    elif check.dotnet_exe:
        found = f"`dotnet` found at {check.dotnet_exe}, but it reports no installed SDKs"
    else:
        found = "`dotnet` was not found on DOTNET_ROOT, PATH, or the default install location"
    return (
        f"This repo has {vb_file_count} .vb file(s), which require the .NET SDK "
        f">= {MIN_SDK_MAJOR}.0 to parse (Roslyn sidecar) — {found}.\n"
        f"Install the .NET SDK: {_INSTALL_URL}\n"
        f"Or skip VB for this run: {_EXCLUDE_HINT}"
    )


def ensure_vb_prerequisites(vb_file_count: int) -> None:
    """Raise :class:`DotnetSdkMissingError` if VB files exist with no adequate SDK.

    A repo with zero ``.vb`` files never pays for the ``dotnet`` probe.
    """
    if vb_file_count <= 0:
        return
    check = check_dotnet_sdk()
    if not check.meets_minimum:
        raise DotnetSdkMissingError(vb_file_count=vb_file_count, check=check)


def repo_has_vb_files(repo_path: Path) -> bool:
    """Cheap presence scan for at least one ``.vb`` file.

    Used by ``doctor`` (and anywhere else that needs an answer before a full
    traversal has run) to decide whether the dotnet SDK row is worth
    showing at all — the advisory is only interesting when the repo
    actually contains VB.
    """
    for _root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in _SCAN_SKIP_DIRS]
        if any(f.endswith(".vb") for f in files):
            return True
    return False
