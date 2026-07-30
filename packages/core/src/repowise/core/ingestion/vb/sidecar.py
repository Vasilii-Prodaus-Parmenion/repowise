"""``VbSidecarClient`` — the process boundary to the Roslyn VB sidecar.

Run-scoped per repo (D6): one dotnet sidecar process per ``run_pipeline()``,
spawned lazily on first use and torn down in a ``finally``. ``get_sidecar``
is a per-repo singleton so the bulk parse phase, ``incremental.py``, and
``reparse.py`` all join whatever sidecar the current run already started
(see vb-support.md §5.2) instead of each starting their own.

Protocol: newline-delimited JSON over stdin/stdout, request/response
correlated by ``id``. stderr is drained continuously and logged at debug —
it is diagnostics only, never transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

import structlog

from repowise.core.ingestion.vb.build import PROTOCOL_VERSION, ensure_sidecar_built
from repowise.core.ingestion.vb.preflight import find_dotnet_executable

log = structlog.get_logger(__name__)

_DLL_NAME = "RepowiseVb.dll"
_SHUTDOWN_GRACE_SECONDS = 5


class VbSidecarError(RuntimeError):
    """Raised when the sidecar fails to start, handshake, or respond."""


class VbSidecarClient:
    """Owns one dotnet sidecar subprocess for the lifetime of a pipeline run."""

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = Path(repo_path)
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = itertools.count(1)
        self._lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        # Bounded so a chatty or crash-looping sidecar can't leak memory;
        # only used to give a crash's "closed stdout unexpectedly" error
        # something more actionable than a bare message (vb-support.md's
        # own diagnostics bar — the .NET SDK "install this" message set
        # the precedent of naming the actual cause, not just the symptom).
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self.info: dict[str, Any] | None = None
        # Raw per-file "parse" result DTOs (symbols/eventWiring/complexity/...),
        # keyed by absolute path, for the two phases that run after parsing
        # and need facts the ParsedFile shape doesn't carry: dynamic_hints
        # (vb/handles.py, D8) and code health (vb/complexity.py, D5). Both
        # ride this singleton rather than a pipeline-result field, per
        # vb-support.md §8. Only cache MISSES populate it (cache hits reuse
        # a previously-parsed ParsedFile and never call the sidecar this
        # run) — a file present in the graph but absent here degrades to
        # silence in both consumers, matching the documented contract.
        self.file_results: dict[str, dict[str, Any]] = {}

    async def ensure_started(self) -> None:
        async with self._lock:
            if self._proc is None:
                await self._spawn_locked()

    async def request(
        self, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """Send one request, restarting the sidecar once on crash/timeout.

        A second failure after the restart propagates — consistent with D3's
        "either analysed properly or not at all", rather than silently
        returning an empty result.
        """
        await self.ensure_started()
        try:
            return await self._send(payload, timeout=timeout)
        except (VbSidecarError, TimeoutError, OSError) as exc:
            log.warning(
                "vb_sidecar.request_failed_restarting", op=payload.get("op"), error=str(exc)
            )
            await self._restart()
            return await self._send(payload, timeout=timeout)

    async def shutdown(self) -> None:
        async with self._lock:
            await self._teardown_locked()

    # -- internals ------------------------------------------------------

    async def _restart(self) -> None:
        async with self._lock:
            await self._teardown_locked()
            await self._spawn_locked()

    async def _spawn_locked(self) -> None:
        sidecar_dir = await asyncio.to_thread(ensure_sidecar_built, self._repo_path)
        dotnet_exe = find_dotnet_executable()
        if dotnet_exe is None:
            raise VbSidecarError(
                "`dotnet` was not found; cannot start the VB sidecar. "
                "Install the .NET SDK: https://dotnet.microsoft.com/download"
            )
        dll_path = sidecar_dir / _DLL_NAME

        self._proc = await asyncio.create_subprocess_exec(
            dotnet_exe,
            str(dll_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=256 * 1024 * 1024,  # 256 MB buffer for large parse responses
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        try:
            resp = await self._send({"op": "hello", "protocol": PROTOCOL_VERSION})
        except Exception:
            await self._teardown_locked()
            raise
        if not resp.get("ok") or resp.get("protocol") != PROTOCOL_VERSION:
            await self._teardown_locked()
            raise VbSidecarError(
                f"VB sidecar handshake failed or protocol mismatch (got {resp!r}); "
                f"delete {sidecar_dir} and retry."
            )
        self.info = resp
        log.debug("vb_sidecar.started", **{k: v for k, v in resp.items() if k not in ("id", "ok")})

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                self._stderr_tail.append(text)
                log.debug("vb_sidecar.stderr", line=text)

    async def _send(
        self, payload: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            raise VbSidecarError("VB sidecar is not running")
        stdin, stdout = proc.stdin, proc.stdout

        request_id = payload.setdefault("id", next(self._next_id))
        line = json.dumps(payload) + "\n"

        # Extract file list for error reporting
        files_in_request = []
        if payload.get("op") == "parse" and "files" in payload:
            files_in_request = [f.get("path", "<unknown>") for f in payload["files"][:5]]
            if len(payload["files"]) > 5:
                files_in_request.append(f"... and {len(payload['files']) - 5} more")

        async def _roundtrip() -> dict[str, Any]:
            stdin.write(line.encode("utf-8"))
            await stdin.drain()
            resp_line = await stdout.readline()
            if not resp_line:
                # A silent exit with no stderr and no non-zero code is the
                # signature of an external kill rather than a .NET crash (a
                # real unhandled exception in Program.cs's own try/except
                # would have printed to stderr first) — in practice this has
                # been Windows Defender's "block executables that don't meet
                # a prevalence/age/trust bar" ASR rule auditing or killing
                # the freshly-built, unsigned RepowiseVb.exe. Named here so
                # the error is actionable instead of a bare protocol symptom.
                raise VbSidecarError(
                    "VB sidecar closed stdout unexpectedly "
                    f"(exit code {proc.returncode!r}); recent stderr: "
                    f"{list(self._stderr_tail) or '<none captured>'}. "
                    f"Files being parsed: {', '.join(files_in_request) or 'none'}. "
                    "If stderr is empty and there's no obvious .NET crash, this "
                    "is often antivirus/EDR (e.g. a Windows Defender "
                    "Attack-Surface-Reduction rule) blocking or killing the "
                    "locally-built, unsigned RepowiseVb.exe — check Windows "
                    "Defender's 'Microsoft-Windows-Windows Defender/Operational' "
                    "event log for RepowiseVb.exe around this time, ask your "
                    "IT admin for an exclusion on the .repowise/roslyn-sidecar/ "
                    "directory, or use REPOWISE_ROSLYN_SIDECAR to point at a "
                    "prebuilt, pre-trusted sidecar."
                )

            try:
                resp: dict[str, Any] = json.loads(resp_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                resp_preview = resp_line[:500] if len(resp_line) > 500 else resp_line
                raise VbSidecarError(
                    f"Failed to parse VB sidecar response (file(s): {', '.join(files_in_request) or 'unknown'}). "
                    f"Response size: {len(resp_line)} bytes. "
                    f"Error: {exc}. "
                    f"Response preview: {resp_preview!r}"
                ) from exc

            if resp.get("id") != request_id:
                raise VbSidecarError(
                    f"VB sidecar response id mismatch: expected {request_id}, got {resp.get('id')}"
                )
            return resp

        if timeout is None:
            return await _roundtrip()
        try:
            return await asyncio.wait_for(_roundtrip(), timeout=timeout)
        except TimeoutError as exc:
            raise VbSidecarError(f"VB sidecar request timed out after {timeout}s") from exc

    async def _teardown_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return

        if proc.returncode is None and proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.write(
                    (json.dumps({"id": next(self._next_id), "op": "shutdown"}) + "\n").encode()
                )
                await proc.stdin.drain()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_GRACE_SECONDS)

        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await self._stderr_task
            self._stderr_task = None


async def parse_batch(
    client: VbSidecarClient,
    files: list[Any],
    *,
    root_namespace: str = "",
    root_namespaces: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Send one ``"parse"`` request for a batch of vbnet ``FileInfo``s.

    Returns ``{file_info.path: result_dto}``. Timeout scales with batch size
    (§4.3: ``10s + 0.5s/file``); a crash/timeout is retried once by
    :meth:`VbSidecarClient.request` itself, a second failure propagates
    (D3 — either analysed properly or not at all).

    ``rootNamespace`` is per-file because a solution can contain several VB
    projects with different root namespaces (§4.2). *root_namespaces* (keyed
    by ``file_info.path``) covers that; *root_namespace* is a single value
    applied to every file in the batch, for callers that already know every
    file in this call shares one project (e.g. :class:`VbSyncBridge`, which
    parses one file at a time).
    """
    if not files:
        return {}

    file_paths = [fi.path for fi in files]
    log.debug("vb_sidecar.parse_batch_start", file_count=len(files), files=file_paths)

    payload = {
        "op": "parse",
        "language": "vbnet",
        "files": [
            {
                "path": fi.path,
                "absPath": fi.abs_path,
                "rootNamespace": (root_namespaces or {}).get(fi.path, root_namespace),
            }
            for fi in files
        ],
    }
    timeout = 10.0 + 0.5 * len(files)
    try:
        resp = await client.request(payload, timeout=timeout)
    except VbSidecarError as exc:
        log.error("vb_sidecar.parse_batch_failed", file_count=len(files), files=file_paths, error=str(exc))
        raise

    if not resp.get("ok"):
        log.error("vb_sidecar.parse_response_not_ok", file_count=len(files), files=file_paths, response=resp)
        raise VbSidecarError(f"VB sidecar parse request failed: {resp!r}")

    results = {r["path"]: r for r in resp.get("results", [])}
    abs_path_by_path = {fi.path: fi.abs_path for fi in files}
    for path, result in results.items():
        abs_path = abs_path_by_path.get(path)
        if abs_path:
            client.file_results[abs_path] = result

    log.debug("vb_sidecar.parse_batch_complete", file_count=len(files), result_count=len(results))
    return results


class VbSyncBridge:
    """Synchronous per-call bridge to a standalone VB sidecar.

    For callers that parse files one at a time inside a synchronous loop
    (``build_repo_graph``, ``reparse_repo``), which may or may not already be
    running inside an asyncio event loop when they do it (``reparse_repo``
    runs off ``asyncio.to_thread`` with no loop of its own; ``build_repo_graph``
    is called synchronously from the already-running loop inside
    ``rebuild_graph_and_git``). ``asyncio.run()`` cannot nest, so rather than
    branch on the caller's context, this always drives its own dedicated
    background thread with its own event loop — safe either way, and correct
    for a whole loop of files since the loop and its sidecar client persist
    across every :meth:`parse_one` call until :meth:`close`.

    Deliberately NOT the run-scoped :func:`get_sidecar` singleton: that
    client's subprocess is bound to whichever loop started it, and a second,
    concurrently-running loop could never safely drive it. Accepted per D6's
    own risk note that a fresh sidecar startup (~1-2s) on a small update is a
    low-severity cost, not a correctness problem — this class pays that cost
    once per call to ``build_repo_graph``/``reparse_repo``, not once per file.
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = Path(repo_path)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._client = VbSidecarClient(self._repo_path)
        # Lazy, computed once and cached for the lifetime of this bridge
        # (one call to build_repo_graph/reparse_repo) — repeated per-file
        # .vbproj re-scans would otherwise dominate a loop over many files.
        self._vb_project_namespaces: dict[Path, str] | None = None

    def _root_namespace_for(self, file_info: Any) -> str:
        from repowise.core.ingestion.vb.rootns import (
            build_vb_project_namespaces,
            root_namespace_for_file,
        )

        if self._vb_project_namespaces is None:
            self._vb_project_namespaces = build_vb_project_namespaces(self._repo_path)
        return root_namespace_for_file(self._vb_project_namespaces, Path(file_info.abs_path))

    def parse_one(self, file_info: Any, source: bytes) -> Any:
        from repowise.core.ingestion.vb.parse import (
            empty_parsed_file,
            sidecar_result_to_parsed_file,
        )

        root_namespace = self._root_namespace_for(file_info)

        async def _once() -> Any:
            results = await parse_batch(self._client, [file_info], root_namespace=root_namespace)
            result = results.get(file_info.path)
            if result is None:
                return empty_parsed_file(
                    file_info,
                    source,
                    parse_errors=["VB sidecar returned no result for this file"],
                )
            return sidecar_result_to_parsed_file(file_info, source, result)

        return asyncio.run_coroutine_threadsafe(_once(), self._loop).result()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._client.shutdown(), self._loop).result(timeout=10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        self._loop.close()

    def __enter__(self) -> VbSyncBridge:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


_SIDECAR_REGISTRY: dict[Path, VbSidecarClient] = {}


def get_sidecar(repo_path: Path) -> VbSidecarClient:
    """Per-repo singleton client, run-scoped (D6).

    ``pipeline/phases/ingestion.py``, ``pipeline/incremental.py`` and
    ``pipeline/reparse.py`` all call this to join whatever sidecar the
    current run already started rather than spawning their own.
    """
    key = Path(repo_path).resolve()
    client = _SIDECAR_REGISTRY.get(key)
    if client is None:
        client = VbSidecarClient(key)
        _SIDECAR_REGISTRY[key] = client
    return client


def lookup_file_result(abs_path: str) -> dict[str, Any] | None:
    """Find the stashed sidecar "parse" result DTO for *abs_path*, if any.

    Searches every registered client rather than requiring a repo_path,
    since callers that only have a :class:`FileInfo` (health's
    ``walk_vb_file``) don't otherwise know which repo's sidecar produced
    it. Safe because absolute paths are globally unique — no collision
    risk even with multiple repos' clients registered at once. The entry
    disappears once :func:`shutdown_sidecar` tears the client down at the
    end of the run that produced it.
    """
    for client in _SIDECAR_REGISTRY.values():
        result = client.file_results.get(abs_path)
        if result is not None:
            return result
    return None


async def shutdown_sidecar(repo_path: Path) -> None:
    """Tear down and forget the sidecar for *repo_path*, if one was started."""
    key = Path(repo_path).resolve()
    client = _SIDECAR_REGISTRY.pop(key, None)
    if client is not None:
        await client.shutdown()
