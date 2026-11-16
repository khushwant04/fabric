"""Latest model-host pod snapshots: status, events and log tail per pod.

A stamp's agent pushes these so the console can show a deployment starting (image pull,
container start, weights loading, readiness) and the tail of the server's log without
anyone running kubectl against the stamp, which the control plane never connects to.

One row per pod, overwritten on every report and deleted when a report stops naming the
pod: the table describes what is running now and does not accumulate. Logs are tenant
data, so the table carries the same forced account-isolation policy as every other
account table.

Revision ID: 0006_deployment_host_snapshots
Revises: 0005_stamp_credential_rotation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.database import JsonColumn

revision = "0006_deployment_host_snapshots"
down_revision = "0005_stamp_credential_rotation"
branch_labels = None
depends_on = None

TABLE = "deployment_host_snapshots"
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
        sa.Column("deployment_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("stamp_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("pod_name", sa.String(253), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("terminating", sa.Boolean(), nullable=False),
        sa.Column("restart_count", sa.Integer(), nullable=False),
        sa.Column("container_state", sa.String(32), nullable=False),
        sa.Column("container_reason", sa.String(128), nullable=True),
        sa.Column("container_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("conditions", JsonColumn, nullable=False),
        sa.Column("events", JsonColumn, nullable=False),
        sa.Column("log_tail", sa.Text(), nullable=False),
        sa.Column("log_truncated", sa.Boolean(), nullable=False),
        sa.Column("log_previous", sa.Boolean(), nullable=False),
        sa.Column("log_error", sa.String(200), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_host_snapshots"),
        sa.ForeignKeyConstraint(
            ["account_id", "deployment_id", "stamp_id"],
            [
                "deployment_placements.account_id",
                "deployment_placements.deployment_id",
                "deployment_placements.stamp_id",
            ],
            name="fk_deployment_host_snapshots_placement",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id",
            "deployment_id",
            "stamp_id",
            "pod_name",
            name="uq_deployment_host_snapshots_pod",
        ),
    )
    op.create_index("ix_deployment_host_snapshots_stamp_id", TABLE, ["stamp_id"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} USING ({PREDICATE}) WITH CHECK ({PREDICATE})"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.drop_index("ix_deployment_host_snapshots_stamp_id", table_name=TABLE)
    op.drop_table(TABLE)
