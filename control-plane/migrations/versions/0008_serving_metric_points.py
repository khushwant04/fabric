"""Serving-metric points: per-minute traffic and GPU series pushed by stamp agents.

The console charts requests, latency, tokens, queue depth, KV cache and GPU use from these
rows (docs/serving-metrics.md). One row per series per minute per stamp; rows older than
seven days are deleted as new samples arrive, so the table is bounded by retention rather
than growing with history.

Points are tenant data. Deployment rows carry the deployment's account and GPU rows the
stamp's account, and the table has the same forced account-isolation policy as every other
account table.

Revision ID: 0008_serving_metric_points
Revises: 0007_stamp_events
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.database import JsonColumn

revision = "0008_serving_metric_points"
down_revision = "0007_stamp_events"
branch_labels = None
depends_on = None

TABLE = "serving_metric_points"
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
        sa.Column("deployment_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("series", sa.String(300), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("data", JsonColumn, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_serving_metric_points"),
        sa.ForeignKeyConstraint(
            ["stamp_id"],
            ["inference_stamps.id"],
            name="fk_serving_metric_points_stamp",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["account_id", "deployment_id"],
            ["deployments.account_id", "deployments.id"],
            name="fk_serving_metric_points_deployment",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("kind in ('deployment', 'gpu')", name="serving_metric_points_kind"),
        sa.UniqueConstraint(
            "stamp_id", "kind", "series", "bucket_start", name="uq_serving_metric_points_bucket"
        ),
    )
    op.create_index(
        "ix_serving_metric_points_deployment",
        TABLE,
        ["account_id", "deployment_id", "bucket_start"],
    )
    op.create_index("ix_serving_metric_points_stamp", TABLE, ["stamp_id", "bucket_start"])

    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {POLICY} ON {TABLE} USING ({PREDICATE}) WITH CHECK ({PREDICATE})")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.drop_index("ix_serving_metric_points_stamp", table_name=TABLE)
    op.drop_index("ix_serving_metric_points_deployment", table_name=TABLE)
    op.drop_table(TABLE)
