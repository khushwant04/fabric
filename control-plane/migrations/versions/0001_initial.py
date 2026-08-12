"""Initial control-plane schema: accounts, identity, credentials, deployments, stamps.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.database import JsonColumn

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = sa.Uuid(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("managed_capacity_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_accounts"),
        sa.UniqueConstraint("slug", name="uq_accounts_slug"),
    )
    op.create_index(
        "uq_accounts_system_singleton",
        "accounts",
        ["is_system"],
        unique=True,
        sqlite_where=sa.text("is_system"),
        postgresql_where=sa.text("is_system"),
    )

    op.create_table(
        "users",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("auth0_subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("auth0_subject", name="uq_users_auth0_subject"),
    )

    op.create_table(
        "account_memberships",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_account_memberships"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_account_memberships_account_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_account_memberships_user_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "account_id", "user_id", name="uq_account_memberships_account_id_user_id"
        ),
    )
    op.create_index("ix_account_memberships_user_id", "account_memberships", ["user_id"])

    op.create_table(
        "service_principals",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_service_principals"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_service_principals_account_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_service_principals_created_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("account_id", "id", name="uq_service_principals_account_id_id"),
        sa.UniqueConstraint("account_id", "name", name="uq_service_principals_account_id_name"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("principal_type", sa.String(16), nullable=False),
        sa.Column("principal_id", UUID, nullable=False),
        sa.Column("key_prefix", sa.String(96), nullable=False),
        sa.Column("secret_verifier", sa.String(128), nullable=False),
        sa.Column("scopes", JsonColumn, nullable=False),
        sa.Column("expires_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("last_used_at", TS, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_api_keys"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_api_keys_account_id", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_api_keys_created_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("key_prefix", name="uq_api_keys_key_prefix"),
        sa.CheckConstraint(
            "principal_type in ('user', 'service')", name="ck_api_keys_api_keys_principal_type"
        ),
    )
    op.create_index("ix_api_keys_account_id", "api_keys", ["account_id"])

    op.create_table(
        "deployments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("model_alias", sa.String(200), nullable=False),
        sa.Column("desired_spec", JsonColumn, nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by_principal_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_deployments"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_deployments_account_id", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("account_id", "id", name="uq_deployments_account_id_id"),
    )
    op.create_index(
        "uq_deployments_active_name",
        "deployments",
        ["account_id", "name"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "inference_stamps",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("orchestrator", sa.String(32), nullable=True),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("capabilities", JsonColumn, nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_inference_stamps"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_inference_stamps_account_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("account_id", "id", name="uq_inference_stamps_account_id_id"),
        sa.CheckConstraint(
            "mode in ('managed', 'byoi')", name="ck_inference_stamps_inference_stamps_mode"
        ),
    )
    op.create_index("ix_inference_stamps_account_id", "inference_stamps", ["account_id"])

    op.create_table(
        "stamp_enrollment_tokens",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("token_prefix", sa.String(96), nullable=False),
        sa.Column("token_verifier", sa.String(128), nullable=False),
        sa.Column("allowed_mode", sa.String(16), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("used_at", TS, nullable=True),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("created_by_user_id", UUID, nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_stamp_enrollment_tokens"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["accounts.id"],
            name="fk_stamp_enrollment_tokens_account_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_stamp_enrollment_tokens_created_by_user_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("token_prefix", name="uq_stamp_enrollment_tokens_token_prefix"),
        sa.CheckConstraint(
            "allowed_mode in ('managed', 'byoi')",
            name="ck_stamp_enrollment_tokens_stamp_enrollment_tokens_allowed_mode",
        ),
    )

    for table, fk_name, index_name in (
        (
            "stamp_credentials",
            "fk_stamp_credentials_account_id_stamp_id",
            "ix_stamp_credentials_stamp_id",
        ),
        (
            "telemetry_credentials",
            "fk_telemetry_credentials_account_id_stamp_id",
            "ix_telemetry_credentials_stamp_id",
        ),
    ):
        op.create_table(
            table,
            sa.Column("id", UUID, primary_key=True),
            sa.Column("account_id", UUID, nullable=False),
            sa.Column("stamp_id", UUID, nullable=False),
            sa.Column("credential_prefix", sa.String(96), nullable=False),
            sa.Column("credential_verifier", sa.String(128), nullable=False),
            sa.Column("scopes", JsonColumn, nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("expires_at", TS, nullable=True),
            sa.Column("last_used_at", TS, nullable=True),
            sa.Column("revoked_at", TS, nullable=True),
            sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["accounts.id"],
                name=f"fk_{table}_account_id",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["account_id", "stamp_id"],
                ["inference_stamps.account_id", "inference_stamps.id"],
                name=fk_name,
                ondelete="CASCADE",
            ),
            sa.UniqueConstraint("credential_prefix", name=f"uq_{table}_credential_prefix"),
        )
        op.create_index(index_name, table, ["stamp_id"])

    op.create_table(
        "deployment_placements",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("deployment_id", UUID, nullable=False),
        sa.Column("stamp_id", UUID, nullable=False),
        sa.Column("desired_generation", sa.Integer(), nullable=False),
        sa.Column("observed_generation", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_placements"),
        sa.ForeignKeyConstraint(
            ["account_id", "deployment_id"],
            ["deployments.account_id", "deployments.id"],
            name="fk_deployment_placements_account_id_deployment_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["stamp_id"],
            ["inference_stamps.id"],
            name="fk_deployment_placements_stamp_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "account_id",
            "deployment_id",
            "stamp_id",
            name="uq_deployment_placements_account_id_deployment_id_stamp_id",
        ),
    )
    op.create_index("ix_deployment_placements_stamp_id", "deployment_placements", ["stamp_id"])

    op.create_table(
        "deployment_status",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("deployment_id", UUID, nullable=False),
        sa.Column("stamp_id", UUID, nullable=False),
        sa.Column("observed_generation", sa.Integer(), nullable=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("ready_replicas", sa.Integer(), nullable=False),
        sa.Column("unavailable_replicas", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=True),
        sa.Column("conditions", JsonColumn, nullable=False),
        sa.Column("reported_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_deployment_status"),
        sa.ForeignKeyConstraint(
            ["account_id", "deployment_id", "stamp_id"],
            [
                "deployment_placements.account_id",
                "deployment_placements.deployment_id",
                "deployment_placements.stamp_id",
            ],
            name="fk_deployment_status_placement",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "account_id",
            "deployment_id",
            "stamp_id",
            name="uq_deployment_status_account_id_deployment_id_stamp_id",
        ),
    )

    op.create_table(
        "usage_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("deployment_id", UUID, nullable=False),
        sa.Column("stamp_id", UUID, nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("received_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("deduplication_key", sa.String(128), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_usage_events"),
        sa.ForeignKeyConstraint(
            ["account_id", "deployment_id", "stamp_id"],
            [
                "deployment_placements.account_id",
                "deployment_placements.deployment_id",
                "deployment_placements.stamp_id",
            ],
            name="fk_usage_events_placement",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("deduplication_key", name="uq_usage_events_deduplication_key"),
    )
    op.create_index(
        "ix_usage_events_account_id_occurred_at", "usage_events", ["account_id", "occurred_at"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("event_metadata", JsonColumn, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], name="fk_audit_events_account_id", ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_audit_events_account_id_created_at", "audit_events", ["account_id", "created_at"]
    )

    op.create_table(
        "outbox_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=True),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("payload", JsonColumn, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", TS, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_outbox_events"),
    )
    op.create_index("ix_outbox_events_processed_at", "outbox_events", ["processed_at"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("principal_id", UUID, nullable=False),
        sa.Column("operation", sa.String(128), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("response_reference", sa.String(200), nullable=True),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_keys"),
        sa.UniqueConstraint(
            "account_id",
            "principal_id",
            "operation",
            "key",
            name="uq_idempotency_keys_account_id_principal_id_operation_key",
        ),
    )


def downgrade() -> None:
    for table in (
        "idempotency_keys",
        "outbox_events",
        "audit_events",
        "usage_events",
        "deployment_status",
        "deployment_placements",
        "telemetry_credentials",
        "stamp_credentials",
        "stamp_enrollment_tokens",
        "inference_stamps",
        "deployments",
        "api_keys",
        "service_principals",
        "account_memberships",
        "users",
        "accounts",
    ):
        op.drop_table(table)
