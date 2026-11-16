"""Sandbox configuration (``FABRIC_SANDBOX_`` prefix)."""

from __future__ import annotations

import sys
from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_TOKEN_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FABRIC_SANDBOX_", extra="ignore")

    host: str = "0.0.0.0"  # noqa: S104 - a container binds its own namespace
    port: int = 8090

    #: Shared secret the gateway sends in ``X-Fabric-Sandbox-Token``. Required: the service
    #: refuses to start without one, and refuses every request that does not carry it.
    token: str = ""

    #: Writable root for session working directories and per-execution scratch space. In
    #: Kubernetes this is the only writable mount (an emptyDir with a size limit).
    root_dir: str = "/var/lib/fabric-sandbox"

    #: Interpreter that runs user code. The image points this at a dedicated environment
    #: holding only the scientific stack; it defaults to this process's interpreter.
    python: str = sys.executable

    #: Execution limits, applied per execution with setrlimit in the child before any user
    #: code runs. Hard and soft limits are set together, so user code cannot raise them.
    default_timeout_seconds: float = Field(default=30.0, gt=0)
    max_timeout_seconds: float = Field(default=120.0, gt=0)
    cpu_seconds: int = Field(default=30, ge=1)
    memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2)
    file_size_bytes: int = Field(default=64 * 1024**2, ge=1024)
    open_files: int = Field(default=256, ge=16)
    #: RLIMIT_NPROC counts every task of the uid, including this service's own threads
    #: and other concurrent executions, so it is a fork-bomb brake rather than a quota.
    max_processes: int = Field(default=256, ge=8)
    max_output_bytes: int = Field(default=32 * 1024, ge=256)
    max_code_bytes: int = Field(default=256 * 1024, ge=64)

    #: Files produced by an execution and returned inline (images only), base64.
    max_returned_file_bytes: int = Field(default=2 * 1024**2, ge=0)
    max_returned_files: int = Field(default=10, ge=0)

    #: Per-run sessions: files persist between executions of one run until idle this long.
    session_ttl_seconds: float = Field(default=1800.0, gt=0)
    session_max_bytes: int = Field(default=256 * 1024**2, ge=1024)
    max_sessions: int = Field(default=1000, ge=1)
    sweep_interval_seconds: float = Field(default=30.0, gt=0)

    #: Executions this replica runs at once; further requests wait up to the queue timeout
    #: and are then refused with 429 so the gateway can report ``sandbox_busy``.
    max_concurrent: int = Field(default=4, ge=1)
    queue_timeout_seconds: float = Field(default=10.0, ge=0)

    #: Run user code as this uid/gid. Only possible when the service runs as root; the
    #: image instead runs everything as an unprivileged user (see docs/hosted-tools.md).
    run_as_uid: int | None = None
    run_as_gid: int | None = None
    #: Running user code as root is refused unless this is set (tests in throwaway
    #: containers only).
    allow_root: bool = False

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if len(self.token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"FABRIC_SANDBOX_TOKEN must be set (at least {MIN_TOKEN_LENGTH} characters): "
                "the sandbox never accepts unauthenticated executions"
            )
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("default_timeout_seconds must not exceed max_timeout_seconds")
        return self
