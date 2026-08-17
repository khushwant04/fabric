"""Audiences, scopes, and role mappings.

Scopes are always derived server-side. Callers never choose their own scopes.
"""

from __future__ import annotations

AUDIENCE_CONTROL = "fabric-control"
AUDIENCE_INFERENCE = "fabric-inference"
SUPPORTED_AUDIENCES = frozenset({AUDIENCE_CONTROL, AUDIENCE_INFERENCE})

# Control-plane scopes.
ACCOUNTS_READ = "accounts:read"
MEMBERS_READ = "members:read"
MEMBERS_WRITE = "members:write"
API_KEYS_READ = "api-keys:read"
API_KEYS_WRITE = "api-keys:write"
DEPLOYMENTS_READ = "deployments:read"
DEPLOYMENTS_WRITE = "deployments:write"
STAMPS_READ = "stamps:read"
STAMPS_WRITE = "stamps:write"

# Granting managed capacity is Fabric's decision, not a customer's, so this scope is
# deliberately outside CONTROL_SCOPES: an account-scoped API key can never carry it,
# which is what stops an account from entitling itself.
CAPACITY_WRITE = "system:capacity:write"

CONTROL_SCOPES = frozenset(
    {
        ACCOUNTS_READ,
        MEMBERS_READ,
        MEMBERS_WRITE,
        API_KEYS_READ,
        API_KEYS_WRITE,
        DEPLOYMENTS_READ,
        DEPLOYMENTS_WRITE,
        STAMPS_READ,
        STAMPS_WRITE,
    }
)

# Inference scope used by the data plane.
INFERENCE_INVOKE = "inference:invoke"
INFERENCE_SCOPES = frozenset({INFERENCE_INVOKE})

# Cluster-agent scopes. The agent credential never receives telemetry write access.
STAMP_HEARTBEAT = "stamp:heartbeat"
STAMP_CAPABILITIES_WRITE = "stamp:capabilities:write"
STAMP_DESIRED_STATE_READ = "stamp:desired-state:read"
STAMP_STATUS_WRITE = "stamp:status:write"
AGENT_SCOPES = frozenset(
    {
        STAMP_HEARTBEAT,
        STAMP_CAPABILITIES_WRITE,
        STAMP_DESIRED_STATE_READ,
        STAMP_STATUS_WRITE,
    }
)

# Collector scope. Write-only and separate from the agent credential.
TELEMETRY_WRITE = "telemetry:write"
TELEMETRY_SCOPES = frozenset({TELEMETRY_WRITE})

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_DEVELOPER = "developer"
ROLE_VIEWER = "viewer"
SUPPORTED_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_DEVELOPER, ROLE_VIEWER)

_READ_ONLY = frozenset(
    {ACCOUNTS_READ, MEMBERS_READ, API_KEYS_READ, DEPLOYMENTS_READ, STAMPS_READ}
)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_OWNER: CONTROL_SCOPES,
    ROLE_ADMIN: CONTROL_SCOPES,
    ROLE_DEVELOPER: frozenset(
        {
            ACCOUNTS_READ,
            MEMBERS_READ,
            API_KEYS_READ,
            API_KEYS_WRITE,
            DEPLOYMENTS_READ,
            DEPLOYMENTS_WRITE,
            STAMPS_READ,
        }
    ),
    ROLE_VIEWER: _READ_ONLY,
}


def scopes_for_role(role: str) -> frozenset[str]:
    """Return the control scopes granted by an account role."""
    return ROLE_SCOPES.get(role, frozenset())


class UnsupportedScopeError(ValueError):
    """A requested scope is not a Fabric scope."""

    def __init__(self, scopes: list[str]) -> None:
        self.scopes = sorted(scopes)
        super().__init__(f"unsupported scopes: {', '.join(self.scopes)}")


class ScopeEscalationError(ValueError):
    """A requested control scope exceeds the creating principal's own scopes."""

    def __init__(self, scopes: list[str]) -> None:
        self.scopes = sorted(scopes)
        super().__init__(f"scopes exceed the creating principal: {', '.join(self.scopes)}")


def validate_requested_key_scopes(
    scopes: list[str], *, principal_scopes: frozenset[str]
) -> list[str]:
    """Return sorted, de-duplicated scopes that an API key may hold.

    Two rules apply, both server-side:

    * Unknown scopes are rejected.
    * A control scope may never exceed the creating principal's own scopes, so a
      key cannot be used to escalate beyond the role that created it.

    ``inference:invoke`` is delegable by any principal allowed to create keys
    because it authorizes the data plane only and is held by no control role.
    """
    allowed = CONTROL_SCOPES | INFERENCE_SCOPES
    requested = set(scopes)
    unknown = requested - allowed
    if unknown:
        raise UnsupportedScopeError(list(unknown))
    escalated = (requested & CONTROL_SCOPES) - principal_scopes
    if escalated:
        raise ScopeEscalationError(list(escalated))
    return sorted(requested)
