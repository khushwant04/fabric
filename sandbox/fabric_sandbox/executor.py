"""Isolated execution of model-written Python, one fresh subprocess per call.

Isolation layers implemented here (docs/hosted-tools.md lists them with what each does and
does not stop; network isolation is the deployment's job):

* a fresh interpreter per execution (``python -I -S``: no ``PYTHON*`` variables, no user
  site, no ``sitecustomize``), started in its own session / process group;
* a scrubbed environment: only a fixed allowlist of variables reaches the child, never this
  service's own environment (which holds the shared token);
* setrlimit in the child before user code: CPU seconds, address space, file size, open
  files, processes, no core dumps (``runner.py``);
* a wall-clock timeout that SIGKILLs the whole process group, and a group kill after every
  execution so background children do not outlive it;
* stdout and stderr read to a cap, the rest drained and discarded;
* per-run working directories with unguessable names (HMAC of the session id with a
  per-process key) under a parent that cannot be listed, removed after an idle TTL, with a
  total size cap per session;
* this process and every child mark themselves non-dumpable, so code running under the same
  uid cannot read another process's environment, memory or cwd via ``/proc`` or ptrace it;
* optional uid/gid switch for the child when the service runs as root, and refusal to run
  user code as root otherwise.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fabric_sandbox.config import Settings

log = logging.getLogger("fabric_sandbox")

RUNNER = str(Path(__file__).with_name("runner.py"))
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
}
_PR_SET_DUMPABLE = 4
#: Upper bound on files examined per execution, so a directory with a million entries
#: cannot make the post-execution scan expensive.
_MAX_SCAN = 2000


class SandboxBusy(Exception):
    """Every execution slot stayed taken for the queue timeout."""


class InvalidRequest(Exception):
    pass


def harden_process() -> None:
    """Mark this process non-dumpable (Linux): its /proc entries become root-owned."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _child_site_dirs(python: str) -> list[str]:
    """Ask the child interpreter where its packages live (it starts with ``-S``)."""
    probe = "import json, site; print(json.dumps(site.getsitepackages()))"
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, configured interpreter
            [python, "-I", "-c", probe], capture_output=True, text=True, timeout=30, check=True
        ).stdout
        return [d for d in json.loads(out) if os.path.isdir(d)]
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("could not list the interpreter's site directories: %s", exc)
        return []


def _force_rmtree(path: Path) -> None:
    def retry(func: Any, target: str, _exc: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(target) or ".", 0o700)
            os.chmod(target, 0o700)
        with contextlib.suppress(OSError):
            func(target)

    shutil.rmtree(path, onexc=retry)


def _dir_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files + dirs:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_size
    return total


@dataclasses.dataclass
class Session:
    key: str
    workdir: Path
    cache: Path
    last_used: float
    lock: asyncio.Lock = dataclasses.field(default_factory=asyncio.Lock)
    executions: int = 0


@dataclasses.dataclass
class Result:
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    exit_code: int | None
    timed_out: bool
    killed_reason: str | None
    duration_ms: float
    files: list[dict[str, Any]]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Sandbox:
    def __init__(self, settings: Settings, *, clock: Any = time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        if os.geteuid() == 0 and settings.run_as_uid in (None, 0) and not settings.allow_root:
            raise RuntimeError(
                "refusing to run user code as root: run the sandbox as an unprivileged user, "
                "or set FABRIC_SANDBOX_RUN_AS_UID when it must start as root"
            )
        if settings.run_as_uid is not None and os.geteuid() != 0:
            raise RuntimeError("FABRIC_SANDBOX_RUN_AS_UID needs the service to start as root")
        self.root = Path(settings.root_dir)
        self.sessions_dir = self.root / "sessions"
        self.tmp_dir = self.root / "tmp"
        self.template_dir = self.root / "mpl-template"
        # Directory names are an HMAC of the session id under a key that never leaves this
        # process, so code that knows another run's id still cannot compute its path.
        self._key = secrets.token_bytes(32)
        self._sessions: dict[str, Session] = {}
        self._slots = asyncio.Semaphore(settings.max_concurrent)
        self._site_dirs = _child_site_dirs(settings.python)
        self.active = 0
        self.counters: dict[str, int] = {
            "executions": 0,
            "timeouts": 0,
            "failures": 0,
            "busy": 0,
        }
        self._prepare_root()

    # ------------------------------------------------------------------ filesystem

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        # A restart forgets every session; leftover directories are unreachable, so remove.
        for child in (self.sessions_dir, self.tmp_dir):
            if child.exists():
                _force_rmtree(child)
            child.mkdir()
        # 0711: children may traverse into their own (unguessable) directory but cannot list
        # the parent to discover other runs' directories.
        os.chmod(self.sessions_dir, 0o711)
        os.chmod(self.tmp_dir, 0o711)

    def _chown(self, path: Path) -> None:
        if self.settings.run_as_uid is not None:
            gid = self.settings.run_as_gid
            os.chown(path, self.settings.run_as_uid, gid if gid is not None else -1)

    def _session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            if len(self._sessions) >= self.settings.max_sessions:
                self._evict_one()
            name = hmac.new(self._key, session_id.encode(), hashlib.sha256).hexdigest()
            workdir = self.sessions_dir / name
            cache = self.sessions_dir / f"{name}.cache"
            for path in (workdir, cache):
                path.mkdir(mode=0o700)
                self._chown(path)
            self._seed_font_cache(cache)
            session = Session(session_id, workdir, cache, self.clock())
            self._sessions[session_id] = session
        session.last_used = self.clock()
        return session

    def _seed_font_cache(self, cache: Path) -> None:
        """Copy matplotlib's font list so a run's first plot does not rebuild it (~1-2 s)."""
        with contextlib.suppress(OSError):
            for item in self.template_dir.glob("fontlist-*.json"):
                target = cache / item.name
                shutil.copyfile(item, target)
                self._chown(target)

    async def warm_font_cache(self) -> None:
        """Build matplotlib's font list once, with the same bootstrap executions use."""
        if self.template_dir.exists():
            return
        self.template_dir.mkdir(mode=0o700)
        env = {"MPLCONFIGDIR": str(self.template_dir), "PATH": "/usr/bin:/bin"}
        with contextlib.suppress(OSError):
            proc = await asyncio.create_subprocess_exec(
                self.settings.python,
                "-I",
                "-S",
                RUNNER,
                json.dumps(self._runner_config()),
                env=env,
                cwd=str(self.template_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(b"import matplotlib.font_manager"), 120)

    def _evict_one(self) -> None:
        idle = [s for s in self._sessions.values() if not s.lock.locked()]
        if not idle:
            raise SandboxBusy("session table is full")
        oldest = min(idle, key=lambda s: s.last_used)
        self._drop(oldest)

    def _drop(self, session: Session) -> None:
        self._sessions.pop(session.key, None)
        for path in (session.workdir, session.cache):
            with contextlib.suppress(OSError):
                _force_rmtree(path)

    def sweep(self) -> int:
        """Remove sessions idle past the TTL; returns how many were removed."""
        cutoff = self.clock() - self.settings.session_ttl_seconds
        expired = [
            s for s in self._sessions.values() if s.last_used < cutoff and not s.lock.locked()
        ]
        for session in expired:
            self._drop(session)
        return len(expired)

    def delete_session(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.lock.locked():
            return False
        self._drop(session)
        return True

    def session_count(self) -> int:
        return len(self._sessions)

    # ------------------------------------------------------------------ execution

    def _runner_config(self) -> dict[str, Any]:
        s = self.settings
        return {
            "cpu_seconds": s.cpu_seconds,
            "memory_bytes": s.memory_bytes,
            "file_size_bytes": s.file_size_bytes,
            "open_files": s.open_files,
            "max_processes": s.max_processes,
            "site_dirs": self._site_dirs,
        }

    def _env(self, session: Session, scratch: Path) -> dict[str, str]:
        # Built from nothing: the service's environment (and its token) never reaches user
        # code. -I makes the interpreter ignore PYTHON* variables in any case.
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(session.workdir),
            "TMPDIR": str(scratch),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "MPLBACKEND": "Agg",
            "MPLCONFIGDIR": str(session.cache),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_MAX_THREADS": "1",
        }

    def _snapshot(self, workdir: Path) -> dict[str, tuple[int, int]]:
        seen: dict[str, tuple[int, int]] = {}
        for root, dirs, files in os.walk(workdir, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if len(seen) >= _MAX_SCAN:
                    return seen
                path = os.path.join(root, name)
                with contextlib.suppress(OSError):
                    info = os.lstat(path)
                    if stat.S_ISREG(info.st_mode):
                        seen[os.path.relpath(path, workdir)] = (info.st_mtime_ns, info.st_size)
        return seen

    def _read_inline(self, workdir: Path, name: str, size: int) -> str | None:
        """Read a produced image for the response, never following a link out of the dir."""
        if size > self.settings.max_returned_file_bytes:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(workdir / name, flags)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            # A hard link could point at a file outside the session; refuse anything shared.
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                return None
            data = os.read(fd, self.settings.max_returned_file_bytes + 1)
        finally:
            os.close(fd)
        if len(data) > self.settings.max_returned_file_bytes:
            return None
        return base64.b64encode(data).decode()

    def _produced(self, workdir: Path, before: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
        after = self._snapshot(workdir)
        files: list[dict[str, Any]] = []
        inline = 0
        for name, (mtime, size) in sorted(after.items()):
            if before.get(name) == (mtime, size):
                continue
            mime = _IMAGE_TYPES.get(Path(name).suffix.lower())
            entry: dict[str, Any] = {"name": name, "bytes": size, "mime_type": mime}
            if mime and inline < self.settings.max_returned_files:
                content = self._read_inline(workdir, name, size)
                if content is not None:
                    entry["content_base64"] = content
                    inline += 1
                else:
                    entry["omitted"] = "size_cap"
            files.append(entry)
        return files

    async def _read_capped(self, stream: asyncio.StreamReader, cap: int) -> tuple[bytes, bool]:
        kept = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            room = cap - len(kept)
            if room > 0:
                kept.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True  # keep draining so the child never blocks on a full pipe
        return bytes(kept), truncated

    def _kill_group(self, pid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)

    async def execute(
        self, session_id: str, code: str, timeout_seconds: float | None = None
    ) -> Result:
        if not SESSION_ID.fullmatch(session_id or ""):
            raise InvalidRequest("session_id must match [A-Za-z0-9_-]{1,128}")
        if not isinstance(code, str) or not code.strip():
            raise InvalidRequest("code must be a non-empty string")
        if len(code.encode()) > self.settings.max_code_bytes:
            raise InvalidRequest(f"code exceeds {self.settings.max_code_bytes} bytes")
        timeout = min(
            timeout_seconds or self.settings.default_timeout_seconds,
            self.settings.max_timeout_seconds,
        )
        try:
            await asyncio.wait_for(self._slots.acquire(), self.settings.queue_timeout_seconds)
        except TimeoutError as exc:
            self.counters["busy"] += 1
            raise SandboxBusy("every execution slot is in use") from exc
        try:
            session = self._session(session_id)
            async with session.lock:
                self.active += 1
                try:
                    result = await self._run(session, code, timeout)
                finally:
                    self.active -= 1
                    session.last_used = self.clock()
                    session.executions += 1
        finally:
            self._slots.release()
        self.counters["executions"] += 1
        if result.timed_out:
            self.counters["timeouts"] += 1
        if result.error or result.exit_code != 0:
            self.counters["failures"] += 1
        return result

    async def _run(self, session: Session, code: str, timeout: float) -> Result:
        settings = self.settings
        scratch = self.tmp_dir / uuid.uuid4().hex
        scratch.mkdir(mode=0o700)
        self._chown(scratch)
        before = self._snapshot(session.workdir)
        started = time.perf_counter()
        user: dict[str, Any] = {}
        if settings.run_as_uid is not None:
            user = {"user": settings.run_as_uid, "extra_groups": []}
            if settings.run_as_gid is not None:
                user["group"] = settings.run_as_gid
        proc = await asyncio.create_subprocess_exec(
            settings.python,
            "-I",
            "-S",
            RUNNER,
            json.dumps(self._runner_config()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(session.workdir),
            env=self._env(session, scratch),
            start_new_session=True,
            close_fds=True,
            **user,
        )
        assert proc.stdin and proc.stdout and proc.stderr
        cap = settings.max_output_bytes
        out_task = asyncio.create_task(self._read_capped(proc.stdout, cap))
        err_task = asyncio.create_task(self._read_capped(proc.stderr, cap))
        timed_out = False
        try:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(code.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            timed_out = True
        finally:
            # Always kill the whole group: on timeout, and also after a normal exit so a
            # background process the code started does not outlive the execution.
            self._kill_group(proc.pid)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
        # A grandchild that escaped the group could hold the pipes open; do not wait on it.
        done, pending = await asyncio.wait({out_task, err_task}, timeout=2)
        for task in pending:
            task.cancel()
        stdout, out_trunc = out_task.result() if out_task in done else (b"", True)
        stderr, err_trunc = err_task.result() if err_task in done else (b"", True)
        duration = round((time.perf_counter() - started) * 1000, 2)
        _force_rmtree(scratch)

        code_ = proc.returncode
        killed: str | None = None
        if timed_out:
            killed = "timeout"
        elif code_ is not None and code_ < 0:
            sig = -code_
            killed = {
                signal.SIGXCPU: "cpu_limit",
                signal.SIGKILL: "cpu_limit",  # the hard CPU limit is soft + 1 s
                signal.SIGXFSZ: "file_size_limit",
                signal.SIGSEGV: "crashed",
            }.get(sig, f"signal_{sig}")

        files = self._produced(session.workdir, before)
        error = None
        if _dir_size(session.workdir) > settings.session_max_bytes:
            # Over quota: the files this execution wrote are removed, earlier ones kept.
            for entry in files:
                with contextlib.suppress(OSError):
                    os.unlink(session.workdir / entry["name"])
            files = []
            error = "session_storage_exceeded"
        return Result(
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_truncated=out_trunc,
            stderr_truncated=err_trunc,
            exit_code=code_,
            timed_out=timed_out,
            killed_reason=killed,
            duration_ms=duration,
            files=files,
            error=error,
        )
