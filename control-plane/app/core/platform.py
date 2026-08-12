"""Supported platform surface.

Kubernetes distribution (``orchestrator``) is separate from stamp ownership mode.
Managed capacity may only be scheduled onto a distribution Fabric operates, so
the allowlist lives here rather than in the stamp payload contract, where a
customer-reported value is untrusted input.
"""

from __future__ import annotations

ORCHESTRATOR_AKS = "aks"
ORCHESTRATOR_K3S = "k3s"

#: Distributions Fabric supports for managed placement.
SUPPORTED_ORCHESTRATORS = frozenset({ORCHESTRATOR_AKS, ORCHESTRATOR_K3S})


def is_supported_orchestrator(value: str | None) -> bool:
    """Return ``True`` only for a distribution Fabric operates."""
    return value is not None and value.strip().lower() in SUPPORTED_ORCHESTRATORS
