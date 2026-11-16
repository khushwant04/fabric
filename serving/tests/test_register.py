"""The vLLM substitution takes its tile from the launch table and never serves BF16 on SM75."""

from __future__ import annotations

import json
import sys

import pytest
import torch
from kernels import gated_delta_packed_decode as packed
from kernels import launch_config as lc

from fabric_serving import register

VARIABLES = (lc.TUNING_FILE_ENV, lc.ENV_BLOCK_V, lc.ENV_NUM_WARPS, lc.ENV_NUM_STAGES)


class _ClaimsCuda(torch.Tensor):
    @property
    def is_cuda(self) -> bool:  # type: ignore[override]
        return True


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    # install() adds an import hook; keep it out of every other test.
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    monkeypatch.setattr(register, "delegations", {})
    lc.reload()
    yield
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    lc.reload()


@pytest.fixture
def calls(monkeypatch):
    seen = {"fabric": [], "original": []}

    def fabric(*args, **kwargs):
        seen["fabric"].append(kwargs)
        return "fabric"

    def original(*args, **kwargs):
        seen["original"].append(kwargs)
        return "original"

    monkeypatch.setattr(packed, "fused_packed_decode", fabric)
    monkeypatch.setattr(register._fabric_packed_decode, "_fabric_original", original, raising=False)
    return seen


def test_wrapper_leaves_the_tile_to_the_launch_table(calls):
    assert register._fabric_packed_decode(torch.zeros(1, 4)) == "fabric"
    # No tile injected: the kernel resolves explicit > env > tuning file > table itself.
    assert calls["fabric"] == [{}]


def test_wrapper_passes_explicit_tiles_through(calls):
    register._fabric_packed_decode(torch.zeros(1, 4), block_v=64, num_warps=2)
    assert calls["fabric"] == [{"block_v": 64, "num_warps": 2}]


def test_bf16_on_sm75_is_handed_back_to_vllm(calls, monkeypatch, capsys):
    monkeypatch.setattr(lc, "device_capability", lambda device: (7, 5))
    mixed = torch.zeros(2, 4, dtype=torch.bfloat16).as_subclass(_ClaimsCuda)
    assert register._fabric_packed_decode(mixed) == "original"
    assert register._fabric_packed_decode(mixed_qkv=mixed) == "original"
    assert calls["fabric"] == []
    assert sum(register.delegations.values()) == 2
    assert "--dtype half" in next(iter(register.delegations))
    assert "delegating" in capsys.readouterr().err


@pytest.mark.parametrize(
    "dtype,capability", [(torch.float16, (7, 5)), (torch.bfloat16, (8, 6))]
)
def test_supported_dtypes_reach_the_fabric_kernel(calls, monkeypatch, dtype, capability):
    monkeypatch.setattr(lc, "device_capability", lambda device: capability)
    mixed = torch.zeros(2, 4, dtype=dtype).as_subclass(_ClaimsCuda)
    assert register._fabric_packed_decode(mixed) == "fabric"
    assert not register.delegations


def test_install_reports_the_tile_sources(tmp_path, monkeypatch):
    path = tmp_path / "tuning.json"
    path.write_text(
        json.dumps(
            {
                "schema": lc.TUNING_SCHEMA,
                "entries": [
                    {
                        "kernel": "packed",
                        "compute_capability": "8.6",
                        "batch_bucket": "17+",
                        "block_v": 64,
                        "num_warps": 2,
                        "num_stages": 2,
                        "measured": True,
                    }
                ],
            }
        )
    )
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(path))
    monkeypatch.setenv(lc.ENV_BLOCK_V, "32")
    result = register.install()
    assert result["tile"] == {
        "environment_overrides": {"block_v": 32},
        "tuning_file": str(path),
        "tuning_entries": 1,
    }


def test_install_refuses_an_invalid_tuning_file(tmp_path, monkeypatch):
    path = tmp_path / "tuning.json"
    path.write_text(json.dumps({"schema": lc.TUNING_SCHEMA, "entries": [{"kernel": "packed"}]}))
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(path))
    before = list(sys.meta_path)
    with pytest.raises(lc.TuningFileError, match="compute_capability"):
        register.install()
    # Nothing armed: vLLM keeps its own op rather than failing the first request.
    assert sys.meta_path == before
