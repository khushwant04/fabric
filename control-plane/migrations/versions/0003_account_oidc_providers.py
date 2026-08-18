"""Per-account OIDC providers, so an account can bring its own identity provider.

One Auth0 tenant belonging to Fabric is a workable way for Fabric's own staff to log in and
a poor way to serve customers: it asks every organisation to keep its people in an identity
provider it does not control, outside the directory it already runs. An account should be
able to point at its own issuer and have its own people recognised.

The provider is a property of the account. Subjects from a customer's provider are stored
namespaced by account, because two customers' identity providers can and will issue the
same subject string, and the existing global uniqueness on subject would otherwise let
whichever account registered it first own that identity everywhere. Namespacing keeps that
constraint meaningful instead of relaxing it.

Revision ID: 0003_account_oidc_providers
Revises: 0002_row_level_security
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_account_oidc_providers"
down_revision = "0002_row_level_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_oidc_providers",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("jwks_uri", sa.String(500), nullable=False),
        sa.Column("audience", sa.String(500), nullable=False),
        # Which claim carries the stable identifier and which the address. Providers
        # disagree: "sub" is standard but some deployments prefer "oid" or "upn", and a
        # provider that cannot be configured is a provider that cannot be used.
        sa.Column("subject_claim", sa.String(100), nullable=False, server_default="sub"),
        sa.Column("email_claim", sa.String(100), nullable=False, server_default="email"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        # When set, a person the provider vouches for who has no membership is given this
        # role on first sign-in. Off unless chosen: being recognised by a directory proves
        # who somebody is, not that they were granted access, and an account may well want
        # to admit people one at a time.
        sa.Column("auto_provision_role", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(as_uuid=True), nullable=True),
    )
    # One provider per account. Several would make "which identity provider does this
    # token belong to" a guess, and guessing which issuer to trust is how a token from one
    # provider gets accepted as another's.
    op.create_index(
        "ix_account_oidc_providers_account",
        "account_oidc_providers",
        ["account_id"],
        unique=True,
    )
    # Resolving a token to an account by its issuer has to be unambiguous.
    op.create_index(
        "ix_account_oidc_providers_issuer",
        "account_oidc_providers",
        ["issuer"],
        unique=True,
    )

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # The same isolation every other account-owned table has: forced, so that even a
    # table owner reads only its own rows, and keyed on the account column.
    op.execute("ALTER TABLE account_oidc_providers ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE account_oidc_providers FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY account_isolation ON account_oidc_providers
        USING (
            account_id = NULLIF(current_setting('fabric.account_id', true), '')::uuid
            OR current_setting('fabric.system', true) = 'on'
        )
        WITH CHECK (
            account_id = NULLIF(current_setting('fabric.account_id', true), '')::uuid
            OR current_setting('fabric.system', true) = 'on'
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS account_isolation ON account_oidc_providers")
    op.drop_index("ix_account_oidc_providers_issuer", table_name="account_oidc_providers")
    op.drop_index("ix_account_oidc_providers_account", table_name="account_oidc_providers")
    op.drop_table("account_oidc_providers")
