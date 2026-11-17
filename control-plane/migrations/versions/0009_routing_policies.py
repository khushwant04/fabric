"""Routing policies and their published versions.

``routing_policies`` holds one row per public alias an account routes by, with its
editable draft and the number of the live version. ``routing_policy_versions`` holds every
published version, immutable once written. Stamps and the edge receive the live versions
as part of their cached snapshots (ADR 0023). Both tables carry the owning account and get
the same forced account-isolation policy as every other account table.

Revision ID: 0009_routing_policies
Revises: 0008_serving_metric_points
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.core.database import JsonColumn

revision = "0009_routing_policies"
down_revision = "0008_serving_metric_points"
branch_labels = None
depends_on = None

TABLES = ("routing_policies", "routing_policy_versions")
POLICY = "fabric_account_isolation"
PREDICATE = (
    "current_setting('fabric.system', true) = 'on' "
    "OR account_id = nullif(current_setting('fabric.account_id', true), '')::uuid"
)


def upgrade() -> None:
    op.create_table(
        "routing_policies",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("draft", JsonColumn, nullable=False),
        sa.Column(
            "draft_updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("draft_updated_by", sa.String(128), nullable=True),
        sa.Column("live_version", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_routing_policies"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_routing_policies_account_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("account_id", "id", name="uq_routing_policies_account_id_id"),
    )
    op.create_index(
        "uq_routing_policies_active_alias",
        "routing_policies",
        ["account_id", "alias"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "routing_policy_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("policy_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("document", JsonColumn, nullable=False),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("rolled_back_from", sa.Integer(), nullable=True),
        sa.Column("published_by", sa.String(128), nullable=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_routing_policy_versions"),
        sa.ForeignKeyConstraint(
            ["account_id", "policy_id"],
            ["routing_policies.account_id", "routing_policies.id"],
            name="fk_routing_policy_versions_policy",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "policy_id", "version", name="uq_routing_policy_versions_policy_id_version"
        ),
    )
    op.create_index(
        "ix_routing_policy_versions_account_id", "routing_policy_versions", ["account_id"]
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} USING ({PREDICATE}) WITH CHECK ({PREDICATE})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
    op.drop_index("ix_routing_policy_versions_account_id", table_name="routing_policy_versions")
    op.drop_table("routing_policy_versions")
    op.drop_index("uq_routing_policies_active_alias", table_name="routing_policies")
    op.drop_table("routing_policies")
