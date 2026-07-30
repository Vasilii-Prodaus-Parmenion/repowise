"""First-use build of the Roslyn VB sidecar, cached per repo (D2).

The sidecar's C# source ships inside the wheel; it is built into
``.repowise/roslyn-sidecar/<key>/`` the first time a repo needs it, where
``<key>`` folds in the repowise version, the wire protocol version, and a
hash of the sidecar sources — so an upgrade (of repowise, or of the
protocol) invalidates the cache automatically instead of silently running a
stale build.

The build lock mirrors the liveness-probe pattern in
``core/update_lock.py`` (atomic hard-link create, PID + creation-token
liveness check, wall-clock staleness ceiling) rather than importing it
directly — that module's payload shape (``target_commit``) is specific to
the update lock, and a build lock has nothing to do with a commit.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import structlog

from repowise.core import __version__ as repowise_version
from repowise.core.repo_config import get_repowise_dir

log = structlog.get_logger(__name__)

#: Wire protocol version. Bumping this invalidates every repo's sidecar
#: cache, same as a repowise upgrade would — kept here (not imported from
#: sidecar.py) so build.py has no runtime dependency on the client.
PROTOCOL_VERSION = 1

_SIDECAR_SRC_DIR = Path(__file__).parent / "sidecar_src"
_ENV_OVERRIDE = "REPOWISE_ROSLYN_SIDECAR"
_DLL_NAME = "RepowiseVb.dll"
_LOCK_SUFFIX = ".build.lock"

# Generous: first-use NuGet restore of Microsoft.CodeAnalysis.VisualBasic
# plus `dotnet build` on a cold cache, observed around 20-40s.
_BUILD_TIMEOUT_SECONDS = 300
# How long a caller will wait for a *different* process's concurrent build
# before giving up — not the build's own timeout.
_BUILD_WAIT_TIMEOUT_SECONDS = 300
_BUILD_LOCK_STALE_AFTER_SECONDS = 20 * 60


class VbSidecarBuildError(RuntimeError):
    """Raised when the sidecar could not be built (or the override is bad)."""


#: MSBuild writes its intermediate/output directories directly into the
#: project directory it builds (``bin/``, ``obj/``), which is exactly
#: ``_SIDECAR_SRC_DIR`` here. A local `dotnet build` (dev machines, or a
#: prior run of this same function) litters those in-tree, and they contain
#: build-specific absolute paths (e.g. ``obj/**/*.FileListAbsolute.txt``
#: records the *previous* build's output directory). If the fingerprint
#: walk includes them, every build changes the fingerprint, which changes
#: the cache key (D2), which points at a *new* output directory next time —
#: guaranteeing the cache never converges and every invocation rebuilds
#: from scratch instead of the intended "once per repowise version".
_BUILD_OUTPUT_DIRS = frozenset({"bin", "obj"})


def sidecar_src_fingerprint() -> str:
    """Hash of every source file under ``sidecar_src/`` — the sidecar's own source.

    Used both by :func:`sidecar_cache_key` (folded with repowise_version, for
    the on-disk build cache key) and, separately without repowise_version, by
    ``parse_cache.parser_fingerprint`` — a released-but-VB-unrelated repowise
    version must not invalidate a user's whole parse cache. Deliberately
    excludes ``bin/``/``obj/`` (see :data:`_BUILD_OUTPUT_DIRS`): those are
    build output, not source, and are never present in the shipped wheel.
    """
    h = hashlib.sha256()
    for path in sorted(_SIDECAR_SRC_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(_SIDECAR_SRC_DIR)
        if rel.parts and rel.parts[0] in _BUILD_OUTPUT_DIRS:
            continue
        h.update(rel.as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def sidecar_cache_key() -> str:
    """Cache key: repowise version + protocol version + sidecar source hash."""
    h = hashlib.sha256()
    h.update(f"repowise:{repowise_version}".encode())
    h.update(f"protocol:{PROTOCOL_VERSION}".encode())
    h.update(f"src:{sidecar_src_fingerprint()}".encode())
    return h.hexdigest()[:16]


def _roslyn_sidecar_root(repo_path: Path) -> Path:
    return get_repowise_dir(repo_path) / "roslyn-sidecar"


def sidecar_cache_dir(repo_path: Path) -> Path:
    return _roslyn_sidecar_root(repo_path) / sidecar_cache_key()


def _lock_path(repo_path: Path) -> Path:
    return _roslyn_sidecar_root(repo_path) / f"{sidecar_cache_key()}{_LOCK_SUFFIX}"


def _read_lock(lock_path: Path) -> dict | None:
    """Return the lock payload if present, live, and not stale; else None."""
    from repowise.core.procutils import pid_alive, process_create_token

    if not lock_path.exists():
        return None
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    started = payload.get("started_at")
    if not isinstance(started, (int, float)):
        return None
    if time.time() - started > _BUILD_LOCK_STALE_AFTER_SECONDS:
        return None

    pid = payload.get("pid")
    if isinstance(pid, int) and pid > 0:
        alive = pid_alive(pid)
        if alive is False:
            return None
        if alive is True:
            stored_token = payload.get("pid_create_token")
            if isinstance(stored_token, str) and stored_token:
                current_token = process_create_token(pid)
                if current_token is not None and current_token != stored_token:
                    return None
    return payload


def _try_acquire_lock(lock_path: Path) -> bool:
    """Atomically acquire the build lock. Clears a stale lock and retries once."""
    from repowise.core.procutils import process_create_token

    payload = {
        "pid": os.getpid(),
        "pid_create_token": process_create_token(os.getpid()),
        "started_at": time.time(),
    }
    data = json.dumps(payload)
    tmp_path = lock_path.with_name(f"{lock_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    for _ in range(2):
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(data, encoding="utf-8")
            os.link(tmp_path, lock_path)
        except FileExistsError:
            if _read_lock(lock_path) is not None:
                return False
            with contextlib.suppress(OSError):
                lock_path.unlink(missing_ok=True)
            continue
        except OSError:
            return True  # advisory lock: never block a build over a fs error
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
        return True
    return _read_lock(lock_path) is None


def _release_lock(lock_path: Path) -> None:
    with contextlib.suppress(OSError):
        lock_path.unlink(missing_ok=True)


def _build(cache_dir: Path) -> None:
    from repowise.core.ingestion.vb.preflight import find_dotnet_executable

    dotnet_exe = find_dotnet_executable()
    if dotnet_exe is None:
        raise VbSidecarBuildError(
            "`dotnet` was not found while building the VB sidecar. "
            "Install the .NET SDK: https://dotnet.microsoft.com/download"
        )

    tmp_dir = cache_dir.parent / f".{cache_dir.name}.build-tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    log.info("vb_sidecar.build.start", cache_dir=str(cache_dir))
    try:
        result = subprocess.run(
            [
                dotnet_exe,
                "build",
                str(_SIDECAR_SRC_DIR / "RepowiseVb.csproj"),
                "-c",
                "Release",
                "--nologo",
                "-o",
                str(tmp_dir),
            ],
            capture_output=True,
            text=True,
            timeout=_BUILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise VbSidecarBuildError(
            f"`dotnet build` timed out after {_BUILD_TIMEOUT_SECONDS}s building the VB sidecar."
        ) from exc

    if result.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise VbSidecarBuildError(
            "Failed to build the VB sidecar (`dotnet build`). The most likely cause "
            "is no network access on first use — building requires NuGet to fetch "
            f"Microsoft.CodeAnalysis.VisualBasic once.\n\n{result.stdout}\n{result.stderr}"
        )

    cache_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp_dir.rename(cache_dir)
    except OSError:
        # Lost a rename race to an equivalent concurrent build (same cache
        # key): that build is as good as this one.
        if not cache_dir.exists():
            raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    log.info("vb_sidecar.build.done", cache_dir=str(cache_dir))


def ensure_sidecar_built(repo_path: Path) -> Path:
    """Return a directory containing a built ``RepowiseVb.dll``.

    Builds on first use per repo (D2); concurrent repowise processes on the
    same repo build once, the rest wait for the lock. ``REPOWISE_ROSLYN_SIDECAR``
    skips the build entirely and uses a prebuilt sidecar (air-gapped installs,
    CI images that pre-seed it, local sidecar development).
    """
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        override_dir = Path(override)
        if not (override_dir / _DLL_NAME).exists():
            raise VbSidecarBuildError(f"{_ENV_OVERRIDE}={override} does not contain {_DLL_NAME}")
        return override_dir

    cache_dir = sidecar_cache_dir(repo_path)
    if (cache_dir / _DLL_NAME).exists():
        return cache_dir

    lock_path = _lock_path(repo_path)
    acquired = False
    try:
        deadline = time.monotonic() + _BUILD_WAIT_TIMEOUT_SECONDS
        while True:
            acquired = _try_acquire_lock(lock_path)
            if acquired:
                break
            if (cache_dir / _DLL_NAME).exists():
                return cache_dir
            if time.monotonic() > deadline:
                raise VbSidecarBuildError(
                    "Timed out waiting for a concurrent repowise process to finish "
                    "building the VB sidecar."
                )
            time.sleep(1)

        # Re-check: another process may have finished between our first
        # check and acquiring the lock.
        if (cache_dir / _DLL_NAME).exists():
            return cache_dir

        _build(cache_dir)
        return cache_dir
    finally:
        if acquired:
            _release_lock(lock_path)
