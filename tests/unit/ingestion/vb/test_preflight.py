"""VB preflight (D3): dotnet SDK discovery and the abort-before-parse gate.

No SDK, no subprocess dependency — every ``dotnet`` invocation is faked via
monkeypatch, per docs/architecture/vb-support.md §10.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from repowise.core.ingestion.vb import preflight

# pathlib in modern Python refuses to instantiate the "wrong" flavour for the
# real OS (WindowsPath on Linux, PosixPath on Windows) even if `os.name` is
# monkeypatched — so these tests exercise the current real platform rather
# than faking a foreign one. The Windows-default fallback is real-OS-gated
# below since it is a Windows-only code path (D3, §5.4).
_DOTNET_EXE_NAME = "dotnet.exe" if os.name == "nt" else "dotnet"


# ---------------------------------------------------------------------------
# find_dotnet_executable
# ---------------------------------------------------------------------------


def test_find_dotnet_executable_prefers_dotnet_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / _DOTNET_EXE_NAME
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "should-not-be-used")

    assert preflight.find_dotnet_executable() == str(exe)


def test_find_dotnet_executable_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOTNET_ROOT", raising=False)
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: "/usr/bin/dotnet")

    assert preflight.find_dotnet_executable() == "/usr/bin/dotnet"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only fallback (D3, §5.4)")
def test_find_dotnet_executable_falls_back_to_windows_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine can have the SDK installed and simply not on PATH."""
    program_files = tmp_path / "Program Files"
    dotnet_dir = program_files / "dotnet"
    dotnet_dir.mkdir(parents=True)
    (dotnet_dir / "dotnet.exe").write_text("", encoding="utf-8")

    monkeypatch.delenv("DOTNET_ROOT", raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(program_files))
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)

    assert preflight.find_dotnet_executable() == str(dotnet_dir / "dotnet.exe")


def test_find_dotnet_executable_none_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOTNET_ROOT", raising=False)
    # Point the Windows-default probe at an empty directory so a real dotnet
    # install on the test machine's actual Program Files doesn't leak in.
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "no-program-files-here"))
    monkeypatch.setattr(preflight.shutil, "which", lambda _name: None)

    assert preflight.find_dotnet_executable() is None


# ---------------------------------------------------------------------------
# check_dotnet_sdk
# ---------------------------------------------------------------------------


def test_check_dotnet_sdk_missing_dotnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "find_dotnet_executable", lambda: None)

    check = preflight.check_dotnet_sdk()

    assert check.dotnet_exe is None
    assert check.sdk_versions == []
    assert not check.meets_minimum


def test_check_dotnet_sdk_parses_list_sdks_and_meets_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight, "find_dotnet_executable", lambda: "/usr/bin/dotnet")
    fake_stdout = (
        "6.0.428 [/usr/share/dotnet/sdk]\n"
        "8.0.423 [/usr/share/dotnet/sdk]\n"
        "\n"  # trailing blank line, must not blow up parsing
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=fake_stdout, stderr=""),
    )

    check = preflight.check_dotnet_sdk()

    assert check.dotnet_exe == "/usr/bin/dotnet"
    assert check.sdk_versions == ["6.0.428", "8.0.423"]
    assert check.meets_minimum


def test_check_dotnet_sdk_too_old_does_not_meet_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "find_dotnet_executable", lambda: "/usr/bin/dotnet")
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="6.0.428 [/usr/share/dotnet/sdk]\n", stderr=""
        ),
    )

    check = preflight.check_dotnet_sdk()

    assert check.sdk_versions == ["6.0.428"]
    assert not check.meets_minimum


def test_check_dotnet_sdk_probe_timeout_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "find_dotnet_executable", lambda: "/usr/bin/dotnet")

    def _raise(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="dotnet", timeout=15)

    monkeypatch.setattr(preflight.subprocess, "run", _raise)

    check = preflight.check_dotnet_sdk()

    assert check.dotnet_exe == "/usr/bin/dotnet"
    assert check.sdk_versions == []
    assert not check.meets_minimum


# ---------------------------------------------------------------------------
# ensure_vb_prerequisites / DotnetSdkMissingError
# ---------------------------------------------------------------------------


def test_ensure_vb_prerequisites_noop_when_no_vb_files(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail_if_called() -> preflight.DotnetSdkCheck:
        raise AssertionError("must not probe dotnet when vb_file_count is 0")

    monkeypatch.setattr(preflight, "check_dotnet_sdk", _fail_if_called)

    preflight.ensure_vb_prerequisites(0)  # must not raise, must not probe


def test_ensure_vb_prerequisites_raises_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "check_dotnet_sdk",
        lambda: preflight.DotnetSdkCheck(dotnet_exe=None, sdk_versions=[]),
    )

    with pytest.raises(preflight.DotnetSdkMissingError) as exc_info:
        preflight.ensure_vb_prerequisites(3)

    err = exc_info.value
    assert err.vb_file_count == 3
    assert "3 .vb file(s)" in str(err)
    assert "https://dotnet.microsoft.com/download" in str(err)
    assert "-x '**/*.vb'" in str(err)


def test_ensure_vb_prerequisites_passes_when_sdk_adequate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight,
        "check_dotnet_sdk",
        lambda: preflight.DotnetSdkCheck(dotnet_exe="/usr/bin/dotnet", sdk_versions=["8.0.423"]),
    )

    preflight.ensure_vb_prerequisites(3)  # must not raise


def test_dotnet_sdk_missing_error_mentions_found_versions_when_too_old() -> None:
    check = preflight.DotnetSdkCheck(dotnet_exe="/usr/bin/dotnet", sdk_versions=["6.0.428"])

    err = preflight.DotnetSdkMissingError(vb_file_count=1, check=check)

    assert "6.0.428" in str(err)
    assert "/usr/bin/dotnet" in str(err)


# ---------------------------------------------------------------------------
# repo_has_vb_files
# ---------------------------------------------------------------------------


def test_repo_has_vb_files_true(tmp_path: Path) -> None:
    (tmp_path / "Module1.vb").write_text("Module Module1\nEnd Module\n", encoding="utf-8")

    assert preflight.repo_has_vb_files(tmp_path) is True


def test_repo_has_vb_files_false_for_empty_repo(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("hi", encoding="utf-8")

    assert preflight.repo_has_vb_files(tmp_path) is False


def test_repo_has_vb_files_ignores_build_output_dirs(tmp_path: Path) -> None:
    """A .vb file only under bin/obj must not count as evidence of a VB repo."""
    generated = tmp_path / "obj" / "Debug"
    generated.mkdir(parents=True)
    (generated / "Generated.vb").write_text("", encoding="utf-8")

    assert preflight.repo_has_vb_files(tmp_path) is False


def test_repo_has_vb_files_finds_nested_source(tmp_path: Path) -> None:
    nested = tmp_path / "src" / "Forms"
    nested.mkdir(parents=True)
    (nested / "MainForm.vb").write_text("", encoding="utf-8")

    assert preflight.repo_has_vb_files(tmp_path) is True
