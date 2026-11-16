"""Per-architecture launch table, override precedence, tuning-file validation."""

from __future__ import annotations

import json
import pathlib

import pytest
import torch

from harness.artifact import read_artifact
from kernels import launch_config as lc

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
T4 = (7, 5)
A10 = (8, 6)
DEV = (8, 9)
UNKNOWN = (9, 0)
VARIABLES = (lc.TUNING_FILE_ENV, lc.ENV_BLOCK_V, lc.ENV_NUM_WARPS, lc.ENV_NUM_STAGES)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    lc.reload()
    yield
    for variable in VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    lc.reload()


def tuning(tmp_path, entries, **extra):
    path = tmp_path / "tuning.json"
    path.write_text(json.dumps({"schema": lc.TUNING_SCHEMA, "entries": entries, **extra}))
    return path


def entry(**overrides):
    return {
        "kernel": "packed",
        "compute_capability": "7.5",
        "batch_bucket": "1",
        "block_v": 64,
        "num_warps": 2,
        "num_stages": 2,
        "measured": True,
        **overrides,
    }


def shape(config):
    return (config.block_v, config.num_warps, config.num_stages)


@pytest.mark.parametrize(
    "batch,bucket",
    [(1, "1"), (2, "2-4"), (4, "2-4"), (5, "5-16"), (16, "5-16"), (17, "17+"), (512, "17+")],
)
def test_batch_buckets_have_the_documented_boundaries(batch, bucket):
    assert lc.batch_bucket(batch) == bucket


def test_nonpositive_batch_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        lc.batch_bucket(0)


@pytest.mark.parametrize(
    "capability,batch,expected,measured",
    [
        (T4, 1, (16, 1, 3), True),
        (T4, 3, (16, 1, 3), True),
        (T4, 8, (16, 1, 3), True),
        (T4, 64, (32, 1, 1), False),
        (A10, 1, (16, 1, 3), False),
        (A10, 4, (32, 1, 3), False),
        (A10, 16, (32, 1, 3), False),
        (A10, 64, (32, 1, 3), False),
        (DEV, 1, (32, 1, 3), False),
        (UNKNOWN, 1, (32, 1, 3), False),
        (UNKNOWN, 100, (32, 1, 3), False),
    ],
)
def test_packed_table_per_capability_and_bucket(capability, batch, expected, measured):
    config = lc.resolve(lc.KERNEL_PACKED, capability, batch)
    assert shape(config) == expected
    assert config.measured is measured


@pytest.mark.parametrize(
    "capability,batch,expected",
    [
        (T4, 1, (16, 4, 1)),
        (T4, 32, (32, 8, 1)),
        (A10, 1, (16, 4, 3)),
        (A10, 8, (32, 8, 3)),
        (DEV, 4, (32, 8, 3)),
        (UNKNOWN, 4, (32, 8, 3)),
    ],
)
def test_recurrent_table_per_capability_and_bucket(capability, batch, expected):
    assert shape(lc.resolve(lc.KERNEL_RECURRENT, capability, batch)) == expected


def test_unknown_capability_uses_the_hosts_own_tiling():
    assert lc.resolve(lc.KERNEL_PACKED, UNKNOWN, 1) is lc.DEFAULTS[lc.KERNEL_PACKED]
    assert lc.resolve(lc.KERNEL_RECURRENT, UNKNOWN, 1) is lc.DEFAULTS[lc.KERNEL_RECURRENT]


def test_every_table_entry_is_launchable_and_explains_itself():
    for kernel, per_cc in lc.TABLE.items():
        for cc, buckets in per_cc.items():
            assert set(buckets) == set(lc.BUCKET_LABELS), (kernel, cc)
            for bucket, config in buckets.items():
                assert lc._check_tile(kernel, *shape(config)) is None, (kernel, cc, bucket)
                assert config.note, (kernel, cc, bucket)
                if config.measured:
                    assert config.evidence, (kernel, cc, bucket)
                else:
                    assert config.note.startswith("Unmeasured"), (kernel, cc, bucket)


def test_sm75_entries_never_rely_on_deep_pipelining():
    for kernel in lc.KERNELS:
        for config in lc.TABLE[kernel]["7.5"].values():
            # Measured entries keep the value they were recorded with (3); reasoned ones
            # use 1 or 2, since SM75 has no cp.async to drive Triton's pipeliner.
            assert config.num_stages <= (3 if config.measured else 2)


def test_measured_t4_entries_are_what_the_committed_artifacts_recorded():
    """A table entry may claim measurement only if an artifact actually records it."""
    best: dict[int, set[tuple[int, int]]] = {}
    for relative in lc._T4_ARTIFACTS:
        artifact = read_artifact(REPO_ROOT / relative)
        assert artifact["environment"]["gpu"]["compute_capability"] == "7.5"
        assert artifact["config"]["measured_in_cuda_graph"] is True
        for row in artifact["measurements"]:
            choice = (row["best_block_v"], row["best_num_warps"])
            label = f"{choice[0]}/{choice[1]}"
            assert row["candidates"][label]["status"] == "pass"
            assert row["candidates"][label]["settings"]["num_stages"] == 3
            best.setdefault(row["batch"], set()).add(choice)
    packed = lc.TABLE[lc.KERNEL_PACKED]["7.5"]
    for batch in (1, 4, 8):
        bucket = packed[lc.batch_bucket(batch)]
        assert bucket.measured
        assert best[batch] == {(bucket.block_v, bucket.num_warps)}
    assert (packed["5-16"].block_v, packed["5-16"].num_warps) in best[16]
    assert not packed["17+"].measured


def test_measured_dev_entries_cite_artifacts_that_exist():
    for config in lc.TABLE[lc.KERNEL_RECURRENT]["8.9"].values():
        for relative in config.evidence:
            artifact = read_artifact(REPO_ROOT / relative)
            assert artifact["environment"]["gpu"]["compute_capability"] == "8.9"
            assert artifact["tile_sweep"]


# --- precedence --------------------------------------------------------------------------


def test_tuning_file_beats_the_table(tmp_path, monkeypatch):
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(tuning(tmp_path, [entry()])))
    lc.reload()
    config = lc.resolve(lc.KERNEL_PACKED, T4, 1)
    assert shape(config) == (64, 2, 2)
    assert config.source.startswith("tuning-file:")
    # Buckets and capabilities the file does not mention keep the table's entry.
    assert shape(lc.resolve(lc.KERNEL_PACKED, T4, 64)) == (32, 1, 1)
    assert shape(lc.resolve(lc.KERNEL_PACKED, A10, 1)) == (16, 1, 3)


def test_environment_beats_the_tuning_file(tmp_path, monkeypatch):
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(tuning(tmp_path, [entry()])))
    monkeypatch.setenv(lc.ENV_BLOCK_V, "32")
    lc.reload()
    config = lc.resolve(lc.KERNEL_PACKED, T4, 1)
    # The tile is overridden, so warps are derived for it rather than taken from an entry
    # tuned for 64; stages are independent of the tile and still come from the file.
    assert shape(config) == (32, 1, 2)
    assert config.measured is False
    assert "environment" in config.source


def test_environment_warps_and_stages(monkeypatch):
    monkeypatch.setenv(lc.ENV_NUM_WARPS, "2")
    monkeypatch.setenv(lc.ENV_NUM_STAGES, "1")
    lc.reload()
    assert shape(lc.resolve(lc.KERNEL_PACKED, T4, 1)) == (16, 2, 1)


def test_explicit_arguments_beat_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(tuning(tmp_path, [entry()])))
    monkeypatch.setenv(lc.ENV_BLOCK_V, "32")
    monkeypatch.setenv(lc.ENV_NUM_WARPS, "2")
    lc.reload()
    config = lc.resolve(lc.KERNEL_PACKED, T4, 1, block_v=128)
    assert shape(config) == (128, 2, 2)
    config = lc.resolve(lc.KERNEL_PACKED, T4, 1, block_v=128, num_warps=4, num_stages=1)
    assert shape(config) == (128, 4, 1)
    assert config.source == "explicit"


def test_explicit_tile_alone_derives_warps_like_the_kernel_always_did():
    assert shape(lc.resolve(lc.KERNEL_PACKED, T4, 1, block_v=128)) == (128, 4, 3)
    assert shape(lc.resolve(lc.KERNEL_RECURRENT, UNKNOWN, 1, block_v=16)) == (16, 4, 3)


def test_environment_overrides_apply_to_the_packed_kernel_only(monkeypatch):
    monkeypatch.setenv(lc.ENV_BLOCK_V, "128")
    lc.reload()
    assert lc.resolve(lc.KERNEL_PACKED, T4, 1).block_v == 128
    assert shape(lc.resolve(lc.KERNEL_RECURRENT, T4, 1)) == (16, 4, 1)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-4"])
def test_unusable_environment_values_are_ignored_as_before(monkeypatch, raw):
    monkeypatch.setenv(lc.ENV_BLOCK_V, raw)
    lc.reload()
    assert shape(lc.resolve(lc.KERNEL_PACKED, T4, 1)) == (16, 1, 3)


def test_fully_explicit_call_never_reads_the_tuning_file(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    lc.reload()
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(bad))
    lc._state.loaded = False  # as if this process had not looked yet
    config = lc.resolve(lc.KERNEL_PACKED, T4, 1, block_v=16, num_warps=1, num_stages=1)
    assert shape(config) == (16, 1, 1)


def test_resolution_is_cached_per_capability_and_batch():
    first = lc.resolve(lc.KERNEL_PACKED, T4, 1)
    assert lc.resolve(lc.KERNEL_PACKED, T4, 1) is first


def test_device_capability_is_queried_once_per_device(monkeypatch):
    calls = []

    def fake(index):
        calls.append(index)
        return (7, 5)

    monkeypatch.setattr(torch.cuda, "get_device_capability", fake)
    lc._capability_for_index.cache_clear()
    try:
        for _ in range(5):
            assert lc.device_capability(torch.device("cuda", 0)) == (7, 5)
        assert lc.device_capability(1) == (7, 5)
        assert calls == [0, 1]
    finally:
        lc._capability_for_index.cache_clear()


def test_device_capability_rejects_a_cpu_device():
    with pytest.raises(ValueError, match="not a CUDA device"):
        lc.device_capability(torch.device("cpu"))


# --- tuning file validation --------------------------------------------------------------


@pytest.mark.parametrize(
    "document,match",
    [
        ([], "JSON object"),
        ({"entries": [entry()]}, "schema"),
        ({"schema": lc.TUNING_SCHEMA, "entries": []}, "non-empty"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(kernel="other")]}, "kernel"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(compute_capability=7.5)]}, "'7.5'"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(batch_bucket="3")]}, "batch_bucket"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(block_v=24)]}, "block_v"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(block_v=True)]}, "block_v"),
        (
            {"schema": lc.TUNING_SCHEMA, "entries": [entry(kernel="recurrent", block_v=64)]},
            "recurrent",
        ),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(num_warps=3)]}, "num_warps"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(num_stages=0)]}, "num_stages"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(measured="yes")]}, "measured"),
        ({"schema": lc.TUNING_SCHEMA, "entries": [entry(), entry()]}, "duplicates"),
    ],
)
def test_invalid_tuning_documents_are_rejected_whole(tmp_path, monkeypatch, document, match):
    path = tmp_path / "tuning.json"
    path.write_text(json.dumps(document))
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(path))
    with pytest.raises(lc.TuningFileError, match=match) as raised:
        lc.reload()
    assert str(path) in str(raised.value)


def test_unparseable_or_missing_tuning_file_is_a_clear_error(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(broken))
    with pytest.raises(lc.TuningFileError, match="not valid JSON"):
        lc.reload()
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(lc.TuningFileError, match="cannot read"):
        lc.reload()


def test_reload_reports_what_is_in_effect(tmp_path, monkeypatch):
    path = tuning(tmp_path, [entry(), entry(batch_bucket="17+")])
    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(path))
    monkeypatch.setenv(lc.ENV_NUM_WARPS, "1")
    assert lc.reload() == {
        "environment_overrides": {"num_warps": 1},
        "tuning_file": str(path),
        "tuning_entries": 2,
    }


# --- dtype policy ------------------------------------------------------------------------


def test_bf16_is_refused_below_ampere_with_the_remedy():
    reason = lc.bf16_unsupported_reason(torch.bfloat16, T4)
    assert reason and "7.5" in reason and "--dtype half" in reason
    assert lc.bf16_unsupported_reason(torch.bfloat16, A10) is None
    assert lc.bf16_unsupported_reason(torch.float16, T4) is None
    assert lc.bf16_unsupported_reason(torch.float32, T4) is None
