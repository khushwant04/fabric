"""AWS Neuron stamps: their capability report, their classes, and what they can hold.

A Neuron stamp (Inferentia2 or Trainium) reports its devices through the same capability
document a GPU stamp uses, marked ``accelerator: "neuron"``. Neuron classes are satisfied
only by Neuron stamps, and GPU and TPU classes never by a Neuron stamp, because each family
runs its own serving image.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.schemas import StampCapabilities
from app.services import placement as placement_service
from tests.helpers import create_deployment, default_capabilities, enroll_stamp, onboard, place
from tests.test_tpu_placement import tpu_capabilities

GIB = 1024**3


def neuron_capabilities(
    *,
    devices: int = 1,
    product: str = "Inferentia2",
    hbm_gib: int = 32,
    accelerator_type: str = "inf2.xlarge",
) -> dict[str, Any]:
    capabilities = default_capabilities(gpus=devices, memory_bytes=hbm_gib * GIB, product=product)
    capabilities["gpus"] = [
        {
            "product": product,
            "count": devices,
            "memory_bytes": hbm_gib * GIB,
            "source": "node label",
            "accelerator": "neuron",
            "vendor": "aws",
            "accelerator_type": accelerator_type,
        }
    ]
    return capabilities


def _error(response) -> dict:
    return response.json()["error"]


def test_the_capability_schema_accepts_neuron_devices() -> None:
    parsed = StampCapabilities.model_validate(neuron_capabilities())
    assert parsed.gpus[0].accelerator == "neuron"
    assert parsed.gpus[0].vendor == "aws"
    assert parsed.gpus[0].accelerator_type == "inf2.xlarge"
    assert parsed.gpus[0].compute_capability is None

    # TPU and GPU reports are unchanged.
    assert StampCapabilities.model_validate(tpu_capabilities()).gpus[0].accelerator == "tpu"
    gpu = StampCapabilities.model_validate(default_capabilities(gpus=1))
    assert gpu.gpus[0].accelerator is None and gpu.gpus[0].accelerator_type is None

    bad = neuron_capabilities(accelerator_type="inf2 xlarge")
    with pytest.raises(ValidationError):
        StampCapabilities.model_validate(bad)
    bad = neuron_capabilities(accelerator_type=".inf2")
    with pytest.raises(ValidationError):
        StampCapabilities.model_validate(bad)


def test_neuron_classes_are_in_the_catalogue_by_hbm_per_device() -> None:
    classes = placement_service.GPU_CLASSES
    assert classes["neuron-inf2"].memory_bytes == 32 * GIB
    assert classes["neuron-trn1"].memory_bytes == 32 * GIB
    assert classes["neuron-trn2"].memory_bytes == 96 * GIB
    assert all(
        classes[name].kind == "neuron" for name in ("neuron-inf2", "neuron-trn1", "neuron-trn2")
    )
    assert placement_service.is_neuron_class("neuron-inf2")
    assert not placement_service.is_neuron_class("tpu-v6e")
    assert not placement_service.is_neuron_class("t4")
    assert not placement_service.is_tpu_class("neuron-inf2")
    assert placement_service.normalise_class("NEURON_inf2") == "neuron-inf2"


async def test_a_neuron_deployment_fits_an_inf2_stamp(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "neuron-fit", "neuron-fit-account")
    deployment = await create_deployment(
        client,
        account_id,
        token,
        release="Qwen/Qwen3.5-4B",
        resources={"gpu_count": 1, "gpu_class": "neuron-inf2"},
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=neuron_capabilities())
    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"])
    assert placed.status_code == 201, placed.text


async def test_trainium2_satisfies_an_inferentia2_class(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "neuron-up", "neuron-up-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 2, "gpu_class": "neuron-inf2"}
    )
    stamp = await enroll_stamp(
        client,
        account_id,
        token,
        capabilities=neuron_capabilities(
            devices=16, product="Trainium2", hbm_gib=96, accelerator_type="trn2.48xlarge"
        ),
    )
    placed = await place(client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"])
    assert placed.status_code == 201, placed.text


async def test_an_inf2_stamp_does_not_satisfy_a_trn2_class_and_says_hbm_times_devices(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "neuron-down", "neuron-down-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "neuron-trn2"}
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=neuron_capabilities())
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "gpu_class_not_satisfied"
    assert details["devices_per_replica"] == 1
    assert details["available_total_hbm_bytes"] == 32 * GIB
    assert details["required_total_hbm_bytes"] == int(96 * GIB * 0.9)


@pytest.mark.parametrize("gpu_class", ["t4", "tpu-v6e"])
async def test_gpu_and_tpu_classes_are_never_placed_on_a_neuron_stamp(
    client: AsyncClient, gpu_class: str
) -> None:
    account_id, token = await onboard(client, f"neuron-{gpu_class}", f"neuron-{gpu_class}-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": gpu_class}
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=neuron_capabilities())
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "accelerator_kind_mismatch"
    assert details["stamp_accelerator"] == "neuron"


@pytest.mark.parametrize(
    "capabilities",
    [
        default_capabilities(
            gpus=8, product="NVIDIA H100", memory_bytes=80 * GIB, compute_capability="9.0"
        ),
        tpu_capabilities(),
    ],
    ids=["gpu", "tpu"],
)
async def test_a_neuron_class_is_never_placed_on_a_gpu_or_tpu_stamp(
    client: AsyncClient, capabilities: dict[str, Any]
) -> None:
    kind = "gpu" if "accelerator" not in capabilities["gpus"][0] else "tpu"
    account_id, token = await onboard(client, f"{kind}-neuron", f"{kind}-neuron-account")
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "neuron-inf2"}
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=capabilities)
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 409, refused.text
    details = _error(refused)["details"]
    assert details["reason"] == "accelerator_kind_mismatch"
    assert details["required_accelerator"] == "neuron"
    assert details["stamp_accelerator"] == kind


async def test_automatic_selection_picks_the_neuron_stamp_for_a_neuron_class(
    client: AsyncClient,
) -> None:
    account_id, token = await onboard(client, "neuron-auto", "neuron-auto-account")
    await enroll_stamp(client, account_id, token, name="gpu-stamp")
    await enroll_stamp(client, account_id, token, name="tpu-stamp", capabilities=tpu_capabilities())
    neuron = await enroll_stamp(
        client, account_id, token, name="inf2-stamp", capabilities=neuron_capabilities()
    )
    deployment = await create_deployment(
        client, account_id, token, resources={"gpu_count": 1, "gpu_class": "neuron-inf2"}
    )
    placed = await place(client, account_id, token, deployment["id"])
    assert placed.status_code == 201, placed.text
    assert placed.json()["stamp_id"] == neuron["stamp"]["id"]


async def test_an_fp8_checkpoint_on_a_neuron_class_is_a_bad_request(client: AsyncClient) -> None:
    account_id, token = await onboard(client, "neuron-fp8", "neuron-fp8-account")
    deployment = await create_deployment(
        client,
        account_id,
        token,
        release="Qwen/Qwen3.6-35B-A3B-FP8",
        resources={"gpu_count": 1, "gpu_class": "neuron-inf2"},
    )
    stamp = await enroll_stamp(client, account_id, token, capabilities=neuron_capabilities())
    refused = await place(
        client, account_id, token, deployment["id"], stamp_id=stamp["stamp"]["id"]
    )
    assert refused.status_code == 400, refused.text
    assert _error(refused)["code"] == "fp8_not_supported_on_neuron"


def test_changing_to_or_from_neuron_counts_as_a_stricter_demand() -> None:
    def demand(gpu_class: str) -> placement_service.Demand:
        return placement_service.Demand(
            replicas=1, gpus_per_replica=1, gpu_class=gpu_class, region=None
        )

    for other in ("h100", "tpu-v5p"):
        assert demand("neuron-inf2").exceeds(demand(other))
        assert demand(other).exceeds(demand("neuron-inf2"))
    assert demand("neuron-trn2").exceeds(demand("neuron-inf2"))
    assert not demand("neuron-inf2").exceeds(demand("neuron-trn2"))
