"""ASGI entrypoints.

The two listeners are separate processes on separate ports, because AR-DP03
requires the administrative listener to be isolated from the inference listener.
Only the inference port should ever be reachable from a customer network.

    uvicorn fabric_data_plane.main:inference --host 0.0.0.0 --port 8080
    uvicorn fabric_data_plane.main:admin     --host 127.0.0.1 --port 8081

Both share one process-local plane when run together in tests, but in deployment
each process builds its own; the only shared state is the key cache and usage
buffer, neither of which is authoritative for another process.
"""

from __future__ import annotations

import logging

from fabric_data_plane.app import build_plane, create_admin_app, create_inference_app
from fabric_data_plane.config import get_settings

logging.basicConfig(level=get_settings().log_level.upper())

_plane = build_plane()

inference = create_inference_app(_plane)
admin = create_admin_app(_plane)
