"""Crash durability and lease semantics for local usage metering."""

from __future__ import annotations

import datetime as dt
import pathlib
import subprocess
import sys
import uuid

import pytest

from fabric_data_plane.usage import (
    UsageBuffer,
    UsageLeaseMismatch,
    UsageRecord,
    UsageSpoolUnavailable,
)

ACCOUNT = uuid.UUID("11111111-1111-1111-1111-111111111111")
DEPLOYMENT = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def record(number: int) -> UsageRecord:
    return UsageRecord(
        record_id=uuid.UUID(f"00000000-0000-0000-0000-{number:012d}"),
        account_id=ACCOUNT,
        deployment_id=DEPLOYMENT,
        input_tokens=number,
        output_tokens=number + 1,
        streamed=bool(number % 2),
        occurred_at=dt.datetime(2026, 9, 18, 12, number % 60, tzinfo=dt.UTC),
    )


def test_records_and_counters_survive_a_data_plane_restart(tmp_path) -> None:
    path = tmp_path / "usage" / "spool.db"
    first = UsageBuffer(capacity=16, path=str(path))
    first.record(record(1))
    before = first.snapshot()
    assert before["durable"] is True
    assert before["recorded"] == 1
    first.close()

    restarted = UsageBuffer(capacity=16, path=str(path))
    lease = restarted.lease()
    assert lease.lease_id is not None
    assert lease.records == [record(1)]
    assert restarted.snapshot()["recorded"] == 1
    restarted.close()


def test_an_outstanding_lease_survives_restart_and_acknowledgement(tmp_path) -> None:
    path = tmp_path / "spool.db"
    first = UsageBuffer(capacity=16, path=str(path))
    first.record(record(1))
    first.record(record(2))
    original = first.lease(limit=1)
    assert [item.record_id for item in original.records] == [record(1).record_id]
    first.close()

    restarted = UsageBuffer(capacity=16, path=str(path))
    replay = restarted.lease(limit=16)
    assert replay == original
    assert replay.lease_id is not None
    first_ack = restarted.acknowledge(replay.lease_id, expected_count=1)
    assert first_ack.acknowledged == 1
    assert first_ack.deleted == 1
    assert first_ack.already_acknowledged is False
    repeated_ack = restarted.acknowledge(replay.lease_id, expected_count=1)
    assert repeated_ack.acknowledged == 1
    assert repeated_ack.deleted == 0
    assert repeated_ack.already_acknowledged is True  # lost ack response retry

    next_lease = restarted.lease()
    assert [item.record_id for item in next_lease.records] == [record(2).record_id]
    restarted.close()


def test_overflow_drops_the_oldest_unleased_records() -> None:
    spool = UsageBuffer(capacity=2)
    for number in (1, 2, 3):
        spool.record(record(number))

    lease = spool.lease()
    assert [item.record_id for item in lease.records] == [record(2).record_id, record(3).record_id]
    assert spool.snapshot() == {
        "healthy": True,
        "last_error": None,
        "buffered": 2,
        "leased": 2,
        "available": 0,
        "capacity": 2,
        "recorded": 3,
        "dropped": 1,
        "durable": False,
        "export_mode": "lease_ack",
    }
    spool.close()


def test_an_outstanding_lease_is_never_evicted() -> None:
    spool = UsageBuffer(capacity=2)
    spool.record(record(1))
    spool.record(record(2))
    protected = spool.lease()

    spool.record(record(3))

    assert spool.lease() == protected
    state = spool.snapshot()
    assert state["buffered"] == 2
    assert state["recorded"] == 3
    assert state["dropped"] == 1
    assert protected.lease_id is not None
    assert spool.acknowledge(protected.lease_id, expected_count=2).acknowledged == 2
    assert spool.lease().records == []
    spool.close()


def test_acknowledgement_count_mismatch_cannot_delete_part_of_a_lease() -> None:
    spool = UsageBuffer(capacity=4)
    spool.record(record(1))
    spool.record(record(2))
    lease = spool.lease()
    assert lease.lease_id is not None

    with pytest.raises(UsageLeaseMismatch):
        spool.acknowledge(lease.lease_id, expected_count=1)

    assert spool.lease() == lease
    assert spool.snapshot()["buffered"] == 2
    spool.close()


def test_lease_size_is_bounded() -> None:
    spool = UsageBuffer(capacity=4)
    for number in range(1, 5):
        spool.record(record(number))

    first = spool.lease(limit=2)
    assert len(first.records) == 2
    # An outstanding lease is stable even when the caller asks for a different size.
    assert spool.lease(limit=4) == first
    assert first.lease_id is not None
    assert spool.acknowledge(first.lease_id, expected_count=2).acknowledged == 2
    assert len(spool.lease(limit=4).records) == 2
    spool.close()



def test_reopening_with_a_lower_capacity_trims_oldest_unleased_records(tmp_path) -> None:
    path = tmp_path / "private" / "usage.db"
    original = UsageBuffer(capacity=4, path=str(path))
    for number in range(1, 5):
        original.record(record(number))
    original.close()

    smaller = UsageBuffer(capacity=2, path=str(path))
    lease = smaller.lease()
    assert [item.record_id for item in lease.records] == [record(3).record_id, record(4).record_id]
    state = smaller.snapshot()
    assert state["buffered"] == 2
    assert state["capacity"] == 2
    assert state["dropped"] == 2
    smaller.close()


def test_reopening_never_trims_an_existing_oversized_lease(tmp_path) -> None:
    path = tmp_path / "private" / "usage.db"
    original = UsageBuffer(capacity=4, path=str(path))
    for number in range(1, 5):
        original.record(record(number))
    leased = original.lease(limit=4)
    original.close()

    smaller = UsageBuffer(capacity=2, path=str(path))
    assert smaller.lease(limit=2) == leased
    assert smaller.snapshot()["buffered"] == 4
    # The stable lease is protected, so a new completion is the record that is dropped.
    smaller.record(record(5))
    assert smaller.snapshot()["dropped"] == 1
    smaller.close()


def test_duplicate_record_identity_is_locally_idempotent() -> None:
    spool = UsageBuffer(capacity=4)
    item = record(1)
    spool.record(item)
    spool.record(item)

    assert spool.lease().records == [item]
    assert spool.snapshot()["recorded"] == 1
    spool.close()


def test_private_directory_and_all_live_sqlite_files_are_restricted(tmp_path) -> None:
    path = tmp_path / "private" / "usage.db"
    spool = UsageBuffer(capacity=4, path=str(path))
    spool.record(record(1))

    assert pathlib.Path(path).parent.stat().st_mode & 0o777 == 0o700
    sqlite_files = list(path.parent.glob("usage.db*"))
    assert sqlite_files
    for candidate in sqlite_files:
        assert candidate.stat().st_mode & 0o777 == 0o600, candidate
    spool.close()


def test_committed_wal_record_survives_a_hard_process_exit(tmp_path) -> None:
    path = tmp_path / "private" / "usage.db"
    program = """
import datetime as dt
import os
import sys
import uuid
from fabric_data_plane.usage import UsageBuffer, UsageRecord
spool = UsageBuffer(capacity=4, path=sys.argv[1])
spool.record(UsageRecord(
    record_id=uuid.UUID('00000000-0000-0000-0000-000000000001'),
    account_id=uuid.UUID('11111111-1111-1111-1111-111111111111'),
    deployment_id=uuid.UUID('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'),
    input_tokens=1,
    output_tokens=2,
    streamed=True,
    occurred_at=dt.datetime(2026, 9, 18, 12, 1, tzinfo=dt.UTC),
))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", program, str(path)], check=True)

    recovered = UsageBuffer(capacity=4, path=str(path))
    assert recovered.lease().records == [record(1)]
    recovered.close()


def test_runtime_storage_failure_marks_the_spool_unhealthy() -> None:
    spool = UsageBuffer(capacity=4)
    spool.close()

    with pytest.raises(UsageSpoolUnavailable):
        spool.record(record(1))

    assert spool.healthy is False
    state = spool.snapshot()
    assert state["healthy"] is False
    assert "ProgrammingError" in state["last_error"]
