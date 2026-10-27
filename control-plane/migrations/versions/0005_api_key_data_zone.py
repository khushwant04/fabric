"""Bind API keys to a data zone.

Revision ID: 0005_api_key_data_zone
Revises: 0004_account_cache_version
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_api_key_data_zone"
down_revision = "0004_account_cache_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default: every existing key is unbound, which is what it always was.
    op.add_column("api_keys", sa.Column("data_zone", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("api_keys", "data_zone")
