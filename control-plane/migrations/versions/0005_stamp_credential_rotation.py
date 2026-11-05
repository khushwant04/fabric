"""Record requested and completed stamp credential rotations.

Revision ID: 0005_stamp_credential_rotation
Revises: 0004_account_cache_version
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_stamp_credential_rotation"
down_revision = "0004_account_cache_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inference_stamps",
        sa.Column("credential_rotation_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "inference_stamps",
        sa.Column("credentials_rotated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("inference_stamps", "credentials_rotated_at")
    op.drop_column("inference_stamps", "credential_rotation_requested_at")
