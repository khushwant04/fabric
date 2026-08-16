"""Tenant context for PostgreSQL row-level security.

Authorization is enforced in the services: every query filters by an ``account_id``
taken from verified credentials. Row-level security is defence in depth for the day
a handler forgets that filter. It cannot replace the service checks, because the
database cannot know which account a caller *should* be, only which one the
connection has declared.

The declared account travels in a context variable and is applied to the database
session as a transaction-local setting, which policies read with
``current_setting('fabric.account_id', true)``.

Two details make this safe:

* The setting is transaction-local, never session-level. A session-level ``SET``
  would survive the connection returning to the pool, and the next request on that
  connection would inherit another tenant's context. Pool reset rolls transactions
  back, so transaction-local settings cannot leak.
* It is re-applied on every transaction begin, not once per request. Handlers commit
  part-way through their work, and a commit ends the transaction and discards the
  setting, so a single application would silently lose the context for everything
  after the first commit.

Some work legitimately spans accounts or happens before an account is known:
exchanging an API key for a token has to find the key before it can know its owner,
and a machine credential's account comes from the stamp or placement it names. Those
paths run in an explicit system context, which policies allow. That is a deliberate
narrowing: row-level security protects the account-scoped request paths, while the
cross-account paths remain protected by the service checks and their tests.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import logging
import uuid
from collections.abc import AsyncIterator, Iterator

from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

#: Account whose rows the current work may touch.
_account: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "fabric_account_id", default=None
)

#: Set for work that legitimately spans accounts or precedes authentication.
_system: contextvars.ContextVar[bool] = contextvars.ContextVar("fabric_system", default=False)

logger = logging.getLogger(__name__)

ACCOUNT_SETTING = "fabric.account_id"
SYSTEM_SETTING = "fabric.system"


def reset_context() -> None:
    """Forget any declared context.

    Exists for test isolation and for long-lived workers that reuse a task between
    units of work; a request-scoped task discards its own context when it ends.
    """
    _account.set(None)
    _system.set(False)


def current_account() -> uuid.UUID | None:
    return _account.get()


def in_system_context() -> bool:
    return _system.get()


def declare_account(account_id: uuid.UUID) -> None:
    """Declare the account for the rest of this request.

    Used from request dependencies rather than a context manager: FastAPI resolves
    dependencies and runs the endpoint in one task, and a task's context variables
    are discarded when it ends, so there is nothing to leak into another request.
    """
    _account.set(account_id)


async def declare_system(session: AsyncSession) -> None:
    """Elevate the open transaction to cross-account work.

    Deliberately transaction-scoped and nothing more. An earlier version set a
    context variable, which meant that once a request had exchanged a token or
    enrolled a stamp, everything else it did stayed elevated: the elevation outlived
    the operation that needed it. A transaction-local setting expires when the
    transaction ends, so the elevation cannot outlast the work.

    A caller that commits and then continues loses the elevation and sees nothing,
    which is the failure worth having: it stops loudly instead of quietly reading
    every account.
    """
    await _set_system(session, True)


@contextlib.asynccontextmanager
async def elevated(session: AsyncSession) -> AsyncIterator[None]:
    """Elevate one lookup that legitimately spans accounts, then drop back.

    Some reads cannot be account-scoped and still be correct. A customer placing a
    deployment onto shared managed capacity has to resolve a stamp owned by the
    Fabric system account, and listing a person's memberships has to cross the
    accounts they belong to.

    The elevation is released on exit rather than left for the rest of the
    transaction, so the writes that follow are still constrained to the caller's
    account. Whether the caller may use what it read stays a service decision:
    entitlement and ownership checks are unchanged and keep their own tests.
    """
    # The previous value is restored rather than forced off, so an elevation nested
    # inside a wider one does not silently end it. Enrolling a stamp does exactly
    # that: it runs elevated and looks up the system account, which elevates again.
    previous = await _read_system(session)
    await _write_system(session, "on")
    try:
        yield
    finally:
        await _write_system(session, previous)


async def _read_system(session: AsyncSession) -> str:
    if session.get_bind().dialect.name != "postgresql":
        return "off"
    value = (
        await session.execute(text("SELECT current_setting(:name, true)"), {"name": SYSTEM_SETTING})
    ).scalar_one()
    return value or "off"


async def _write_system(session: AsyncSession, value: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": SYSTEM_SETTING, "value": value},
    )


async def _set_system(session: AsyncSession, enabled: bool) -> None:
    await _write_system(session, "on" if enabled else "off")


@contextlib.contextmanager
def account_context(account_id: uuid.UUID) -> Iterator[None]:
    """Declare the account whose rows may be read and written."""
    token = _account.set(account_id)
    try:
        yield
    finally:
        _account.reset(token)


@contextlib.contextmanager
def system_context() -> Iterator[None]:
    """Declare work that spans accounts or runs before one is known.

    Kept explicit and narrow: an unmarked handler runs with no account declared and
    therefore sees nothing, which fails loudly rather than leaking.
    """
    token = _system.set(True)
    try:
        yield
    finally:
        _system.reset(token)


def apply_context(connection: Connection) -> None:
    """Apply the current context to an open transaction.

    Takes the connection the event supplies rather than asking the session for one:
    the session is still provisioning that connection at this point, and asking again
    re-enters it.
    """
    if connection.dialect.name != "postgresql":
        # SQLite has no row-level security. Tests that assert isolation skip
        # themselves rather than passing vacuously on an engine that cannot enforce
        # it.
        return

    account = _account.get()
    # One statement, so declaring context costs one round trip per transaction.
    # Bound parameters rather than driver-level placeholders: the async driver uses
    # its own parameter style, and a literal here would be an injection seam.
    connection.execute(
        text(
            "SELECT set_config(:account_name, :account_value, true), "
            "set_config(:system_name, :system_value, true)"
        ),
        {
            "account_name": ACCOUNT_SETTING,
            "account_value": "" if account is None else str(account),
            "system_name": SYSTEM_SETTING,
            "system_value": "on" if _system.get() else "off",
        },
    )


def install_session_context() -> None:
    """Re-apply the tenant context whenever a transaction begins.

    Registered once, on the Session class, so no call site has to remember. A
    handler that commits and continues working gets the context restored for the
    next transaction automatically.
    """
    if getattr(install_session_context, "_installed", False):
        return

    @event.listens_for(Session, "after_begin")
    def _after_begin(_session: Session, _transaction: object, connection: Connection) -> None:
        apply_context(connection)

    install_session_context._installed = True  # type: ignore[attr-defined]


@dataclasses.dataclass(frozen=True)
class RoleCapability:
    """What the connecting role can do to row-level security."""

    role: str
    is_superuser: bool
    bypasses_rls: bool

    @property
    def policies_enforced(self) -> bool:
        """Whether policies actually apply to this role.

        Enabling and forcing row-level security is not enough. A superuser, or any
        role with BYPASSRLS, ignores policies entirely while ``pg_class`` still
        reports them as enabled and forced, so the protection looks present and is
        not. Managed PostgreSQL makes this easy to hit: the default owner role on at
        least one popular provider has BYPASSRLS.
        """
        return not (self.is_superuser or self.bypasses_rls)


async def inspect_role(session: AsyncSession) -> RoleCapability | None:
    """Report whether policies bind the role this process connects as."""
    if session.get_bind().dialect.name != "postgresql":
        return None
    row = (
        await session.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).one()
    return RoleCapability(role=row[0], is_superuser=row[1], bypasses_rls=row[2])


async def verify_policies_are_enforceable(session: AsyncSession) -> RoleCapability | None:
    """Log loudly when row-level security cannot bind this connection.

    Deliberately not fatal: a deployment may legitimately run migrations or recovery
    as an administrative role, and refusing to start would turn a hardening gap into
    an outage. It is logged at error level with the remedy, because the failure is
    otherwise completely silent.
    """
    capability = await inspect_role(session)
    if capability is None or capability.policies_enforced:
        return capability

    logger.error(
        "row-level security is not enforced for database role %r "
        "(superuser=%s, bypassrls=%s): account isolation policies are present but "
        "ignored for this connection. Connect as a role created by "
        "control-plane/scripts/create-app-role.sql.",
        capability.role,
        capability.is_superuser,
        capability.bypasses_rls,
    )
    return capability
