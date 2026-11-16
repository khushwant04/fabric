"""``python -m fabric_sandbox``: serve the sandbox API."""

from __future__ import annotations

import logging
import os
import tempfile

import uvicorn

from fabric_sandbox.app import create_app
from fabric_sandbox.config import Settings
from fabric_sandbox.executor import harden_process


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Before anything else: the token is in this process's environment, and code running as
    # the same uid must not be able to read it through /proc/<pid>/environ.
    harden_process()
    settings = Settings()
    # The server's own temporary files go under the sandbox root, the only writable mount.
    server_tmp = os.path.join(settings.root_dir, "server-tmp")
    os.makedirs(server_tmp, mode=0o700, exist_ok=True)
    tempfile.tempdir = server_tmp
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
