"""The edge gateway's routing table (ADR 0021).

One endpoint, readable only by the Fabric edge credential. It answers conditional GETs so
a polling edge costs a hash comparison when nothing has changed, and it never carries a
secret: the edge forwards the caller's own token and the stamp verifies it again.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.security import PrincipalContext, require_control_principal
from app.schemas import EdgeRoutingTable
from app.services.edge import build_routing_table, record_routing_read, require_edge_reader

router = APIRouter(tags=["edge"])


@router.get(
    "/v1/edge/routing-table",
    response_model=EdgeRoutingTable,
    summary="Cross-account routing table for the edge gateway (edge credential only)",
    responses={304: {"description": "Unchanged since the presented ETag"}},
)
async def routing_table(
    response: Response,
    if_none_match: str | None = Header(default=None),
    principal: PrincipalContext = Depends(require_control_principal),
    session: AsyncSession = Depends(get_db_session),
):
    """Deployments placed on live stamps that report a public gateway URL.

    Deliberately not account-scoped: the edge serves every account from one URL. The
    token must carry ``edge:routing:read`` and belong to a service principal of the Fabric
    system account, and the read runs in a scoped elevation that ends with the query.
    """
    principal.require_scopes(scope_defs.EDGE_ROUTING_READ)
    await require_edge_reader(
        session, account_id=principal.account_id, principal_type=principal.principal_type
    )

    table = await build_routing_table(session)
    headers = {
        "ETag": table.etag,
        "X-Fabric-Routing-Revision": str(table.revision),
        "Cache-Control": "no-store",
    }
    presented = {tag.strip() for tag in (if_none_match or "").split(",") if tag.strip()}
    if table.etag in presented or f"W/{table.etag}" in presented:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)

    await record_routing_read(
        session,
        account_id=principal.account_id,
        actor_id=str(principal.principal_id) if principal.principal_id else principal.subject,
        table=table,
    )
    await session.commit()
    response.headers.update(headers)
    return table
