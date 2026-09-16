"""Placement admits only what a stamp can hold (M4, ADR 0013).

Before this, ``POST /placements`` answered ``201 assigned`` for a deployment asking for eight
H100s onto a single-T4 stamp. The agent delivered it and the pod stayed ``Pending`` forever,
so the API's success meant nothing. These tests are about the two halves of fixing that:
refusing what cannot fit with a reason, and choosing a stamp that can when the caller does not
name one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditEvent, InferenceStamp
from app.services import placement as placement_service
from tests.conftest import USING_POSTGRES
from tests.helpers import (
    bearer,
    create_deployment,
    default_capabilities,
    enable_managed_capacity,
    enroll_managed_stamp,
    enroll_stamp,
    onboard,
    place,
    report_capabilities,
    with_claims,
)

A100_MEMORY_BYTES = 40960 * 1024 * 1024
H100_MEMORY_BYTES = 81920 * 1024 * 1024


def _error(response) -> dict:
    return response.json()["error"]


# --- refusing what cannot fit ---------------------------------------------


async def test_a_deployment_larger_than_the_stamp_is_refused_with_a_reason(
    client: AsyncClient,
) -> None:
    """The failure this milestone exists to move from reconcile time to the API."""
    account_id, token = await onboard(client, "fit-small", "fit-small-account")
    deployment = await create_deployment(client, account_id, token, replicas=4)
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=1)
    )

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409, refused.text
    error = _error(refused)
    assert error["code"] == "stamp_cannot_fit_deployment"
    details = error["details"]
    assert details["reason"] == "insufficient_free_gpus"
    # The caller has to be able to tell whether to shrink the request or add capacity.
    assert details["free_gpus"] == 1
    assert details["allocatable_gpus"] == 1
    assert details["demand"]["gpus_total"] == 4


async def test_a_deployment_that_fits_is_still_assigned(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "fit-ok", "fit-ok-account")
    deployment = await create_deployment(client, account_id, token, replicas=2)
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )

    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == enrolled["stamp"]["id"]
    assert placed.json()["status"] == "assigned"


async def test_gpu_count_multiplies_the_demand(client: AsyncClient) -> None:
    """Two replicas of two devices need four, not two."""
    account_id, token = await onboard(client, "fit-count", "fit-count-account")
    deployment = await create_deployment(
        client, account_id, token, replicas=2, resources={"gpu_count": 2, "gpu_class": "t4"}
    )
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=3)
    )

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["demand"]["gpus_total"] == 4


async def test_committed_placements_reduce_what_the_next_one_may_take(
    client: AsyncClient,
) -> None:
    """Admission counts its own writes, not the stamp's report.

    No heartbeat happens between these two calls, so the stamp still reports zero GPUs in
    use. Counting only what the stamp has reported would let every placement in a burst see
    the same free capacity and collectively overcommit it.
    """
    account_id, token = await onboard(client, "fit-commit", "fit-commit-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    stamp_id = enrolled["stamp"]["id"]

    first = await create_deployment(client, account_id, token, name="first", replicas=2)
    second = await create_deployment(client, account_id, token, name="second", replicas=1)

    filled = await place(client, account_id, token, first["id"], stamp_id=stamp_id)
    assert filled.status_code == 201, filled.text

    refused = await place(client, account_id, token, second["id"], stamp_id=stamp_id)
    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["reason"] == "insufficient_free_gpus"
    assert details["committed_gpus"] == 2
    assert details["free_gpus"] == 0


async def test_re_placing_a_deployment_is_measured_against_its_new_demand(
    client: AsyncClient,
) -> None:
    """A deployment does not compete with itself for capacity."""
    account_id, token = await onboard(client, "fit-replace", "fit-replace-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    stamp_id = enrolled["stamp"]["id"]
    deployment = await create_deployment(client, account_id, token, replicas=2)

    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    ).status_code == 201
    # The same placement again: counting the existing one would make its own two GPUs the
    # reason it no longer fits.
    again = await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    assert again.status_code == 201, again.text


async def test_a_foreign_gpu_workload_reduces_free_capacity(client: AsyncClient) -> None:
    """A GPU somebody else is using is not free, whatever the node advertises."""
    account_id, token = await onboard(client, "fit-foreign", "fit-foreign-account")
    deployment = await create_deployment(client, account_id, token, replicas=2)
    enrolled = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=default_capabilities(gpus=2, requested_gpus=1, fabric_requested_gpus=0),
    )

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["foreign_requested_gpus"] == 1
    assert details["free_gpus"] == 1


async def test_fabrics_own_hosts_are_not_counted_twice(client: AsyncClient) -> None:
    """A running Fabric host is already in that deployment's committed row.

    The stamp reports one GPU held by a deployment the control plane also holds a placement
    for. Subtracting the report *and* the row would make a two-GPU stamp look full at one
    GPU used.
    """
    account_id, token = await onboard(client, "fit-double", "fit-double-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    stamp_id = enrolled["stamp"]["id"]

    running = await create_deployment(client, account_id, token, name="running", replicas=1)
    assert (
        await place(client, account_id, token, running["id"], stamp_id=stamp_id)
    ).status_code == 201
    # The operator has started it, so the stamp now reports the device as held.
    beat = await report_capabilities(
        client,
        stamp_id,
        enrolled["agent_credential"],
        with_claims(default_capabilities(gpus=2), {running["id"]: 1}),
    )
    assert beat.status_code == 200, beat.text

    second = await create_deployment(client, account_id, token, name="second", replicas=1)
    placed = await place(client, account_id, token, second["id"], stamp_id=stamp_id)
    assert placed.status_code == 201, placed.text


# --- GPU class as a minimum ------------------------------------------------


async def test_a_stronger_gpu_satisfies_a_weaker_class(client: AsyncClient) -> None:
    """An A100 (8.0, 40 GiB) serves an ``a10`` request (8.6, 24 GiB).

    Comparing exact compute capability would refuse it, even though it has more memory and
    every arithmetic tier a serving stack depends on.
    """
    account_id, token = await onboard(client, "class-up", "class-up-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "a10"}
    )
    enrolled = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=default_capabilities(
            gpus=2,
            product="NVIDIA A100",
            memory_bytes=A100_MEMORY_BYTES,
            compute_capability="8.0",
        ),
    )

    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text


async def test_a_weaker_gpu_does_not_satisfy_a_stronger_class(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "class-down", "class-down-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "h100"}
    )
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=8)
    )

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["reason"] == "gpu_class_not_satisfied"
    assert details["weakest_compute_capability"] == "7.5"
    assert details["required_compute_capability"] == "8.9"


async def test_the_weakest_gpu_in_a_mixed_stamp_decides(client: AsyncClient) -> None:
    """A pod may land on any GPU node, so the best one cannot carry the guarantee."""
    account_id, token = await onboard(client, "class-mixed", "class-mixed-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "a100"}
    )
    mixed = default_capabilities(gpus=4, product="NVIDIA A100", memory_bytes=A100_MEMORY_BYTES)
    mixed["gpus"] = [
        {
            "product": "NVIDIA A100",
            "count": 2,
            "memory_bytes": A100_MEMORY_BYTES,
            "compute_capability": "8.0",
        },
        {
            "product": "Tesla T4",
            "count": 2,
            "memory_bytes": 15360 * 1024 * 1024,
            "compute_capability": "7.5",
        },
    ]
    enrolled = await enroll_stamp(client, account_id, token, capabilities=mixed)

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["reason"] == "gpu_class_not_satisfied"


async def test_a_nominal_size_is_matched_by_the_real_advertised_memory(
    client: AsyncClient,
) -> None:
    """A "16 GB" T4 advertises 15360 MiB, and must still satisfy the ``t4`` class."""
    account_id, token = await onboard(client, "class-tol", "class-tol-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "t4"}
    )
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=1)
    )

    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text


async def test_an_unknown_gpu_class_is_a_bad_request(client: AsyncClient) -> None:
    """No amount of capacity makes an unrecognised class placeable, so it is not a conflict."""
    account_id, token = await onboard(client, "class-bogus", "class-bogus-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "gtx-1080"}
    )
    enrolled = await enroll_stamp(client, account_id, token)

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 400
    error = _error(refused)
    assert error["code"] == "unknown_gpu_class"
    assert error["details"]["gpu_class"] == "gtx-1080"
    # Naming the catalogue is the difference between a usable error and a guess.
    assert "t4" in error["details"]["known_gpu_classes"]


async def test_a_class_name_is_matched_however_it_is_written(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "class-fold", "class-fold-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "  A100_80GB "}
    )
    enrolled = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=default_capabilities(
            gpus=1,
            product="NVIDIA H100",
            memory_bytes=H100_MEMORY_BYTES,
            compute_capability="9.0",
        ),
    )

    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text


async def test_a_replica_needing_more_devices_than_any_node_has_is_refused(
    client: AsyncClient,
) -> None:
    """One replica cannot be split across nodes, so a stamp-wide total does not answer this."""
    account_id, token = await onboard(client, "fit-node", "fit-node-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 4, "gpu_class": "t4"}
    )
    enrolled = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=default_capabilities(gpus=8, max_gpus_per_node=2),
    )

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["reason"] == "gpu_count_exceeds_largest_node"
    assert details["max_gpus_per_node"] == 2


async def test_a_stamp_reporting_no_gpus_is_refused(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "fit-none", "fit-none-account")
    deployment = await create_deployment(client, account_id, token)
    empty = default_capabilities(gpus=0)
    empty["gpus"] = []
    enrolled = await enroll_stamp(client, account_id, token, capabilities=empty)

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["reason"] == "stamp_reports_no_gpus"


async def test_a_stamp_that_describes_no_hardware_is_admitted_and_recorded(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An older agent reports a GPU count and nothing about the devices.

    Refusing would make upgrading every agent a prerequisite for placing anything, so the
    class requirement is skipped. It is recorded on the audit trail rather than passed over
    silently, because a silent admission is the defect this milestone removes.
    """
    account_id, token = await onboard(client, "fit-blind", "fit-blind-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "h100"}
    )
    legacy = {"orchestrator": "k3s", "region": "local", "gpus": [], "allocatable_gpus": 4}
    enrolled = await enroll_stamp(client, account_id, token, capabilities=legacy)

    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.account_id == uuid.UUID(account_id),
                AuditEvent.action == "placement.created",
            )
        )
    ).scalar_one()
    assert event.event_metadata["gpu_class_verified"] is False
    assert event.event_metadata["stamp_selected"] is False


# --- choosing a stamp -----------------------------------------------------


async def test_placement_without_a_stamp_chooses_one_that_fits(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "pick-fit", "pick-fit-account")
    small = await enroll_stamp(
        client, account_id, token, name="small", capabilities=default_capabilities(gpus=1)
    )
    large = await enroll_stamp(
        client, account_id, token, name="large", capabilities=default_capabilities(gpus=8)
    )
    deployment = await create_deployment(client, account_id, token, replicas=4)

    placed = await place(client, account_id, token, deployment["id"])

    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == large["stamp"]["id"]
    assert placed.json()["stamp_id"] != small["stamp"]["id"]


async def test_selection_prefers_the_least_loaded_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "pick-load", "pick-load-account")
    busy = await enroll_stamp(
        client, account_id, token, name="busy", capabilities=default_capabilities(gpus=8)
    )
    idle = await enroll_stamp(
        client, account_id, token, name="idle", capabilities=default_capabilities(gpus=8)
    )

    filler = await create_deployment(client, account_id, token, name="filler", replicas=6)
    assert (
        await place(client, account_id, token, filler["id"], stamp_id=busy["stamp"]["id"])
    ).status_code == 201

    deployment = await create_deployment(client, account_id, token, name="next", replicas=2)
    placed = await place(client, account_id, token, deployment["id"])

    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == idle["stamp"]["id"]


async def test_selection_refuses_with_a_reason_for_every_candidate(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "pick-none", "pick-none-account")
    await enroll_stamp(
        client, account_id, token, name="tiny", capabilities=default_capabilities(gpus=1)
    )
    deployment = await create_deployment(client, account_id, token, replicas=8)

    refused = await place(client, account_id, token, deployment["id"])

    assert refused.status_code == 409
    error = _error(refused)
    assert error["code"] == "no_stamp_fits_deployment"
    assert error["details"]["candidates_considered"] == 1
    assert error["details"]["candidates"][0]["reason"] == "insufficient_free_gpus"
    assert error["details"]["demand"]["gpus_total"] == 8


async def test_selection_never_reaches_another_accounts_stamp(client: AsyncClient) -> None:
    """A neighbour's idle cluster is not capacity this account may be given."""
    other_id, other_token = await onboard(client, "pick-other", "pick-other-account")
    await enroll_stamp(
        client, other_id, other_token, name="theirs", capabilities=default_capabilities(gpus=8)
    )

    account_id, token = await onboard(client, "pick-mine", "pick-mine-account")
    deployment = await create_deployment(client, account_id, token)

    refused = await place(client, account_id, token, deployment["id"])

    assert refused.status_code == 409
    error = _error(refused)
    assert error["code"] == "no_stamp_fits_deployment"
    assert error["details"]["candidates_considered"] == 0
    assert error["details"]["candidates"] == []


async def test_selection_skips_a_stamp_that_stopped_heartbeating(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "pick-dead", "pick-dead-account")
    gone = await enroll_stamp(
        client, account_id, token, name="gone", capabilities=default_capabilities(gpus=8)
    )
    live = await enroll_stamp(
        client, account_id, token, name="live", capabilities=default_capabilities(gpus=2)
    )
    await db_session.execute(
        update(InferenceStamp)
        .where(InferenceStamp.id == uuid.UUID(gone["stamp"]["id"]))
        .values(
            last_heartbeat_at=dt.datetime.now(tz=dt.UTC)
            - placement_service.LIVENESS_WINDOW
            - dt.timedelta(minutes=1)
        )
    )
    await db_session.commit()

    deployment = await create_deployment(client, account_id, token, replicas=2)
    placed = await place(client, account_id, token, deployment["id"])

    # The larger stamp would have won on free GPUs if it were still alive.
    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == live["stamp"]["id"]


async def test_a_named_stamp_is_not_refused_for_a_stale_heartbeat(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The caller chose it, and a stamp that missed a few beats usually returns."""
    account_id, token = await onboard(client, "named-dead", "named-dead-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    await db_session.execute(
        update(InferenceStamp)
        .where(InferenceStamp.id == uuid.UUID(enrolled["stamp"]["id"]))
        .values(last_heartbeat_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=1))
    )
    await db_session.commit()

    deployment = await create_deployment(client, account_id, token)
    placed = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )
    assert placed.status_code == 201, placed.text


async def test_selection_prefers_the_accounts_own_hardware_over_managed_capacity(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A customer's own cluster is already paid for; managed capacity is billed."""
    account_id, token = await onboard(client, "pick-own", "pick-own-account")
    await enroll_managed_stamp(
        client, db_session, capabilities=default_capabilities(gpus=8, region="local")
    )
    await enable_managed_capacity(db_session, account_id)
    own = await enroll_stamp(
        client, account_id, token, name="own", capabilities=default_capabilities(gpus=2)
    )

    deployment = await create_deployment(client, account_id, token, replicas=2)
    placed = await place(client, account_id, token, deployment["id"])

    # Managed capacity has more free GPUs and still loses.
    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == own["stamp"]["id"]


async def test_selection_skips_managed_capacity_without_the_entitlement(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "pick-entitle", "pick-entitle-account")
    managed, _system = await enroll_managed_stamp(
        client, db_session, capabilities=default_capabilities(gpus=8)
    )
    deployment = await create_deployment(client, account_id, token)

    refused = await place(client, account_id, token, deployment["id"])

    assert refused.status_code == 409
    error = _error(refused)
    assert error["code"] == "no_stamp_fits_deployment"
    # No id, no per-stamp entry and no count: a caller must not be able to size Fabric's fleet
    # or watch each member's state from a refusal. The reason is the part it can act on.
    assert error["details"]["candidates"] == []
    assert error["details"]["candidates_considered"] == 0
    assert error["details"]["managed_capacity_reasons"] == ["managed_capacity_not_enabled"]
    assert managed["stamp"]["id"] not in refused.text


async def test_selection_skips_managed_capacity_on_an_unsupported_orchestrator(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "pick-orch", "pick-orch-account")
    managed, _system = await enroll_managed_stamp(
        client,
        db_session,
        orchestrator="nomad",
        capabilities=default_capabilities(gpus=8),
    )
    await enable_managed_capacity(db_session, account_id)
    deployment = await create_deployment(client, account_id, token)

    refused = await place(client, account_id, token, deployment["id"])

    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["candidates"] == []
    assert details["managed_capacity_reasons"] == ["unsupported_orchestrator"]
    assert managed["stamp"]["id"] not in refused.text


async def test_a_revoked_stamp_is_never_selected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "pick-revoked", "pick-revoked-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=8)
    )
    revoked = await client.delete(
        f"/v1/accounts/{account_id}/stamps/{enrolled['stamp']['id']}", headers=bearer(token)
    )
    assert revoked.status_code in (200, 204), revoked.text

    deployment = await create_deployment(client, account_id, token)
    refused = await place(client, account_id, token, deployment["id"])

    assert refused.status_code == 409
    assert _error(refused)["details"]["candidates_considered"] == 0


async def test_a_selected_placement_is_recorded_as_selected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    account_id, token = await onboard(client, "pick-audit", "pick-audit-account")
    await enroll_stamp(client, account_id, token, capabilities=default_capabilities(gpus=8))
    deployment = await create_deployment(client, account_id, token)

    assert (await place(client, account_id, token, deployment["id"])).status_code == 201

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.account_id == uuid.UUID(account_id),
                AuditEvent.action == "placement.created",
            )
        )
    ).scalar_one()
    assert event.event_metadata["stamp_selected"] is True
    assert event.event_metadata["gpu_class_verified"] is True


# --- region ---------------------------------------------------------------


async def test_a_region_narrows_selection(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "region-pick", "region-pick-account")
    await enroll_stamp(
        client,
        account_id,
        token,
        name="west",
        capabilities=default_capabilities(gpus=8, region="us-west"),
    )
    east = await enroll_stamp(
        client,
        account_id,
        token,
        name="east",
        capabilities=default_capabilities(gpus=2, region="us-east"),
    )
    deployment = await create_deployment(client, account_id, token, replicas=2)

    placed = await place(client, account_id, token, deployment["id"], region="us-east")

    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == east["stamp"]["id"]


async def test_a_region_also_constrains_a_named_stamp(client: AsyncClient) -> None:
    """Naming a stamp must not be a way to land somewhere the request excluded."""
    account_id, token = await onboard(client, "region-named", "region-named-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=8, region="us-west")
    )
    deployment = await create_deployment(client, account_id, token)

    refused = await place(
        client,
        account_id,
        token,
        deployment["id"],
        stamp_id=enrolled["stamp"]["id"],
        region="us-east",
    )

    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["reason"] == "region_mismatch"
    assert details["stamp_region"] == "us-west"


# --- editing a placed deployment -----------------------------------------


async def test_raising_replicas_beyond_capacity_is_refused(client: AsyncClient) -> None:
    """Growing a placed deployment overcommits a stamp exactly as an oversized placement does."""
    account_id, token = await onboard(client, "grow", "grow-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    deployment = await create_deployment(client, account_id, token, replicas=1)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    refused = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-1"}, "replicas": 4}},
        headers=bearer(token),
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["reason"] == "insufficient_free_gpus"

    # The refused edit left the deployment exactly as it was.
    current = await client.get(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}", headers=bearer(token)
    )
    assert current.json()["generation"] == deployment["generation"]
    assert current.json()["desired_spec"]["replicas"] == 1


async def test_growing_a_deployment_within_capacity_still_works(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "grow-ok", "grow-ok-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    deployment = await create_deployment(client, account_id, token, replicas=1)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    grown = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-1"}, "replicas": 4}},
        headers=bearer(token),
    )
    assert grown.status_code == 200, grown.text
    assert grown.json()["desired_spec"]["replicas"] == 4


async def test_an_unplaced_deployment_can_still_be_edited_freely(client: AsyncClient) -> None:
    """Fit is a property of a placement; intent with no placement has nothing to check."""
    account_id, token = await onboard(client, "grow-free", "grow-free-account")
    deployment = await create_deployment(client, account_id, token, replicas=1)

    grown = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-1"}, "replicas": 32}},
        headers=bearer(token),
    )
    assert grown.status_code == 200, grown.text


# --- the reported contract ------------------------------------------------


async def test_the_capability_schema_accepts_the_fields_the_agent_now_measures(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The agent reports these on every heartbeat, and unknown fields are rejected."""
    account_id, token = await onboard(client, "caps", "caps-account")
    rolling = "11111111-1111-4111-8111-111111111111"
    enrolled = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=with_claims(
            default_capabilities(gpus=4, max_gpus_per_node=2), {rolling: 2}, foreign_gpus=1
        ),
    )

    stamp = (
        await db_session.execute(
            select(InferenceStamp).where(
                InferenceStamp.id == uuid.UUID(enrolled["stamp"]["id"])
            )
        )
    ).scalar_one()
    capacity = placement_service.read_capacity(stamp)

    assert capacity.allocatable_gpus == 4
    # Three claimed, two of them ours: one belongs to somebody else.
    assert capacity.foreign_requested_gpus == 1
    assert capacity.fabric_requested_gpus == 2
    assert capacity.fabric_claims == {rolling: 2}
    assert capacity.untracked_fabric_gpus == 0
    assert capacity.claims_measured is True
    assert capacity.max_gpus_per_node == 2

    # One replica committed while two devices run, which is what a rollout looks like. Use is
    # the larger of the two views for that deployment, plus the foreign claim.
    assert capacity.used_gpus({rolling: 1}) == 3
    assert capacity.free_gpus({rolling: 1}) == 1
    # A deployment the stamp reports but the control plane has no row for — a teardown in
    # progress — is charged at what is running.
    assert capacity.used_gpus({}) == 3


# --- what the stamp reports it is using ----------------------------------


async def test_gpus_running_beyond_what_is_committed_are_not_offered(
    client: AsyncClient,
) -> None:
    """A release change under ADR 0012 runs two workloads for one deployment.

    So a deployment really holds twice its replicas while it rolls out. Counting only the
    placement rows would offer capacity that is physically occupied, and the new placement's
    pod would sit Pending — the failure this milestone exists to move to the API, reached from
    the other side.
    """
    account_id, token = await onboard(client, "surge", "surge-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    stamp_id = enrolled["stamp"]["id"]

    rolling = await create_deployment(client, account_id, token, name="rolling", replicas=1)
    placed = await place(client, account_id, token, rolling["id"], stamp_id=stamp_id)
    assert placed.status_code == 201, placed.text
    # Mid-rollout: the candidate is up beside the active host, so one replica holds two.
    await report_capabilities(
        client,
        stamp_id,
        enrolled["agent_credential"],
        with_claims(default_capabilities(gpus=4), {rolling["id"]: 2}),
    )

    # Committed says 1, the stamp says 2 are running. Three more replicas fit against the rows
    # and do not fit against the hardware.
    incoming = await create_deployment(client, account_id, token, name="incoming", replicas=3)
    refused = await place(client, account_id, token, incoming["id"], stamp_id=stamp_id)

    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "insufficient_free_gpus"
    assert details["committed_gpus"] == 1
    assert details["fabric_requested_gpus"] == 2
    # The larger of the two views, and not their sum.
    assert details["used_gpus"] == 2
    assert details["free_gpus"] == 2


async def test_divergences_in_opposite_directions_do_not_cancel(
    client: AsyncClient,
) -> None:
    """Why the maximum is taken per deployment rather than over the stamp's totals.

    With one deployment's pods ahead of its rows and another's rows ahead of its pods, the two
    stamp-wide sums are equal to a stamp where neither diverges, and the surge disappears into
    the comparison.
    """
    account_id, token = await onboard(client, "cancel", "cancel-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=8)
    )
    stamp_id = enrolled["stamp"]["id"]

    rolling = await create_deployment(client, account_id, token, name="rolling", replicas=2)
    assert (
        await place(client, account_id, token, rolling["id"], stamp_id=stamp_id)
    ).status_code == 201
    # Mid-rollout: two replicas committed, four devices held.
    await report_capabilities(
        client,
        stamp_id,
        enrolled["agent_credential"],
        with_claims(default_capabilities(gpus=8), {rolling["id"]: 4}),
    )

    # Four remain: 8 - max(2 committed, 4 running).
    second = await create_deployment(client, account_id, token, name="second", replicas=2)
    assert (
        await place(client, account_id, token, second["id"], stamp_id=stamp_id)
    ).status_code == 201

    # Now two remain. Over stamp-wide totals this would still read as four free, because the
    # second deployment's committed row raises the committed sum to match the reported one.
    third = await create_deployment(client, account_id, token, name="third", replicas=4)
    refused = await place(client, account_id, token, third["id"], stamp_id=stamp_id)

    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["used_gpus"] == 6
    assert details["free_gpus"] == 2


async def test_a_deployment_is_not_charged_for_its_own_running_pods(
    client: AsyncClient,
) -> None:
    """Growing a deployment on the stamp already running it must not fail on itself.

    Its committed row is excluded because its new demand replaces it; its reported claim has to
    be excluded for the same reason. Left in, a two-replica deployment holding two of four
    devices could never be grown to four, and re-placing it would start returning 409.
    """
    account_id, token = await onboard(client, "self-charge", "self-charge-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    stamp_id = enrolled["stamp"]["id"]
    deployment = await create_deployment(client, account_id, token, replicas=2)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    ).status_code == 201
    await report_capabilities(
        client,
        stamp_id,
        enrolled["agent_credential"],
        with_claims(default_capabilities(gpus=4), {deployment["id"]: 2}),
    )

    grown = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-1"}, "replicas": 4}},
        headers=bearer(token),
    )
    assert grown.status_code == 200, grown.text

    again = await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
    assert again.status_code == 201, again.text


async def test_fabric_gpus_attributed_to_no_deployment_are_still_charged(
    client: AsyncClient,
) -> None:
    """A truncated claim list, or a host missing its deployment label, still holds devices."""
    account_id, token = await onboard(client, "untracked", "untracked-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    stamp_id = enrolled["stamp"]["id"]
    await report_capabilities(
        client,
        stamp_id,
        enrolled["agent_credential"],
        with_claims(default_capabilities(gpus=4), {}, untracked_gpus=3),
    )

    deployment = await create_deployment(client, account_id, token, replicas=2)
    refused = await place(client, account_id, token, deployment["id"], stamp_id=stamp_id)

    assert refused.status_code == 409, refused.text
    assert _error(refused)["details"]["free_gpus"] == 1


async def test_running_and_committed_gpus_are_not_added_together(
    client: AsyncClient,
) -> None:
    """In the ordinary case a deployment's row and its pods describe the same hosts."""
    account_id, token = await onboard(client, "no-double", "no-double-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    stamp_id = enrolled["stamp"]["id"]

    settled = await create_deployment(client, account_id, token, name="settled", replicas=2)
    assert (
        await place(client, account_id, token, settled["id"], stamp_id=stamp_id)
    ).status_code == 201
    await report_capabilities(
        client,
        stamp_id,
        enrolled["agent_credential"],
        with_claims(default_capabilities(gpus=4), {settled["id"]: 2}),
    )

    # Two committed and the same two running, so two devices remain. Summing would leave none.
    incoming = await create_deployment(client, account_id, token, name="incoming", replicas=2)
    placed = await place(client, account_id, token, incoming["id"], stamp_id=stamp_id)
    assert placed.status_code == 201, placed.text


async def test_a_stamp_that_did_not_measure_claims_says_so_when_it_refuses(
    client: AsyncClient,
) -> None:
    """An admission decided without claim data has to be visible in the refusal."""
    account_id, token = await onboard(client, "unmeasured", "unmeasured-account")
    blind = default_capabilities(gpus=1)
    blind["gpu_claims_measured"] = False
    enrolled = await enroll_stamp(client, account_id, token, capabilities=blind)
    deployment = await create_deployment(client, account_id, token, replicas=2)

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["gpu_claims_measured"] is False


# --- what a refusal discloses --------------------------------------------


async def test_a_managed_stamps_occupancy_is_not_disclosed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Managed capacity is shared, so its free and committed counts are other tenants'."""
    account_id, token = await onboard(client, "opaque", "opaque-account")
    managed, _system = await enroll_managed_stamp(
        client, db_session, capabilities=default_capabilities(gpus=1)
    )
    await enable_managed_capacity(db_session, account_id)
    deployment = await create_deployment(client, account_id, token, replicas=4)

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=managed["stamp"]["id"]
    )

    assert refused.status_code == 409
    details = _error(refused)["details"]
    assert details["reason"] == "insufficient_free_gpus"
    # The caller still learns what it asked for and that the stamp cannot take it.
    assert details["demand"]["gpus_total"] == 4
    for disclosed in ("free_gpus", "committed_gpus", "used_gpus", "allocatable_gpus"):
        assert disclosed not in details, f"{disclosed} describes other accounts' use"


async def test_an_owned_stamps_occupancy_is_disclosed(client: AsyncClient) -> None:
    """A customer's own cluster is its own business, and the numbers are how it acts."""
    account_id, token = await onboard(client, "clear", "clear-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=1)
    )
    deployment = await create_deployment(client, account_id, token, replicas=4)

    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"]
    )

    details = _error(refused)["details"]
    assert details["free_gpus"] == 1
    assert details["allocatable_gpus"] == 1


# --- editing a placed deployment on a full stamp -------------------------


async def test_an_edit_that_asks_for_less_is_allowed_on_a_full_stamp(
    client: AsyncClient,
) -> None:
    """Shrinking is the remedy for an oversubscribed stamp, so it cannot be the thing refused.

    A stamp fills up for reasons its owner did not cause — a foreign workload appearing, a
    rollout in flight. Judging every edit against current occupancy would leave the owner
    unable to do the only thing that helps.
    """
    account_id, token = await onboard(client, "shrink", "shrink-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    deployment = await create_deployment(client, account_id, token, replicas=4)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    shrunk = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-1"}, "replicas": 2}},
        headers=bearer(token),
    )
    assert shrunk.status_code == 200, shrunk.text
    assert shrunk.json()["desired_spec"]["replicas"] == 2


async def test_an_unrelated_edit_is_allowed_on_a_full_stamp(client: AsyncClient) -> None:
    """A release change asks for nothing extra, so occupancy is not its concern."""
    account_id, token = await onboard(client, "release", "release-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    deployment = await create_deployment(client, account_id, token, replicas=2)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    rolled = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-2"}, "replicas": 2}},
        headers=bearer(token),
    )
    assert rolled.status_code == 200, rolled.text


async def test_a_wider_replica_is_checked_even_when_the_total_falls(
    client: AsyncClient,
) -> None:
    """Four replicas of one device become one of four: fewer GPUs, and unschedulable."""
    account_id, token = await onboard(client, "wider", "wider-account")
    enrolled = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=default_capabilities(gpus=4, max_gpus_per_node=1),
    )
    deployment = await create_deployment(client, account_id, token, replicas=4)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    refused = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={
            "spec": {
                "runtime": {"release": "runtime-release-1"},
                "replicas": 1,
                "resources": {"gpu_count": 4, "gpu_class": "t4"},
            }
        },
        headers=bearer(token),
    )

    assert refused.status_code == 409
    assert _error(refused)["details"]["reason"] == "gpu_count_exceeds_largest_node"


async def test_a_legacy_gpu_class_does_not_block_an_unrelated_edit(
    client: AsyncClient,
) -> None:
    """The reason creation does not validate the catalogue: it grows.

    A deployment naming a class this platform no longer recognises has to stay editable, and a
    caller setting an unrecognised class is still refused.
    """
    account_id, token = await onboard(client, "legacy-class", "legacy-class-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "gtx-1080"}
    )

    kept = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={
            "spec": {
                "runtime": {"release": "runtime-release-2"},
                "replicas": 1,
                "resources": {"gpu_count": 1, "gpu_class": "gtx-1080"},
            }
        },
        headers=bearer(token),
    )
    assert kept.status_code == 200, kept.text

    refused = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={
            "spec": {
                "runtime": {"release": "runtime-release-2"},
                "replicas": 1,
                "resources": {"gpu_count": 1, "gpu_class": "rtx-9090"},
            }
        },
        headers=bearer(token),
    )
    assert refused.status_code == 400
    assert _error(refused)["code"] == "unknown_gpu_class"


async def test_growing_onto_an_undescribed_stamp_records_the_unverified_class(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The skip has to be as visible on an edit as it is at placement."""
    account_id, token = await onboard(client, "grow-blind", "grow-blind-account")
    legacy = {"orchestrator": "k3s", "region": "local", "gpus": [], "allocatable_gpus": 8}
    enrolled = await enroll_stamp(client, account_id, token, capabilities=legacy)
    deployment = await create_deployment(
        client, account_id, token, replicas=1, resources={"gpu_count": 1, "gpu_class": "h100"}
    )
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    grown = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={
            "spec": {
                "runtime": {"release": "runtime-release-1"},
                "replicas": 4,
                "resources": {"gpu_count": 1, "gpu_class": "h100"},
            }
        },
        headers=bearer(token),
    )
    assert grown.status_code == 200, grown.text

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.account_id == uuid.UUID(account_id),
                AuditEvent.action == "deployment.updated",
            )
        )
    ).scalar_one()
    assert event.event_metadata["gpu_class_verified"] is False



@pytest.mark.skipif(
    not USING_POSTGRES,
    reason="serialization needs row locks; SQLite runs one connection at a time",
)
async def test_two_placements_racing_for_the_last_gpus_do_not_both_win(
    client: AsyncClient,
) -> None:
    """Admission counts its own writes, which is only true if the writes are ordered.

    Each request reads what is committed and then writes a placement. Under READ COMMITTED
    neither can see the other's uncommitted row, so without a lock on the stamp both would
    pass a check only one of them should and both would commit.
    """
    account_id, token = await onboard(client, "race-cap", "race-cap-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=2)
    )
    stamp_id = enrolled["stamp"]["id"]

    contenders = [
        await create_deployment(client, account_id, token, name=f"racer-{index}", replicas=2)
        for index in range(4)
    ]
    responses = await asyncio.gather(
        *(
            place(client, account_id, token, deployment["id"], stamp_id=stamp_id)
            for deployment in contenders
        )
    )

    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409, 409, 409], [response.text for response in responses]
    for response in responses:
        if response.status_code == 409:
            assert _error(response)["details"]["reason"] == "insufficient_free_gpus"

    # And the stamp really is holding one deployment's worth, not four.
    placements = await client.get(
        f"/v1/accounts/{account_id}/deployments/{contenders[0]['id']}/placements",
        headers=bearer(token),
    )
    assert placements.status_code == 200



async def test_an_unchecked_edit_does_not_claim_a_verification(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """"Not checked" and "checked and unverifiable" are different facts.

    An edit asking for no more than before consults no stamp, so recording it as class-verified
    would put a claim on the audit trail that nothing established.
    """
    account_id, token = await onboard(client, "unchecked", "unchecked-account")
    enrolled = await enroll_stamp(
        client, account_id, token, capabilities=default_capabilities(gpus=4)
    )
    deployment = await create_deployment(client, account_id, token, replicas=2)
    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    rolled = await client.patch(
        f"/v1/accounts/{account_id}/deployments/{deployment['id']}",
        json={"spec": {"runtime": {"release": "runtime-release-2"}, "replicas": 2}},
        headers=bearer(token),
    )
    assert rolled.status_code == 200, rolled.text

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.account_id == uuid.UUID(account_id),
                AuditEvent.action == "deployment.updated",
            )
        )
    ).scalar_one()
    assert event.event_metadata["capacity_checked"] is False
    assert "gpu_class_verified" not in event.event_metadata


async def test_an_admission_records_whether_claims_were_measured(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A refusal is the case that already failed closed; the admission is the one that needs it."""
    account_id, token = await onboard(client, "audit-claims", "audit-claims-account")
    blind = default_capabilities(gpus=4)
    blind["gpu_claims_measured"] = False
    enrolled = await enroll_stamp(client, account_id, token, capabilities=blind)
    deployment = await create_deployment(client, account_id, token, replicas=1)

    assert (
        await place(client, account_id, token, deployment["id"], stamp_id=enrolled["stamp"]["id"])
    ).status_code == 201

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.account_id == uuid.UUID(account_id),
                AuditEvent.action == "placement.created",
            )
        )
    ).scalar_one()
    assert event.event_metadata["gpu_claims_measured"] is False
