"""Regression tests for balance-only ORESTAR-absence certification."""

from __future__ import annotations

import csv
import gzip
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest


SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import process as P  # noqa: E402
from balance_snapshot import (  # noqa: E402
    transaction_filer_snapshots,
    transaction_snapshot_id,
)


START = date(2006, 1, 1)
END = date(2026, 9, 2)
CAPTURED_AT = datetime(
    2026, 9, 1, tzinfo=timezone.utc,
).timestamp()


def _transaction(fid: str, tran_id: str) -> dict[str, str]:
    return {
        "tran_id": tran_id,
        "original id": tran_id,
        "tran_date": "09/01/2026",
        "filer id": fid,
        "amount": "100.00",
    }


def _shards(tmp_path: Path, rows: list[dict[str, str]]):
    transaction_dir = tmp_path / "transactions"
    transaction_dir.mkdir()
    with gzip.open(
        transaction_dir / "txn_2026.csv.gz", "wt", encoding="utf-8", newline="",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "tran_id", "original id", "tran_date", "filer id", "amount",
        ])
        writer.writeheader()
        writer.writerows(rows)
    ids = sorted({row["filer id"] for row in rows})
    return (
        transaction_dir,
        transaction_snapshot_id(transaction_dir),
        transaction_filer_snapshots(transaction_dir, ids, START, END),
    )


def _comparison(ids: list[str], fingerprint: str) -> dict:
    return {
        "status": "paired",
        "captured_at": CAPTURED_AT,
        "app_transaction_snapshot_id": fingerprint,
        "filer_ids": ids,
    }


def _observation(
    fid: str,
    fingerprint: str,
    digest: str,
    *,
    started: datetime | None = None,
    missing=(),
    surplus=(),
) -> dict:
    started = started or datetime(2026, 9, 2, tzinfo=timezone.utc)
    checked = started + timedelta(microseconds=1)
    missing = list(missing)
    surplus = list(surplus)
    return {
        "filer_id": fid,
        "name": f"Committee {fid}",
        "orestar": 1 - len(surplus) + len(missing),
        "held": 1,
        "complete": not surplus and not missing,
        "missing": missing,
        "surplus": surplus,
        "superseded": [],
        "evidence_version": 2,
        "collection_started_at": started.isoformat().replace("+00:00", "Z"),
        "checked": checked.date().isoformat(),
        "checked_at": checked.isoformat().replace("+00:00", "Z"),
        "transaction_snapshot_id": fingerprint,
        "filer_transaction_digest": digest,
        "range_start": START.isoformat(),
        "range_end": END.isoformat(),
    }


def _certify(
    rows,
    transaction_dir: Path,
    fingerprint: str,
    ids=("1",),
):
    members = list(ids)
    return P._certified_orestar_absent(
        rows,
        {"Canonical": members},
        {"Canonical": _comparison(members, fingerprint)},
        transaction_dir,
    )


def test_legacy_surplus_is_audit_only_and_cannot_suppress_cash(tmp_path) -> None:
    transaction_dir, fingerprint, _snapshots = _shards(
        tmp_path, [_transaction("1", "old")],
    )
    legacy = {
        "filer_id": "1",
        "complete": False,
        "missing": [],
        "surplus": ["old"],
        "superseded": [],
        "checked": "2026-09-02",
    }

    absent, blocked, error = _certify([legacy], transaction_dir, fingerprint)

    assert absent == {}
    assert blocked == {"1"}
    assert error is None


def test_newer_clean_verdict_overrides_old_surplus(tmp_path) -> None:
    transaction_dir, fingerprint, snapshots = _shards(
        tmp_path, [_transaction("1", "old")],
    )
    digest = snapshots["1"]["filer_transaction_digest"]
    old = _observation("1", fingerprint, digest, surplus=["old"])
    current = _observation(
        "1",
        "sha256:unrelated-global-change",
        digest,
        started=datetime(2026, 9, 3, tzinfo=timezone.utc),
    )
    current["usable_history"] = [old]

    absent, blocked, error = _certify([current], transaction_dir, fingerprint)

    assert absent == {}
    assert blocked == set()
    assert error is None


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda row: row.update(transaction_snapshot_id="sha256:wrong"),
        lambda row: row.update(filer_transaction_digest="sha256:wrong"),
        lambda row: row.update(range_start="2007-01-01"),
        lambda row: row.update(range_end="2026-08-31"),
        lambda row: row.update(
            collection_started_at="2026-09-01T00:00:00Z"
        ),
        lambda row: row.update(complete="false"),
        lambda row: row.update(usable_history=[{
            **row,
            "filer_id": "wrong-owner",
        }]),
    ],
    ids=[
        "wrong-fingerprint",
        "wrong-digest",
        "partial-start",
        "partial-end",
        "stale",
        "malformed",
        "wrong-owner",
    ],
)
def test_invalid_exact_evidence_cannot_suppress_cash(tmp_path, corrupt) -> None:
    transaction_dir, fingerprint, snapshots = _shards(
        tmp_path, [_transaction("1", "old")],
    )
    row = _observation(
        "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
        surplus=["old"],
    )
    corrupt(row)

    absent, blocked, error = _certify([row], transaction_dir, fingerprint)

    assert absent == {}
    assert "1" in blocked
    assert error is None


def test_partial_multi_id_evidence_blocks_the_entire_scope(tmp_path) -> None:
    transaction_dir, fingerprint, snapshots = _shards(tmp_path, [
        _transaction("1", "old-one"),
        _transaction("2", "old-two"),
    ])
    first = _observation(
        "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
        surplus=["old-one"],
    )

    absent, blocked, error = _certify(
        [first], transaction_dir, fingerprint, ids=("1", "2"),
    )

    assert absent == {}
    assert blocked == {"1", "2"}
    assert error is None


def test_mixed_missing_and_surplus_blocks_the_entire_balance_scope(
    tmp_path,
) -> None:
    transaction_dir, fingerprint, snapshots = _shards(tmp_path, [
        _transaction("1", "old-one"),
        _transaction("2", "old-two"),
    ])
    rows = [
        _observation(
            "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
            surplus=["old-one"],
        ),
        _observation(
            "2", fingerprint, snapshots["2"]["filer_transaction_digest"],
            missing=["not-yet-held"],
        ),
    ]

    absent, blocked, error = _certify(
        rows, transaction_dir, fingerprint, ids=("1", "2"),
    )

    assert absent == {}
    assert blocked == {"1", "2"}
    assert error is None


def test_same_filer_missing_and_surplus_cannot_suppress_cash(tmp_path) -> None:
    transaction_dir, fingerprint, snapshots = _shards(
        tmp_path, [_transaction("1", "old")],
    )
    row = _observation(
        "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
        missing=["not-yet-held"], surplus=["old"],
    )

    absent, blocked, error = _certify([row], transaction_dir, fingerprint)

    assert absent == {}
    assert blocked == {"1"}
    assert error is None


def test_valid_common_lane_certifies_only_nonreturning_member_rows(tmp_path) -> None:
    transaction_dir, fingerprint, snapshots = _shards(tmp_path, [
        _transaction("1", "old-one"),
        _transaction("2", "old-two"),
    ])
    rows = [
        _observation(
            "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
            surplus=["old-one"],
        ),
        _observation(
            "2", fingerprint, snapshots["2"]["filer_transaction_digest"],
        ),
    ]

    absent, blocked, error = _certify(
        rows, transaction_dir, fingerprint, ids=("1", "2"),
    )

    assert absent == {"1": {"old-one"}}
    assert P._orestar_absent_for_filers(["1", "2"], absent) == {"old-one"}
    assert blocked == set()
    assert error is None


def test_aggregation_keeps_nonreturning_row_but_omits_it_from_cash_frames(
    tmp_path,
) -> None:
    import generate_activity_snapshot

    data_dir = tmp_path / "data"
    agg_dir = data_dir / "aggregated"
    transaction_dir = data_dir / "transactions"
    agg_dir.mkdir(parents=True)
    transaction_dir.mkdir()
    df = pd.DataFrame({
        "tran_id": ["old"],
        "filed_date": pd.to_datetime(["2026-01-01"]),
        "amount": [100.0],
        "tran_type": ["C"],
        "sub_type": ["Cash Contribution"],
        "contributor_payee": ["Donor"],
        "filer": ["Committee"],
        "filer id": ["1"],
        "year": [2026],
        "month": ["2026-01"],
        "book_type": ["Individual"],
        "is_out_of_state": [False],
        "_undated": [False],
    })
    empty = df.iloc[0:0].copy()
    empty_snapshot = {"meta": {"total_candidates": 0}, "legislative_map": {}}

    with patch.object(P, "DATA_DIR", data_dir), \
         patch.object(P, "AGG_DIR", agg_dir), \
         patch.object(P, "TRANS_DIR", transaction_dir), \
         patch.object(P, "transaction_snapshot_id", return_value="sha256:now"), \
         patch.object(P, "_row_completeness", return_value={}), \
         patch.object(P, "_row_diff", return_value=({}, [])), \
         patch.object(
             P, "_certified_orestar_absent",
             return_value=({"1": {"old"}}, set(), None),
         ), \
         patch.object(P.supabase_sync, "bulk_upsert_filer_detail"), \
         patch.object(P.supabase_sync, "upsert_dashboard_cache"), \
         patch.object(P.supabase_sync, "get_dashboard_cache", return_value={}), \
         patch.object(
             generate_activity_snapshot, "generate", return_value=empty_snapshot,
         ):
        P.aggregate_filers(
            df, df, empty, empty, empty, empty, empty,
            "filer", "contributor_payee",
        )

    [detail_path] = (agg_dir / "filers").glob("*.json")
    detail = json.loads(detail_path.read_text())
    assert detail["cash_on_hand"] == 0.0
    assert detail["total_in"] == 100.0
    assert detail["tran_count"] == 1
    assert detail["orestar_absent"] == {"count": 1, "amount": 100.0}
    assert detail["orestar_withdrawn"] == detail["orestar_absent"]
