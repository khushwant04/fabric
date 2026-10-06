"""Add durable response-cache generations to accounts.

Revision ID: 0004_account_cache_version
Revises: 0003_account_oidc_providers
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_account_cache_version"
down_revision = "0003_account_oidc_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("cache_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("accounts", "cache_version")
