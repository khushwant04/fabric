"""Routing for stamp-local file search state (docs/file-search.md).

Files and vector stores live on one stamp: its gateway's SQLite database and blob volume.
The edge therefore sends every ``/v1/files*`` and ``/v1/vector_stores*`` call of an account
to one deterministic stamp, the *file stamp*: the highest rendezvous weight for the key
``<account>:files`` among the stamps holding any of the account's deployments. Affinity is
strict (no failover), because a different stamp would answer 404 for every id, which is worse
than an honest 503.

An agent run that declares ``file_search`` must execute on the file stamp too. Its model
candidates are narrowed to that stamp; when the file stamp does not serve the requested model
the run is refused with ``file_search_stamp_mismatch`` rather than run somewhere its stores
do not exist.

The file stamp moves only if the account's set of stamps changes (a stamp gains or loses the
account's last deployment there). Stores on the old stamp are then unreachable through the
edge until the set changes back; see the doc's limits section.
"""

from __future__ import annotations

import uuid
from typing import Any

from fabric_data_plane.edge.routing import rendezvous_order
from fabric_data_plane.edge.table import Placement, RoutingTable

FILE_ROUTE_PREFIXES = ("/v1/files", "/v1/vector_stores")


def file_stamp(table: RoutingTable, account_id: uuid.UUID) -> Placement | None:
    """The one stamp holding this account's files and vector stores, or None."""
    ranked = rendezvous_order(f"{account_id}:files", table.account_stamps(account_id))
    return ranked[0] if ranked else None


def references_file_search(payload: dict[str, Any] | None) -> bool:
    """Whether an agent-run body declares a file_search tool in any accepted shape."""
    tools = (payload or {}).get("tools")
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "file_search":
            return True
        if tool.get("type") == "builtin" and tool.get("name") == "file_search":
            return True
        executor = tool.get("x-fabric-executor")
        if isinstance(executor, dict) and executor.get("name") == "file_search":
            return True
    return False
