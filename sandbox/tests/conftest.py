from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

import pytest

from fabric_sandbox.config import Settings

TOKEN = "test-sandbox-token-0123456789"
IS_ROOT = os.geteuid() == 0


@pytest.fixture
def root_dir() -> Iterator[str]:
    # Under /tmp with a traversable parent, so a uid-switched child can reach its directory.
    path = tempfile.mkdtemp(prefix="fabric-sandbox-test-")
    os.chmod(path, 0o711)
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def make_settings(root_dir: str):
    def build(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "token": TOKEN,
            "root_dir": root_dir,
            "default_timeout_seconds": 10,
            "queue_timeout_seconds": 1,
            # CI runs unprivileged; a root dev container opts in explicitly.
            "allow_root": IS_ROOT,
        }
        values.update(overrides)
        return Settings(**values)

    return build
