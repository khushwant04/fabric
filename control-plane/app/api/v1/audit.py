"""Audit-trail endpoints.

Audit rows have been written in the same transaction as every privileged change since
the first schema; these endpoints are how an account reads them back. Reads are never
cached: an audit view that lags the changes it records is the one view that must not.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scopes as scope_defs
from app.core.database import get_db_session
from app.core.errors import BadRequest
from app.core.security import PrincipalContext, account_scope
from app.schemas import AuditEventPage, AuditEventResponse
from app.services.audit import AuditFilter, list_audit_events, record_audit

router = APIRouter(tags=["audit"])

_BASE = "/v1/accounts/{account_id}/audit-events"

#: Rows in one export response. Larger trails continue from ``Fabric-Next-Cursor``, so a
#: single request never holds an unbounded result in memory.
EXPORT_PAGE_LIMIT = 5000

NEXT_CURSOR_HEADER = "Fabric-Next-Cursor"


def _filters(
    action: str | None = Query(default=None, max_length=128, description="Exact action"),
    resource_type: str | None = Query(default=None, max_length=64),
    resource_id: str | None = Query(default=None, max_length=128),
    actor_id: str | None = Query(default=None, max_length=128),
    since: dt.datetime | None = Query(default=None, description="Inclusive lower bound"),
    until: dt.datetime | None = Query(default=None, description="Exclusive upper bound"),
) -> AuditFilter:
    bounds = []
    for value in (since, until):
        if value is not None and value.tzinfo is None:
            raise BadRequest("invalid_time_bound", "since and until need a UTC offset")
        # Stored timestamps are UTC; comparing in one zone keeps every engine consistent.
        bounds.append(value.astimezone(dt.UTC) if value is not None else None)
    if bounds[0] is not None and bounds[1] is not None and bounds[0] >= bounds[1]:
        raise BadRequest("invalid_time_range", "since must be earlier than until")
    return AuditFilter(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        actor_id=actor_id,
        since=bounds[0],
        until=bounds[1],
    )


@router.get(
    _BASE,
    response_model=AuditEventPage,
    summary="List the account's audit trail, newest first",
)
async def list_events(
    filters: AuditFilter = Depends(_filters),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalContext = Depends(account_scope(scope_defs.AUDIT_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> AuditEventPage:
    events, next_cursor = await list_audit_events(
        session, account_id=principal.account_id, filters=filters, limit=limit, cursor=cursor
    )
    return AuditEventPage(
        events=[AuditEventResponse.model_validate(event) for event in events],
        next_cursor=next_cursor,
    )


@router.get(
    _BASE + "/export",
    summary="Export the account's audit trail as newline-delimited JSON",
    response_class=Response,
    responses={200: {"content": {"application/x-ndjson": {}}}},
)
async def export_events(
    filters: AuditFilter = Depends(_filters),
    cursor: str | None = Query(default=None, max_length=512),
    principal: PrincipalContext = Depends(account_scope(scope_defs.AUDIT_READ)),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    events, next_cursor = await list_audit_events(
        session,
        account_id=principal.account_id,
        filters=filters,
        limit=EXPORT_PAGE_LIMIT,
        cursor=cursor,
    )
    lines = [
        AuditEventResponse.model_validate(event).model_dump_json() for event in events
    ]
    # Taking a copy of the trail is itself a privileged act, so it leaves a record. It is
    # written after the read, so an export never contains its own event.
    await record_audit(
        session,
        account_id=principal.account_id,
        actor_type=principal.principal_type,
        actor_id=str(principal.principal_id) if principal.principal_id else None,
        action="audit.exported",
        resource_type="audit_events",
        metadata={
            "filters": filters.as_metadata(),
            "events": len(lines),
            "continued": cursor is not None,
        },
    )
    await session.commit()
    headers = {NEXT_CURSOR_HEADER: next_cursor} if next_cursor else {}
    body = "".join(line + "\n" for line in lines)
    return Response(content=body, media_type="application/x-ndjson", headers=headers)

