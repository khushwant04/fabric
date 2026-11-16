"""Stamp activity log, and weight download progress on host snapshots.

``stamp_events`` is a bounded timeline per stamp: the agent pushes lifecycle events
(enrolled, GPUs measured, GPU runtime checked, credentials rotated, desired state applied,
model-host image pulled, errors) and the console shows them on the stamp page. Only the
newest events are kept per stamp. Rows carry the stamp's account and the table has the same
forced account-isolation policy as every other account table.

``deployment_host_snapshots.weights`` carries how much of a model's weights the node's cache
holds, so the console can show download progress while a host starts.

Revision ID: 0007_stamp_events
Revises: 0006_deployment_host_snapshots
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.database import JsonColumn

revision = "0007_stamp_events"
down_revision = "0006_deployment_host_snapshots"
branch_labels = None
depends_on = None

TABLE = "stamp_events"
POLICY = "fabric_account_isolation"
PREDICATE = (
    "current_setting('fabric.system', true) = 'on' "
    "OR account_id = nullif(current_setting('fabric.account_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("stamp_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_key", sa.String(64), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("hint", sa.String(500), nullable=True),
        sa.Column("details", JsonColumn, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_stamp_events"),
        sa.ForeignKeyConstraint(
            ["account_id", "stamp_id"],
            ["inference_stamps.account_id", "inference_stamps.id"],
            name="fk_stamp_events_stamp",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("stamp_id", "event_key", name="uq_stamp_events_stamp_id_event_key"),
    )
    op.create_index("ix_stamp_events_stamp_id_occurred_at", TABLE, ["stamp_id", "occurred_at"])
    with op.batch_alter_table("deployment_host_snapshots") as batch:
        batch.add_column(sa.Column("weights", JsonColumn, nullable=True))

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} USING ({PREDICATE}) WITH CHECK ({PREDICATE})"
    )


def downgrade() -> None:
    with op.batch_alter_table("deployment_host_snapshots") as batch:
        batch.drop_column("weights")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.drop_index("ix_stamp_events_stamp_id_occurred_at", table_name=TABLE)
    op.drop_table(TABLE)
