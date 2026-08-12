"""Control-plane ORM models.

Tenancy rule: account is the only customer hierarchy level. Every tenant-owned
table carries ``account_id``, and child rows use composite foreign keys so a
record cannot reference another account's resource. Placements intentionally
reference ``stamp_id`` independently because an authorized managed placement
crosses from a customer account to the protected Fabric system account.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, JsonColumn
from app.core.timeutil import utc_now

MODE_MANAGED = "managed"
MODE_BYOI = "byoi"
SUPPORTED_STAMP_MODES = (MODE_MANAGED, MODE_BYOI)

PRINCIPAL_USER = "user"
PRINCIPAL_SERVICE = "service"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, server_default=func.now()
    )


class Account(Base):
    """A customer or internal tenant. The only ownership boundary in MVP."""

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    managed_capacity_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        # Only one protected system account may exist.
        Index(
            "uq_accounts_system_singleton",
            "is_system",
            unique=True,
            sqlite_where=text("is_system"),
            postgresql_where=text("is_system"),
        ),
    )


class User(Base):
    """A global Auth0-authenticated identity. Not itself a tenant resource."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    auth0_subject: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )


class AccountMembership(Base):
    """Grants a user a role within exactly one account."""

    __tablename__ = "account_memberships"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("account_id", "user_id", name="uq_account_memberships_account_id_user_id"),
        Index("ix_account_memberships_user_id", "user_id"),
    )


class ServicePrincipal(Base):
    """A non-human account-owned identity that can own API keys."""

    __tablename__ = "service_principals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("account_id", "id", name="uq_service_principals_account_id_id"),
        UniqueConstraint("account_id", "name", name="uq_service_principals_account_id_name"),
    )


class ApiKey(Base):
    """An account-owned long-lived credential exchanged for short-lived JWTs."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(16), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    secret_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "principal_type in ('user', 'service')", name="api_keys_principal_type"
        ),
        Index("ix_api_keys_account_id", "account_id"),
    )


class Deployment(Base):
    """Customer-owned desired inference deployment intent."""

    __tablename__ = "deployments"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_alias: Mapped[str] = mapped_column(String(200), nullable=False)
    desired_spec: Mapped[dict[str, Any]] = mapped_column(JsonColumn, nullable=False, default=dict)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_by_principal_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("account_id", "id", name="uq_deployments_account_id_id"),
        Index(
            "uq_deployments_active_name",
            "account_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class InferenceStamp(Base):
    """A registered Kubernetes inference stamp.

    Managed stamps belong to the Fabric system account. BYOI stamps belong to the
    enrolling customer account. ``orchestrator`` records the distribution such as
    ``aks`` or ``k3s`` and is never an ownership mode.
    """

    __tablename__ = "inference_stamps"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    orchestrator: Mapped[str | None] = mapped_column(String(32))
    region: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    capabilities: Mapped[dict[str, Any]] = mapped_column(JsonColumn, nullable=False, default=dict)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_heartbeat_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("account_id", "id", name="uq_inference_stamps_account_id_id"),
        CheckConstraint("mode in ('managed', 'byoi')", name="inference_stamps_mode"),
        Index("ix_inference_stamps_account_id", "account_id"),
    )


class StampEnrollmentToken(Base):
    """A one-time account-bound token that registers a new stamp."""

    __tablename__ = "stamp_enrollment_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    token_prefix: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    token_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    allowed_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=MODE_BYOI)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "allowed_mode in ('managed', 'byoi')", name="stamp_enrollment_tokens_allowed_mode"
        ),
    )


class StampCredential(Base):
    """Agent credential: heartbeat, capabilities, desired-state reads, status writes."""

    __tablename__ = "stamp_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    stamp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    credential_prefix: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    credential_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "stamp_id"],
            ["inference_stamps.account_id", "inference_stamps.id"],
            ondelete="CASCADE",
            name="fk_stamp_credentials_account_id_stamp_id",
        ),
        Index("ix_stamp_credentials_stamp_id", "stamp_id"),
    )


class TelemetryCredential(Base):
    """Write-only collector credential, separate from the agent credential."""

    __tablename__ = "telemetry_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    stamp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    credential_prefix: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    credential_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JsonColumn, nullable=False, default=list)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "stamp_id"],
            ["inference_stamps.account_id", "inference_stamps.id"],
            ondelete="CASCADE",
            name="fk_telemetry_credentials_account_id_stamp_id",
        ),
        Index("ix_telemetry_credentials_stamp_id", "stamp_id"),
    )


class DeploymentPlacement(Base):
    """Assignment of a customer deployment to a stamp.

    The deployment reference is account-composite so it cannot cross accounts.
    ``stamp_id`` is a plain reference because managed placement targets a stamp
    owned by the Fabric system account; the placement service enforces the
    allowed ownership branch.
    """

    __tablename__ = "deployment_placements"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stamp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("inference_stamps.id", ondelete="RESTRICT"), nullable=False
    )
    desired_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_generation: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="assigned")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "deployment_id"],
            ["deployments.account_id", "deployments.id"],
            ondelete="CASCADE",
            name="fk_deployment_placements_account_id_deployment_id",
        ),
        UniqueConstraint(
            "account_id",
            "deployment_id",
            "stamp_id",
            name="uq_deployment_placements_account_id_deployment_id_stamp_id",
        ),
        Index("ix_deployment_placements_stamp_id", "stamp_id"),
    )


class DeploymentStatus(Base):
    """Latest reported status for one placement, anchored to that placement."""

    __tablename__ = "deployment_status"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stamp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    observed_generation: Mapped[int | None] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    ready_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unavailable_replicas: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    endpoint: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonColumn, nullable=False, default=list
    )
    reported_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "deployment_id", "stamp_id"],
            [
                "deployment_placements.account_id",
                "deployment_placements.deployment_id",
                "deployment_placements.stamp_id",
            ],
            ondelete="CASCADE",
            name="fk_deployment_status_placement",
        ),
        UniqueConstraint(
            "account_id",
            "deployment_id",
            "stamp_id",
            name="uq_deployment_status_account_id_deployment_id_stamp_id",
        ),
    )


class UsageEvent(Base):
    """Operational token accounting. Not billing-grade in MVP."""

    __tablename__ = "usage_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    deployment_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    stamp_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[dt.datetime] = _created_at()
    deduplication_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "deployment_id", "stamp_id"],
            [
                "deployment_placements.account_id",
                "deployment_placements.deployment_id",
                "deployment_placements.stamp_id",
            ],
            ondelete="CASCADE",
            name="fk_usage_events_placement",
        ),
        Index("ix_usage_events_account_id_occurred_at", "account_id", "occurred_at"),
    )


class AuditEvent(Base):
    """Durable record of security- and product-relevant actions."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL")
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JsonColumn, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (Index("ix_audit_events_account_id_created_at", "account_id", "created_at"),)


class OutboxEvent(Base):
    """Transactional outbox used to publish desired-state changes."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonColumn, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = _created_at()
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_outbox_events_processed_at", "processed_at"),)


class IdempotencyKey(Base):
    """Deduplicates retried write operations per principal."""

    __tablename__ = "idempotency_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    response_reference: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "principal_id",
            "operation",
            "key",
            name="uq_idempotency_keys_account_id_principal_id_operation_key",
        ),
    )
