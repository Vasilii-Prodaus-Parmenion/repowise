"""VB sidecar build cache (D2): cache key, the REPOWISE_ROSLYN_SIDECAR escape
hatch, and the build lock's liveness-probe semantics (mirrors
tests/unit/cli/test_update_lock.py — see core/update_lock.py precedent).

No SDK, no real ``dotnet build`` — ``_build`` is monkeypatched out wherever a
real build would otherwise run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repowise.core.ingestion.vb import build
from repowise.core.procutils import process_create_token


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return proc.pid


# ---------------------------------------------------------------------------
# cache key / cache dir
# ---------------------------------------------------------------------------


def test_sidecar_cache_key_is_deterministic() -> None:
    assert build.sidecar_cache_key() == build.sidecar_cache_key()


def test_sidecar_cache_dir_lives_under_repowise_dir(tmp_path: Path) -> None:
    cache_dir = build.sidecar_cache_dir(tmp_path)

    assert cache_dir.parent.parent == tmp_path / ".repowise"
    assert cache_dir.name == build.sidecar_cache_key()


# ---------------------------------------------------------------------------
# REPOWISE_ROSLYN_SIDECAR escape hatch
# ---------------------------------------------------------------------------


def test_ensure_sidecar_built_uses_override_when_dll_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_dir = tmp_path / "prebuilt"
    override_dir.mkdir()
    (override_dir / build._DLL_NAME).write_text("", encoding="utf-8")
    monkeypatch.setenv("REPOWISE_ROSLYN_SIDECAR", str(override_dir))

    def _fail_if_called(_cache_dir: Path) -> None:
        raise AssertionError("must not build when the override is set")

    monkeypatch.setattr(build, "_build", _fail_if_called)

    result = build.ensure_sidecar_built(tmp_path / "repo")

    assert result == override_dir


def test_ensure_sidecar_built_override_missing_dll_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override_dir = tmp_path / "prebuilt-empty"
    override_dir.mkdir()
    monkeypatch.setenv("REPOWISE_ROSLYN_SIDECAR", str(override_dir))

    with pytest.raises(build.VbSidecarBuildError):
        build.ensure_sidecar_built(tmp_path / "repo")


# ---------------------------------------------------------------------------
# cache hit skips the build entirely
# ---------------------------------------------------------------------------


def test_ensure_sidecar_built_reuses_existing_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOWISE_ROSLYN_SIDECAR", raising=False)
    repo = tmp_path / "repo"
    cache_dir = build.sidecar_cache_dir(repo)
    cache_dir.mkdir(parents=True)
    (cache_dir / build._DLL_NAME).write_text("", encoding="utf-8")

    def _fail_if_called(_cache_dir: Path) -> None:
        raise AssertionError("must not rebuild when the DLL already exists")

    monkeypatch.setattr(build, "_build", _fail_if_called)

    result = build.ensure_sidecar_built(repo)

    assert result == cache_dir


def test_ensure_sidecar_built_invokes_build_on_cold_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOWISE_ROSLYN_SIDECAR", raising=False)
    repo = tmp_path / "repo"
    calls: list[Path] = []

    def _fake_build(cache_dir: Path) -> None:
        calls.append(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / build._DLL_NAME).write_text("", encoding="utf-8")

    monkeypatch.setattr(build, "_build", _fake_build)

    result = build.ensure_sidecar_built(repo)

    assert calls == [build.sidecar_cache_dir(repo)]
    assert (result / build._DLL_NAME).exists()


# ---------------------------------------------------------------------------
# build lock: liveness probe (mirrors test_update_lock.py)
# ---------------------------------------------------------------------------


def test_lock_acquire_records_pid_and_token(tmp_path: Path) -> None:
    lock_path = tmp_path / "sidecar.build.lock"

    assert build._try_acquire_lock(lock_path) is True
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert payload["pid"] == os.getpid()
    assert payload["pid_create_token"] == process_create_token(os.getpid())


def test_lock_second_acquire_blocked_while_live(tmp_path: Path) -> None:
    lock_path = tmp_path / "sidecar.build.lock"

    assert build._try_acquire_lock(lock_path) is True
    assert build._try_acquire_lock(lock_path) is False


def test_lock_release_then_reacquire(tmp_path: Path) -> None:
    lock_path = tmp_path / "sidecar.build.lock"

    assert build._try_acquire_lock(lock_path) is True
    build._release_lock(lock_path)
    assert build._try_acquire_lock(lock_path) is True


def test_lock_with_dead_pid_is_stale_and_reclaimed(tmp_path: Path) -> None:
    lock_path = tmp_path / "sidecar.build.lock"
    lock_path.write_text(
        json.dumps({"pid": _dead_pid(), "started_at": time.time()}), encoding="utf-8"
    )

    assert build._read_lock(lock_path) is None
    assert build._try_acquire_lock(lock_path) is True


def test_lock_past_wall_clock_ceiling_is_stale(tmp_path: Path) -> None:
    lock_path = tmp_path / "sidecar.build.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "pid_create_token": process_create_token(os.getpid()),
                "started_at": time.time() - build._BUILD_LOCK_STALE_AFTER_SECONDS - 60,
            }
        ),
        encoding="utf-8",
    )

    assert build._read_lock(lock_path) is None
