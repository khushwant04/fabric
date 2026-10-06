"""Optional fixed-account policy for dedicated installations."""

from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.core.errors import Forbidden


def require_installation_account(account_id: uuid.UUID) -> None:
    fixed = get_settings().single_tenant_account_id
    if fixed is not None and account_id != fixed:
        raise Forbidden(
            "installation_account_required", "Account is unavailable in this installation"
        )
