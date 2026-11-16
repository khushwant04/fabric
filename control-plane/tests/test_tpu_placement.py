"""Cloud TPU stamps: their capability report, their classes, and what they can hold.

A TPU stamp reports its chips through the same capability document a GPU stamp uses, marked
``accelerator: "tpu"``. TPU classes are satisfied only by TPU stamps and GPU classes only by
GPU stamps, because the serving image differs and neither runs on the other's hardware.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.schemas import StampCapabilities
from app.services import placement as placement_service
from tests.helpers import create_deployment, default_capabilities, enroll_stamp, onboard, place

GIB = 1024**3


def tpu_capabilities(
    *, chips: int = 4, product: str = "TPU v6e", hbm_gib: int = 32, accelerator_type: str = "v6e-4"
) -> dict[str, Any]:
    capabilities = default_capabilities(gpus=chips, memory_bytes=hbm_gib * GIB, product=product)
    capabilities["gpus"] = [
        {
            "product": product,
            "count": chips,
            "memory_bytes": hbm_gib * GIB,
            "source": "node label",
            "accelerator": "tpu",
            "vendor": "google",
            "accelerator_type": accelerator_type,
        }
    ]
    return capabilities


def _error(response) -> dict:
    return response.json()["error"]


def test_the_capability_schema_accepts_tpu_chips_and_still_accepts_gpus() -> None:
    parsed = StampCapabilities.model_validate(tpu_capabilities())
    assert parsed.gpus[0].accelerator == "tpu"
    assert parsed.gpus[0].accelerator_type == "v6e-4"
    assert parsed.gpus[0].compute_capability is None

    gpu = StampCapabilities.model_validate(default_capabilities(gpus=1))
    assert gpu.gpus[0].accelerator is None and gpu.gpus[0].vendor is None

    bad = tpu_capabilities()
    bad["gpus"][0]["accelerator"] = "npu"
    with pytest.raises(ValidationError):
        StampCapabilities.model_validate(bad)
    bad = tpu_capabilities(accelerator_type="V6E 4")
    with pytest.raises(ValidationError):
        StampCapabilities.model_validate(bad)


def test_tpu_classes_are_in_the_catalogue_by_hbm_per_chip() -> None:
    assert placement_service.GPU_CLASSES["tpu-v5e"].memory_bytes == 16 * GIB
    assert placement_service.GPU_CLASSES["tpu-v6e"].memory_bytes == 32 * GIB
    assert placement_service.GPU_CLASSES["tpu-v5p"].memory_bytes == 95 * GIB
    assert placement_service.GPU_CLASSES["tpu-v4"].memory_bytes == 32 * GIB
    assert all(
        placement_service.GPU_CLASSES[name].kind == "tpu"
        for name in ("tpu-v5e", "tpu-v6e", "tpu-v5p", "tpu-v4")
    )
    assert placement_service.GPU_CLASSES["t4"].kind == "gpu"
    assert placement_service.normalise_class("TPU_v6e") == "tpu-v6e"


def test_fp8_checkpoints_are_recognised() -> None:
    assert placement_service.fp8_checkpoint("Qwen/Qwen3.6-35B-A3B-FP8")
    assert placement_service.fp8_checkpoint("org/model_fp8")
    assert not placement_service.fp8_checkpoint("Qwen/Qwen3.5-9B")
    assert not placement_service.fp8_checkpoint(None)


async def test_a_tpu_deployment_fits_a_tpu_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "tpu-fit", "tpu-fit-account")
    deployment = await create_deployment(
        client,
        account_id,
        token,
        release="Qwen/Qwen3.5-9B",
        resources={"gpu_count": 4, "gpu_class": "tpu-v6e"},
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=tpu_capabilities())

    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"])
    assert placed.status_code == 201, placed.text


async def test_a_better_tpu_satisfies_a_smaller_tpu_class(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "tpu-up", "tpu-up-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 4, "gpu_class": "tpu-v5e"}
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=tpu_capabilities())
    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"])
    assert placed.status_code == 201, placed.text


async def test_a_v5e_stamp_does_not_satisfy_a_v6e_class_and_says_hbm_times_chips(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "tpu-down", "tpu-down-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 4, "gpu_class": "tpu-v6e"}
    )
    stamp = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=tpu_capabilities(
            product="TPU v5e", hbm_gib=16, accelerator_type="v5litepod-4"
        ),
    )
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "gpu_class_not_satisfied"
    assert details["chips_per_replica"] == 4
    assert details["available_total_hbm_bytes"] == 64 * GIB
    assert details["required_total_hbm_bytes"] == int(32 * GIB * 0.9) * 4


async def test_a_gpu_class_is_never_placed_on_a_tpu_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "tpu-gpu", "tpu-gpu-account")
    # The schema's default class is t4: a deployment that never chose TPU stays off TPUs.
    deployment = await create_deployment(client, account_id, token)
    stamp = await enroll_stamp(client, account_id, token, capabilities=tpu_capabilities())
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "accelerator_kind_mismatch"
    assert details["required_accelerator"] == "gpu"
    assert details["stamp_accelerator"] == "tpu"


async def test_a_tpu_class_is_never_placed_on_a_gpu_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "gpu-tpu", "gpu-tpu-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "tpu-v5e"}
    )
    stamp = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=default_capabilities(
            gpus=8, product="NVIDIA H100", memory_bytes=80 * GIB, compute_capability="9.0"
        ),
    )
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409, refused.text
    assert _error(refused)["details"]["reason"] == "accelerator_kind_mismatch"


async def test_automatic_selection_picks_the_tpu_stamp_for_a_tpu_class(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "tpu-auto", "tpu-auto-account")
    await enroll_stamp(client, account_id, token, name="gpu-stamp")
    tpu = await enroll_stamp(
        client, account_id, token, name="tpu-stamp", capabilities=tpu_capabilities()
    )
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 4, "gpu_class": "tpu-v6e"}
    )
    placed = await place(client, account_id, token, deployment["id"])
    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == tpu["stamp"]["id"]


async def test_an_fp8_checkpoint_on_a_tpu_class_is_a_bad_request(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "tpu-fp8", "tpu-fp8-account")
    deployment = await create_deployment(
        client,
        account_id,
        token,
        release="Qwen/Qwen3.6-35B-A3B-FP8",
        resources={"gpu_count": 4, "gpu_class": "tpu-v6e"},
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=tpu_capabilities())
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 400, refused.text
    assert _error(refused)["code"] == "fp8_not_supported_on_tpu"


def test_changing_accelerator_kind_counts_as_a_stricter_demand() -> None:
    gpu = placement_service.Demand(replicas=1, gpus_per_replica=1, gpu_class="h100", region=None)
    tpu = placement_service.Demand(replicas=1, gpus_per_replica=1, gpu_class="tpu-v5e", region=None)
    assert tpu.exceeds(gpu)
    assert gpu.exceeds(tpu)
