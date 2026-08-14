"""Row-level security: account isolation policies on tenant tables.

Revision ID: 0002_row_level_security
Revises: 0001_initial
Create Date: 2026-08-14

Authorization already happens in the services, which filter every query by an
account taken from verified credentials. These policies are defence in depth for the
day a handler forgets that filter: the database refuses to return or write another
account's row even if the SQL asks for it.

PostgreSQL only. SQLite has no row-level security, so this migration is a no-op
there and the isolation tests skip rather than pass vacuously.

Two decisions worth stating:

* ``FORCE ROW LEVEL SECURITY`` is applied as well as ``ENABLE``. A table's owner
  bypasses policies otherwise, and the application commonly connects as the owner of
  its schema, which would make the policies decorative in exactly the deployment
  they are meant to protect. Superusers still bypass, which is why migrations and
  administrative access continue to work.
* Policies read a transaction-local setting rather than a database role per tenant.
  Roles do not scale to accounts created at runtime, and a connection pool cannot
  switch roles safely.

A caller with no declared account sees nothing. That is deliberate: a missing
context fails closed and loudly instead of quietly returning every row.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_row_level_security"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "fabric_account_isolation"

#: Tables whose rows belong to exactly one account. ``users`` is absent on purpose:
#: a human identity is global and may belong to several accounts, and membership is
#: what carries the account.
ACCOUNT_TABLES = (
    "account_memberships",
    "service_principals",
    "api_keys",
    "deployments",
    "inference_stamps",
    "stamp_enrollment_tokens",
    "stamp_credentials",
    "telemetry_credentials",
    "deployment_placements",
    "deployment_status",
    "usage_events",
    "audit_events",
    "outbox_events",
    "idempotency_keys",
)

#: The accounts table identifies the tenant by its primary key.
SELF_SCOPED = {"accounts": "id"}

#: True when the transaction declared this account, or declared system context.
#: ``nullif`` maps an unset or blank setting to NULL, and a comparison with NULL is
#: never true, so an undeclared context matches no row.
PREDICATE = (
    "current_setting('fabric.system', true) = 'on' "
    "OR {column} = nullif(current_setting('fabric.account_id', true), '')::uuid"
)


def _tables() -> list[tuple[str, str]]:
    return [(table, "account_id") for table in ACCOUNT_TABLES] + list(SELF_SCOPED.items())


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table, column in _tables():
        predicate = PREDICATE.format(column=column)
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} "
            f"USING ({predicate}) WITH CHECK ({predicate})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table, _column in _tables():
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
