"""ASGI entrypoints.

The two listeners are separate applications on separate ports, because AR-DP03
requires the administrative listener to be isolated from the inference listener.
Only the inference port should ever be reachable from a customer network.

These objects exist for running one listener at a time, which is useful in
development:

    uvicorn fabric_data_plane.main:inference --host 0.0.0.0 --port 8080

For deployment use :mod:`fabric_data_plane.serve`, which runs both in one process.
They must share a process: the usage buffer is in memory, so an administrative
listener in a separate process would drain a buffer that never saw any request and
report no usage at all, silently. Importing this module twice produces two planes
with two buffers, which is exactly the mistake to avoid.
"""

from __future__ import annotations

import logging

from fabric_data_plane.app import build_plane, create_admin_app, create_inference_app
from fabric_data_plane.config import get_settings

logging.basicConfig(level=get_settings().log_level.upper())

_plane = build_plane()

inference = create_inference_app(_plane)
admin = create_admin_app(_plane)
