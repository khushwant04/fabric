"""What this gateway enforces on inference tokens, and where that came from.

The issuer and key source are the two values every request is checked against. They
arrive two ways: from this process's environment, set when the stamp was installed, and
from the control plane, which reports its own signing identity on every desired-state
pass. The control plane is authoritative — it is the thing that mints the tokens, so it
cannot disagree with itself, while a per-cluster Helm value can and does drift.

Adoption is deliberately narrow, because this is the one setting that can refuse all
traffic at once:

* both halves must be present. An issuer with no key source verifies nothing, and keys
  with no issuer accept anything that is merely signed, so a partial answer is refused
  rather than half-applied.
* absent means keep. A response carrying no verification is an older control plane or a
  stamp with nothing placed on it, not an instruction to stop checking.
* a rejected update is counted and surfaced, because silently ignoring configuration is
  how a fleet ends up enforcing something nobody chose.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from fabric_data_plane.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verification:
    """An issuer and the key source that verifies it."""

    jwt_issuer: str
    jwks_url: str

    @property
    def complete(self) -> bool:
        return bool(self.jwt_issuer and self.jwks_url)


def verification_from_payload(payload: dict[str, Any]) -> Verification | None:
    """Read the stamp-wide verification section, or None when it is absent.

    Parsed leniently on shape and strictly on content: a malformed section is treated as
    absent so a bad document cannot stop the gateway verifying, and the caller keeps what
    it already had.
    """
    section = payload.get("verification")
    if not isinstance(section, dict):
        return None
    issuer = str(section.get("jwt_issuer") or "").strip()
    jwks_url = str(section.get("jwks_url") or "").strip()
    candidate = Verification(jwt_issuer=issuer, jwks_url=jwks_url)
    return candidate if candidate.complete else None


class VerificationPolicy:
    """Decides the issuer and key source in force, and records why.

    Local values seed it so a gateway verifies from the moment it starts, before any
    desired-state pass has happened. Once the control plane reports its identity that
    value wins, which is what makes a locally edited issuer converge back rather than
    persist as drift.
    """

    def __init__(self, settings: Settings) -> None:
        self._local = Verification(
            jwt_issuer=settings.jwt_issuer, jwks_url=settings.jwks_url
        )
        self._adopted: Verification | None = None
        self._lock = threading.Lock()
        self._adoptions = 0
        self._rejected = 0
        self._last_rejected_reason: str | None = None
        self._corrected_drift = 0

    # -- what is enforced --------------------------------------------------

    @property
    def effective(self) -> Verification:
        with self._lock:
            return self._adopted or self._local

    @property
    def jwt_issuer(self) -> str:
        return self.effective.jwt_issuer

    @property
    def jwks_url(self) -> str:
        return self.effective.jwks_url

    # -- adoption ----------------------------------------------------------

    def apply(self, candidate: Verification | None) -> bool:
        """Adopt a reported contract, returning whether the key source changed.

        The return value exists because a changed key source invalidates the cached
        keys: they belong to the issuer that was being verified before.
        """
        if candidate is None:
            # Absent is not empty. Keeping what is in force is the only safe reading,
            # since the alternative is a gateway that stops checking issuers.
            return False

        if not candidate.complete:
            with self._lock:
                self._rejected += 1
                self._last_rejected_reason = "issuer and jwks_url must both be set"
            logger.warning(
                "refused an incomplete verification contract: issuer=%r jwks_url=%r",
                candidate.jwt_issuer,
                candidate.jwks_url,
            )
            return False

        with self._lock:
            current = self._adopted or self._local
            if candidate == current:
                # Record that it was confirmed even when nothing moved, so the first
                # adoption is distinguishable from never having heard from the control
                # plane at all.
                self._adopted = candidate
                return False

            drifted = current != candidate and self._adopted is None
            self._adopted = candidate
            self._adoptions += 1
            if drifted:
                self._corrected_drift += 1
            key_source_changed = current.jwks_url != candidate.jwks_url

        if drifted:
            # Logged at warning because the cluster was enforcing something the control
            # plane did not choose, which is worth noticing even though it is now fixed.
            logger.warning(
                "local verification differed from the control plane and was corrected: "
                "was issuer=%r jwks_url=%r, now issuer=%r jwks_url=%r",
                current.jwt_issuer,
                current.jwks_url,
                candidate.jwt_issuer,
                candidate.jwks_url,
            )
        else:
            logger.info(
                "adopted verification from the control plane: issuer=%r jwks_url=%r",
                candidate.jwt_issuer,
                candidate.jwks_url,
            )
        return key_source_changed

    # -- reporting ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Operational view: what is enforced, where it came from, what was refused."""
        with self._lock:
            adopted = self._adopted
            effective = adopted or self._local
            return {
                "issuer": effective.jwt_issuer,
                "jwks_url": effective.jwks_url,
                # "synced" means the control plane's own identity is in force. "local"
                # means this gateway has not been told yet and is running on the value
                # its install supplied, which is the state to watch for.
                "source": "synced" if adopted is not None else "local",
                "local_issuer": self._local.jwt_issuer,
                "local_jwks_url": self._local.jwks_url,
                "matches_local": effective == self._local,
                "adoptions": self._adoptions,
                "corrected_drift": self._corrected_drift,
                "rejected_updates": self._rejected,
                "last_rejected_reason": self._last_rejected_reason,
            }
