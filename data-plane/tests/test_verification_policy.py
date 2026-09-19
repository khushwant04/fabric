"""What this gateway verifies against, and who gets to decide it.

The issuer and the key source are the one pair of settings that can refuse all traffic at
once, so these tests are written around the failure they exist to prevent: a stamp whose
locally installed issuer no longer matches the control plane that mints its tokens, which
rejects every request with a generic code and looks like a fleet of bad callers.

The control plane is authoritative because it is the thing that signs the tokens. What
follows pins down how narrow that authority is: it can correct drift, it cannot silently
switch verification off, and it cannot take a gateway down by being unreachable.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from fabric_data_plane.app import DataPlane, create_admin_app
from fabric_data_plane.errors import Unauthorized
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from fabric_data_plane.verification import (
    Verification,
    VerificationPolicy,
    verification_from_payload,
)
from tests.conftest import (
    ACCOUNT_A,
    ISSUER,
    JWKS_URL,
    UPSTREAM,
    ControlPlaneStub,
    SigningKey,
    UpstreamStub,
    make_settings,
)

#: A second control plane identity, standing in for the real case this guards: the stamp
#: was installed pointing at one issuer and the fleet now mints tokens under another.
OTHER_ISSUER = "https://control.fabric.example"
OTHER_JWKS_URL = "https://control.fabric.example/.well-known/jwks.json"

DEPLOYMENT = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


def make_policy(**overrides) -> VerificationPolicy:
    return VerificationPolicy(make_settings(**overrides))


def registry_with(verification: Verification | None) -> DeploymentRegistry:
    """A registry carrying one deployment and the stamp-wide contract under test."""
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT,
                account_id=ACCOUNT_A,
                model_alias="launch-model",
                upstream_url=UPSTREAM,
                upstream_model="internal-release-1",
            )
        ],
        verification=verification,
    )


class TwoPlaneStub:
    """Two control planes at different URLs, so a key source can actually move.

    The conftest stub asserts it is only ever called at one URL, which is the right
    assertion there and the wrong one here: the whole point of adopting a key source is
    that the next fetch goes somewhere else.
    """

    def __init__(self) -> None:
        self.local = SigningKey()
        self.remote = SigningKey()
        self.fetches: list[str] = []
        self.offline = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.fetches.append(url)
        if self.offline:
            raise httpx.ConnectError("control plane is unreachable", request=request)
        key = self.remote if url == OTHER_JWKS_URL else self.local
        return httpx.Response(200, json={"keys": [key.jwk]})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


# -- parsing ---------------------------------------------------------------


def test_a_document_without_a_verification_section_reports_nothing():
    # Distinct from reporting empty values: the caller must be able to tell "no
    # instruction" apart from "verify nothing", because it keeps what it has for one and
    # would have to refuse everything for the other.
    assert verification_from_payload({"deployments": []}) is None


def test_a_half_filled_section_is_read_as_absent():
    # An issuer with no key source verifies nothing, and keys with no issuer accept
    # anything merely signed. Neither half is useful alone, so the pair is refused.
    assert verification_from_payload({"verification": {"jwt_issuer": ISSUER}}) is None
    assert verification_from_payload({"verification": {"jwks_url": JWKS_URL}}) is None


def test_a_malformed_section_does_not_stop_the_gateway_verifying():
    # A bad document is an agent or rendering fault. Treating it as absent keeps the
    # gateway enforcing what it already had rather than failing open or crashing.
    assert verification_from_payload({"verification": "https://control"}) is None
    assert verification_from_payload({"verification": []}) is None


def test_a_complete_section_is_read_whole():
    parsed = verification_from_payload(
        {"verification": {"jwt_issuer": OTHER_ISSUER, "jwks_url": OTHER_JWKS_URL}}
    )
    assert parsed == Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)


# -- the policy ------------------------------------------------------------


def test_a_gateway_verifies_from_its_own_settings_before_it_hears_anything():
    # Readiness does not wait on a desired-state pass, so the installed values have to be
    # in force from the first request or a cold start would accept unverified traffic.
    policy = make_policy()
    assert policy.jwt_issuer == ISSUER
    assert policy.jwks_url == JWKS_URL
    assert policy.snapshot()["source"] == "local"


def test_being_told_nothing_leaves_what_is_in_force_untouched():
    policy = make_policy()
    assert policy.apply(None) is False
    assert policy.jwt_issuer == ISSUER
    # Still "local": silence is an older control plane or an unplaced stamp, not
    # confirmation, and an operator needs to see the difference.
    assert policy.snapshot()["source"] == "local"


def test_a_partial_contract_is_refused_and_counted():
    policy = make_policy()
    assert policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url="")) is False
    snapshot = policy.snapshot()
    assert snapshot["issuer"] == ISSUER
    assert snapshot["rejected_updates"] == 1
    # Surfaced rather than only logged: configuration that is being ignored has to be
    # visible, otherwise the fleet enforces something nobody chose and nobody notices.
    assert snapshot["last_rejected_reason"]


def test_a_contract_matching_the_install_is_recorded_as_confirmed():
    policy = make_policy()
    changed = policy.apply(Verification(jwt_issuer=ISSUER, jwks_url=JWKS_URL))
    # Nothing moved, so the key cache must not be disturbed.
    assert changed is False
    snapshot = policy.snapshot()
    # But it is now known to agree with the control plane, which is the state an operator
    # wants to confirm before trusting the stamp.
    assert snapshot["source"] == "synced"
    assert snapshot["matches_local"] is True
    assert snapshot["corrected_drift"] == 0


def test_a_locally_drifted_issuer_is_corrected_and_reported_as_drift():
    policy = make_policy()
    changed = policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL))
    # The key source moved, so cached keys belong to the wrong issuer.
    assert changed is True
    snapshot = policy.snapshot()
    assert snapshot["issuer"] == OTHER_ISSUER
    assert snapshot["jwks_url"] == OTHER_JWKS_URL
    assert snapshot["source"] == "synced"
    # Both retained: the enforced value alone would hide that this cluster was configured
    # by hand and disagreed with the control plane.
    assert snapshot["local_issuer"] == ISSUER
    assert snapshot["matches_local"] is False
    assert snapshot["corrected_drift"] == 1


def test_only_an_issuer_change_without_a_key_move_leaves_keys_alone():
    # Same signing keys, different issuer claim. Re-fetching would be pointless work on
    # the one path that can refuse traffic, so the cache is told nothing happened.
    policy = make_policy()
    assert policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url=JWKS_URL)) is False
    assert policy.jwt_issuer == OTHER_ISSUER


def test_the_control_plane_cannot_be_overridden_by_a_later_silence():
    policy = make_policy()
    policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL))
    # A pass that carries no section must not revert to the installed value, or every
    # older-document reload would reopen the drift this just closed.
    policy.apply(None)
    assert policy.jwt_issuer == OTHER_ISSUER


# -- the key cache ---------------------------------------------------------


def test_keys_are_fetched_from_the_source_the_policy_names():
    stub = TwoPlaneStub()
    settings = make_settings()
    policy = VerificationPolicy(settings)
    cache = KeyCache(settings, client=stub.client(), policy=policy)

    cache.refresh()
    assert stub.fetches == [JWKS_URL]

    policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL))
    # Read per fetch, not captured at construction: adoption is worthless if the cache
    # keeps calling the URL it was built with.
    assert cache.source == OTHER_JWKS_URL
    cache.refresh()
    assert stub.fetches[-1] == OTHER_JWKS_URL


def test_a_cache_without_a_policy_uses_its_configured_source():
    # Every existing caller constructs it this way, and must keep behaving identically.
    settings = make_settings()
    cache = KeyCache(settings, client=TwoPlaneStub().client())
    assert cache.source == JWKS_URL


def test_moving_the_source_replaces_the_keys_that_were_held():
    stub = TwoPlaneStub()
    settings = make_settings()
    policy = VerificationPolicy(settings)
    cache = KeyCache(settings, client=stub.client(), policy=policy)
    cache.refresh()
    assert cache.key_ids == [stub.local.kid]

    policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL))
    assert cache.invalidate_source() is True
    # The old keys were the previous issuer's; holding them would let tokens from an
    # issuer this gateway no longer recognises keep verifying.
    assert cache.key_ids == [stub.remote.kid]


def test_a_source_move_during_an_outage_keeps_serving_from_cache():
    stub = TwoPlaneStub()
    settings = make_settings()
    policy = VerificationPolicy(settings)
    cache = KeyCache(settings, client=stub.client(), policy=policy)
    cache.refresh()
    held = cache.key_ids

    stub.offline = True
    policy.apply(Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL))
    assert cache.invalidate_source() is False
    # AR-DP02. Dropping the keys first would turn an unreachable control plane into a
    # total outage of a data plane that was serving perfectly well; the keys are instead
    # left in place and stale, so the next lookup retries the new source.
    assert cache.key_ids == held
    assert cache.snapshot()["fetch_failures"] == 1


# -- the gateway -----------------------------------------------------------


def make_plane(
    stub: TwoPlaneStub,
    upstream: UpstreamStub,
    verification: Verification | None,
) -> DataPlane:
    settings = make_settings()
    policy = VerificationPolicy(settings)
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=stub.client(), policy=policy),
        registry=registry_with(verification),
        client=upstream.client(),
        policy=policy,
    )


def test_the_gateway_adopts_the_contract_the_agent_wrote(upstream: UpstreamStub):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    # Taken during the reconcile the constructor already runs, so a gateway is never
    # briefly enforcing the installed value after the document says otherwise.
    assert plane.verification.jwt_issuer == OTHER_ISSUER
    assert plane.keys.source == OTHER_JWKS_URL
    # And the move was acted on, not merely recorded.
    assert OTHER_JWKS_URL in stub.fetches


def test_the_gateway_keeps_its_own_settings_when_the_document_is_silent(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(stub, upstream, None)
    assert plane.verification.jwt_issuer == ISSUER
    assert plane.verification.snapshot()["source"] == "local"


def test_the_gateway_shares_one_policy_with_its_key_cache(upstream: UpstreamStub):
    # Two copies could adopt at different moments, and a gateway checking one issuer with
    # another issuer's keys refuses everything.
    stub = TwoPlaneStub()
    plane = make_plane(stub, upstream, None)
    assert plane.keys.policy is plane.verification


def test_a_later_reload_moves_what_the_gateway_enforces(upstream: UpstreamStub):
    stub = TwoPlaneStub()
    plane = make_plane(stub, upstream, None)
    assert plane.verification.jwt_issuer == ISSUER

    plane.registry = registry_with(
        Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    plane.reconcile_pools()
    # Adoption rides on the reload path that already runs, so the contract follows
    # placement without a second watcher on the same file.
    assert plane.verification.jwt_issuer == OTHER_ISSUER
    assert plane.keys.source == OTHER_JWKS_URL


def test_a_token_from_the_adopted_issuer_is_accepted_after_drift_is_corrected(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    # The end this all serves: the fleet's real token verifies on a stamp that was
    # installed pointing somewhere else.
    token = stub.remote.issue(account_id=ACCOUNT_A, issuer=OTHER_ISSUER)
    principal = plane.authenticate(f"Bearer {token}")
    assert principal.account_id == ACCOUNT_A


def test_a_token_from_the_stale_local_issuer_is_refused_after_adoption(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    # Correction has to cut both ways. If the superseded issuer still verified, the
    # control plane would only be adding an issuer, and drift would never actually close.
    token = stub.local.issue(account_id=ACCOUNT_A, issuer=ISSUER)
    with pytest.raises(Unauthorized):
        plane.authenticate(f"Bearer {token}")


def test_a_gateway_that_was_never_told_still_verifies_its_own_issuer(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(stub, upstream, None)
    token = stub.local.issue(account_id=ACCOUNT_A, issuer=ISSUER)
    assert plane.authenticate(f"Bearer {token}").account_id == ACCOUNT_A


# -- reporting -------------------------------------------------------------


async def test_the_admin_listener_reports_what_is_enforced_not_what_was_installed(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    transport = httpx.ASGITransport(app=create_admin_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as http:
        body = (await http.get("/admin/verification")).json()

    # Reading settings here would report a value the process is no longer applying, which
    # is precisely the confusion this endpoint exists to resolve.
    assert body["issuer"] == OTHER_ISSUER
    assert body["source"] == "synced"
    assert body["local_issuer"] == ISSUER
    assert body["matches_local"] is False
    # The constants a caller's token has to satisfy stay reported alongside, so diagnosing
    # a refusal needs one request rather than the source.
    assert body["audience"] == "fabric-inference"
    assert body["required_scope"] == "inference:invoke"


async def test_the_key_listener_reports_the_source_in_force(upstream: UpstreamStub):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    transport = httpx.ASGITransport(app=create_admin_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as http:
        body = (await http.get("/admin/keys")).json()
    assert body["source"] == OTHER_JWKS_URL


# -- the whole loop --------------------------------------------------------


def test_an_operator_cannot_switch_verification_off_by_editing_the_document(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(stub, upstream, None)
    # A hand-edited or truncated document, which is the shape a mistake takes: half the
    # section present. It must not be adopted, and it must not clear what is in force.
    plane.registry = registry_with(Verification(jwt_issuer="", jwks_url=OTHER_JWKS_URL))
    plane.reconcile_pools()
    assert plane.verification.jwt_issuer == ISSUER
    assert plane.verification.jwks_url == JWKS_URL


def test_adoption_is_idempotent_across_repeated_passes(upstream: UpstreamStub):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    fetches = len(stub.fetches)
    for _ in range(5):
        plane.reconcile_pools()
    # reconcile_pools runs on every reload and after every placement change, so a contract
    # that has not moved must cost nothing: re-fetching keys here would put the control
    # plane back on the hot path it was removed from.
    assert len(stub.fetches) == fetches
    assert plane.verification.snapshot()["adoptions"] == 1


async def test_inference_still_makes_no_control_plane_call(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    plane = make_plane(
        stub, upstream, Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    token = stub.remote.issue(account_id=ACCOUNT_A, issuer=OTHER_ISSUER)
    fetches = len(stub.fetches)
    for _ in range(3):
        plane.authenticate(f"Bearer {token}")
    # The reason verification is local at all. Adopting configuration from the control
    # plane must not have quietly reintroduced a dependency on it per request.
    assert len(stub.fetches) == fetches


def test_a_control_plane_outage_cannot_stop_a_placed_gateway_verifying(
    upstream: UpstreamStub,
):
    stub = TwoPlaneStub()
    settings = make_settings()
    policy = VerificationPolicy(settings)
    cache = KeyCache(settings, client=stub.client(), policy=policy)
    plane = DataPlane(
        settings=settings,
        keys=cache,
        registry=registry_with(None),
        client=upstream.client(),
        policy=policy,
    )
    token = stub.local.issue(account_id=ACCOUNT_A, issuer=ISSUER)
    assert plane.authenticate(f"Bearer {token}").account_id == ACCOUNT_A

    # The control plane disappears and the agent then writes a document moving the key
    # source. Nothing about that sequence may cost the gateway its ability to verify the
    # tokens it already holds keys for.
    stub.offline = True
    plane.registry = registry_with(
        Verification(jwt_issuer=OTHER_ISSUER, jwks_url=OTHER_JWKS_URL)
    )
    plane.reconcile_pools()
    # Preparation failed, so the complete old generation remains active. Publishing the
    # new issuer while retaining old keys would make both old and new tokens fail.
    assert plane.verification.jwt_issuer == ISSUER
    assert plane.verification.jwks_url == JWKS_URL
    assert plane.authenticate(f"Bearer {token}").account_id == ACCOUNT_A


def test_the_control_plane_stub_from_conftest_still_pairs_with_a_policy(
    control_plane: ControlPlaneStub,
):
    # Guards the fixture every other module uses: a policy seeded from the test settings
    # must resolve to the one URL that stub allows, or unrelated suites would fail on an
    # assertion about the control-plane URL rather than on their own subject.
    settings = make_settings()
    policy = VerificationPolicy(settings)
    cache = KeyCache(settings, client=control_plane.client(), policy=policy)
    assert cache.refresh() is True
    assert control_plane.fetches == 1
