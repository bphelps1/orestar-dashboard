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
from audit_consistency import audit_filer  # noqa: E402
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


@pytest.mark.parametrize("new_missing", [(), ("new",)])
def test_newer_full_history_range_revokes_older_surplus(
    tmp_path,
    new_missing,
) -> None:
    transaction_dir, fingerprint, snapshots = _shards(
        tmp_path, [_transaction("1", "old")],
    )
    old = _observation(
        "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
        surplus=["old"],
    )
    later_end = END + timedelta(days=1)
    later_digest = transaction_filer_snapshots(
        transaction_dir, ["1"], START, later_end,
    )["1"]["filer_transaction_digest"]
    current = _observation(
        "1",
        "sha256:new-global-snapshot",
        later_digest,
        started=datetime(2026, 9, 3, tzinfo=timezone.utc),
        missing=new_missing,
    )
    current["range_end"] = later_end.isoformat()
    current["usable_history"] = [old]

    absent, blocked, error = _certify([current], transaction_dir, fingerprint)

    assert absent == {}
    assert blocked == {"1"}
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
        lambda row: row.pop("held"),
        lambda row: row.update(held=2, orestar=1),
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
        "missing-count",
        "held-count-does-not-match-current-set",
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


def test_claimed_missing_id_that_is_already_held_blocks_surplus(tmp_path) -> None:
    transaction_dir, fingerprint, snapshots = _shards(tmp_path, [
        _transaction("1", "held"),
        _transaction("1", "claimed-surplus"),
    ])
    row = _observation(
        "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
        missing=["held"], surplus=["claimed-surplus"],
    )
    row.update(held=2, orestar=2)

    absent, blocked, error = _certify([row], transaction_dir, fingerprint)

    assert absent == {}
    assert blocked == {"1"}
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


def test_unpaired_overlapping_scope_cannot_receive_paired_omissions(
    tmp_path,
) -> None:
    transaction_dir, fingerprint, snapshots = _shards(tmp_path, [
        _transaction("1", "old-one"),
        _transaction("2", "held-two"),
    ])
    row = _observation(
        "1", fingerprint, snapshots["1"]["filer_transaction_digest"],
        surplus=["old-one"],
    )

    absent, blocked, error = P._certified_orestar_absent(
        [row],
        {
            "Paired": ["1"],
            "Unpaired overlap": ["1", "2"],
        },
        {
            "Paired": _comparison(["1"], fingerprint),
            "Unpaired overlap": {"status": "unpaired"},
        },
        transaction_dir,
    )

    assert absent == {}
    assert "1" in blocked
    assert P._orestar_absent_for_filers(["1", "2"], absent) == set()
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
        global_data = P.aggregate_filers(
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
    # The filing remains visible as history even though it no longer moves
    # the balance ORESTAR reports.
    assert sum(
        row.get("contributions", 0) for row in detail["timeline"]
    ) == 100.0
    assert sum(
        row.get("cash_balance_net", 0)
        for row in detail["timeline"]
    ) == 0.0
    assert sum(global_data["global_cash_timeline"].values()) == 0.0


def _cash_row(
    tran_id: str,
    amount: float,
    tran_type: str,
    sub_type: str,
) -> dict:
    return {
        "tran_id": tran_id,
        "filed_date": pd.Timestamp("2026-01-01"),
        "amount": amount,
        "tran_type": tran_type,
        "sub_type": sub_type,
        "contributor_payee": "Counterparty",
        "filer": "Committee",
        "filer id": "1",
        "year": 2026,
        "month": "2026-01",
        "book_type": "Individual",
        "is_out_of_state": False,
        "_undated": False,
    }


def _aggregate_cash_rows(
    tmp_path: Path,
    rows: list[dict],
    absent_ids: set[str],
    yearly: dict | None = None,
) -> dict:
    import generate_activity_snapshot

    data_dir = tmp_path / "data"
    agg_dir = data_dir / "aggregated"
    transaction_dir = data_dir / "transactions"
    agg_dir.mkdir(parents=True)
    transaction_dir.mkdir()
    if yearly is not None:
        (data_dir / "orestar_yearly_summaries.json").write_text(
            json.dumps(yearly)
        )

    df = pd.DataFrame(rows)
    contributions = df[df["tran_type"] == "C"].copy()
    inkind = contributions.iloc[0:0].copy()
    expenditures = df[df["tran_type"] == "E"].copy()
    other_receipts = df[df["tran_type"] == "OR"].copy()
    other_disbursements = df[df["tran_type"] == "OD"].copy()
    balance_adjustments = df[
        df["sub_type"] == "Cash Balance Adjustment"
    ].copy()
    empty_snapshot = {"meta": {"total_candidates": 0}, "legislative_map": {}}

    with patch.object(P, "DATA_DIR", data_dir), \
         patch.object(P, "AGG_DIR", agg_dir), \
         patch.object(P, "TRANS_DIR", transaction_dir), \
         patch.object(P, "transaction_snapshot_id", return_value="sha256:now"), \
         patch.object(P, "_row_completeness", return_value={}), \
         patch.object(P, "_row_diff", return_value=({}, [])), \
         patch.object(
             P, "_certified_orestar_absent",
             return_value=({"1": absent_ids}, set(), None),
         ), \
         patch.object(P.supabase_sync, "bulk_upsert_filer_detail"), \
         patch.object(P.supabase_sync, "upsert_dashboard_cache"), \
         patch.object(P.supabase_sync, "get_dashboard_cache", return_value={}), \
         patch.object(
             generate_activity_snapshot, "generate", return_value=empty_snapshot,
         ):
        P.aggregate_filers(
            df,
            contributions,
            inkind,
            expenditures,
            other_receipts,
            other_disbursements,
            balance_adjustments,
            "filer",
            "contributor_payee",
        )

    [detail_path] = (agg_dir / "filers").glob("*.json")
    return json.loads(detail_path.read_text())


def _timeline_net(detail: dict) -> float:
    def row_net(row: dict) -> float:
        value = row.get("cash_balance_net")
        if isinstance(value, (int, float)):
            return float(value)
        return (
            row.get("contributions", 0)
            + row.get("other_receipts", 0)
            + row.get("balance_adjustments", 0)
            - row.get("expenditures", 0)
            - row.get("other_disbursements", 0)
        )

    return round(sum(
        row_net(row)
        for row in detail["timeline"]
    ), 2)


def test_absent_cash_expenditure_is_omitted_from_stored_and_timeline_cash(
    tmp_path,
) -> None:
    detail = _aggregate_cash_rows(
        tmp_path,
        [_cash_row("old-expenditure", 100.0, "E", "Cash Expenditure")],
        {"old-expenditure"},
    )

    assert detail["cash_on_hand"] == 0.0
    assert detail["total_out"] == 100.0
    assert detail["timeline"][0]["expenditures"] == 100.0
    assert detail["timeline"][0]["cash_balance_net"] == 0.0
    assert _timeline_net(detail) == 0.0


def test_absent_other_receipt_stays_filtered_when_exempt_loan_is_removed(
    tmp_path,
) -> None:
    summary = {
        "beginning_balance": 0.0,
        "contributions": 0.0,
        "expenditures": 1.0,
        "other_receipts": 1.0,
        "other_disbursements": 0.0,
        "balance_adjustments": 0.0,
        "ending_cash_balance": 0.0,
        "loans_received": 0.0,
        "loans_received_exempt": 0.0,
        "loan_payments": 0.0,
        "loan_payments_exempt": 0.0,
        "summary_field_version": 2,
        "scrape_ts": 1_800_000_000.0,
    }
    detail = _aggregate_cash_rows(
        tmp_path,
        [
            _cash_row("old-receipt", 100.0, "OR", "Miscellaneous Other Receipt"),
            _cash_row("exempt-loan", 500.0, "OR", "Loan Received (Exempt)"),
        ],
        {"old-receipt"},
        {"1": {"years": {"2026": summary}, "ts": 1_800_000_000.0}},
    )

    assert detail["cash_on_hand"] == 0.0
    assert detail["exempt_loans_excluded"] == {"2026": 500.0}
    assert detail["orestar_absent"] == {"count": 1, "amount": 100.0}
    assert detail["total_or"] == 600.0
    assert detail["timeline"][0]["other_receipts"] == 600.0
    assert _timeline_net(detail) == 0.0


@pytest.mark.parametrize(
    ("tran_type", "sub_type", "timeline_field"),
    [
        ("OR", "Miscellaneous Other Receipt", "other_receipts"),
        ("OD", "Miscellaneous Other Disbursement", "other_disbursements"),
        ("O", "Cash Balance Adjustment", "balance_adjustments"),
    ],
)
def test_absent_other_cash_components_stay_visible_but_do_not_move_cash(
    tmp_path,
    tran_type,
    sub_type,
    timeline_field,
) -> None:
    detail = _aggregate_cash_rows(
        tmp_path,
        [_cash_row("old-component", 100.0, tran_type, sub_type)],
        {"old-component"},
    )

    assert detail["cash_on_hand"] == 0.0
    assert detail["timeline"][0][timeline_field] == 100.0
    assert detail["timeline"][0]["cash_balance_net"] == 0.0


def test_absent_nonexempt_loan_is_not_subtracted_twice(tmp_path) -> None:
    summary = {
        "beginning_balance": 0.0,
        "contributions": 50.0,
        "expenditures": 0.0,
        "other_receipts": 0.0,
        "other_disbursements": 0.0,
        "balance_adjustments": 0.0,
        "ending_cash_balance": 50.0,
        "loans_received": 0.0,
        "loans_received_exempt": 0.0,
        "loan_payments": 0.0,
        "loan_payments_exempt": 0.0,
        "summary_field_version": 2,
        "scrape_ts": 1_800_000_000.0,
    }
    detail = _aggregate_cash_rows(
        tmp_path,
        [
            _cash_row("cash", 50.0, "C", "Cash Contribution"),
            _cash_row("old-loan", 100.0, "C", "Loan Received (Non-Exempt)"),
        ],
        {"old-loan"},
        {"1": {"years": {"2026": summary}, "ts": 1_800_000_000.0}},
    )

    assert detail["cash_on_hand"] == 50.0
    assert detail["timeline"][0]["contributions"] == 150.0
    assert detail["timeline"][0]["loans_received"] == 100.0
    assert detail["timeline"][0]["cash_balance_net"] == 50.0
    assert _timeline_net(detail) == 50.0


def test_absent_nonexempt_loan_payment_stays_visible_but_not_in_cash(
    tmp_path,
) -> None:
    detail = _aggregate_cash_rows(
        tmp_path,
        [_cash_row(
            "old-loan-payment", 100.0, "E", "Loan Payment (Non-Exempt)",
        )],
        {"old-loan-payment"},
    )

    assert detail["cash_on_hand"] == 0.0
    assert detail["total_out"] == 100.0
    assert detail["timeline"][0]["expenditures"] == 100.0
    assert detail["timeline"][0]["loan_payments"] == 100.0
    assert detail["timeline"][0]["cash_balance_net"] == 0.0


def test_reported_loan_without_held_denominator_is_not_synthesized(
    tmp_path,
) -> None:
    summary = {
        "beginning_balance": 0.0,
        "contributions": 150.0,
        "expenditures": 0.0,
        "other_receipts": 0.0,
        "other_disbursements": 0.0,
        "balance_adjustments": 0.0,
        "ending_cash_balance": 150.0,
        "loans_received": 100.0,
        "loans_received_exempt": 0.0,
        "loan_payments": 0.0,
        "loan_payments_exempt": 0.0,
        "summary_field_version": 2,
        "scrape_ts": 1_800_000_000.0,
    }
    detail = _aggregate_cash_rows(
        tmp_path,
        [_cash_row("cash", 50.0, "C", "Cash Contribution")],
        set(),
        {"1": {"years": {"2026": summary}, "ts": 1_800_000_000.0}},
    )

    # A summary line is a validation signal, not a transaction. With no held
    # loan row there is no date or identity to add safely, so remediation must
    # supply the missing row before it can move cash.
    assert detail["cash_on_hand"] == 50.0
    assert sum(row["loans_received"] for row in detail["timeline"]) == 0.0
    assert _timeline_net(detail) == 50.0
    assert detail["yearly_discrepancies"]["2026"]["our_contributions"] == 50.0


@pytest.mark.parametrize(
    ("held_amounts", "reported", "expected_cash"),
    [
        ([1.0, 1.0, 1.0], 1.0, [0.34, 0.33, 0.33]),
        ([1.0, 1.0], 0.01, [0.01, 0.0]),
        ([1.0] * 12, 0.07, [0.01] * 7 + [0.0] * 5),
        ([1.0, 2.0, 3.0], 1.0, [0.17, 0.33, 0.5]),
        ([2.0, -1.0], 0.5, [1.0, -0.5]),
        ([1.0, 2.0], 0.0, [0.0, 0.0]),
    ],
)
def test_fractional_monthly_loan_scaling_reconciles_to_annual_cash(
    tmp_path,
    held_amounts,
    reported,
    expected_cash,
) -> None:
    rows = []
    for index, amount in enumerate(held_amounts, start=1):
        row = _cash_row(
            f"loan-{index}", amount, "C", "Loan Received (Non-Exempt)",
        )
        row["filed_date"] = pd.Timestamp(year=2026, month=index, day=1)
        row["month"] = f"2026-{index:02d}"
        rows.append(row)
    summary = {
        "beginning_balance": 0.0,
        "contributions": reported,
        "expenditures": 0.0,
        "other_receipts": 0.0,
        "other_disbursements": 0.0,
        "balance_adjustments": 0.0,
        "ending_cash_balance": reported,
        "loans_received": reported,
        "loans_received_exempt": 0.0,
        "loan_payments": 0.0,
        "loan_payments_exempt": 0.0,
        "summary_field_version": 2,
        "scrape_ts": 1_800_000_000.0,
    }

    detail = _aggregate_cash_rows(
        tmp_path,
        rows,
        set(),
        {"1": {"years": {"2026": summary}, "ts": 1_800_000_000.0}},
    )

    assert detail["cash_on_hand"] == reported
    assert _timeline_net(detail) == reported
    # Historical components report the held transactions; only cash uses the
    # ORESTAR-normalized annual amount.
    assert sum(row["loans_received"] for row in detail["timeline"]) == sum(
        held_amounts
    )
    assert sum(row["contributions"] for row in detail["timeline"]) == sum(
        held_amounts
    )
    actual_cash = [row["cash_balance_net"] for row in detail["timeline"]]
    assert actual_cash == expected_cash
    assert all(
        held * allocated >= 0
        for held, allocated in zip(held_amounts, actual_cash)
    )
    assert detail["nonexempt_loan_cash_treatment"] == {
        "2026": {
            "transaction_total": sum(held_amounts),
            "orestar_counted": reported,
        }
    }
    scale = reported / sum(held_amounts)
    assert sum(
        row.get("cash_balance_rounding_adjustment", 0)
        for row in detail["timeline"]
    ) == pytest.approx(
        reported - sum(round(amount * scale, 2) for amount in held_amounts)
    )


def test_frontend_cash_contract_prefers_numeric_zero_and_keeps_legacy_fallback(
) -> None:
    def browser_row_net(row: dict) -> float:
        value = row.get("cash_balance_net")
        if isinstance(value, (int, float)):
            return float(value)
        return (
            row.get("contributions", 0)
            + row.get("other_receipts", 0)
            + row.get("balance_adjustments", 0)
            - row.get("expenditures", 0)
            - row.get("other_disbursements", 0)
        )

    assert browser_row_net({
        "contributions": 100.0,
        "cash_balance_net": 0.0,
    }) == 0.0
    assert browser_row_net({"contributions": 100.0}) == 100.0

    def browser_cash_position(rows, beginning_balances, through_month):
        exact_schema = bool(rows) and all(
            isinstance(row.get("cash_balance_net"), (int, float))
            for row in rows
        )
        years = sorted(beginning_balances)
        first_year = years[0] if years else ""
        anchor_month = f"{first_year}-01" if first_year else ""
        position = (
            beginning_balances.get(first_year, 0.0)
            if (not anchor_month or not through_month
                or anchor_month <= through_month)
            else 0.0
        )
        for row in rows:
            month = row.get("month")
            if not month:
                continue
            if anchor_month and month < anchor_month:
                continue
            if through_month and month > through_month:
                break
            if exact_schema:
                position += row["cash_balance_net"]
            else:
                legacy_row = dict(row)
                legacy_row.pop("cash_balance_net", None)
                position += browser_row_net(legacy_row)
        return round(position, 2)

    # Empty selected ranges still report the position at their endpoint.
    assert browser_cash_position(
        [{"month": "2024-01", "cash_balance_net": 100.0}], {}, "2026-12",
    ) == 100.0
    # A late official opening statement supersedes older retained history.
    late_anchor = [
        {"month": "2025-01", "cash_balance_net": 0.0},
        {"month": "2026-06", "cash_balance_net": 3_000.0},
    ]
    assert browser_cash_position(late_anchor, {"2026": 50_000.0}, "2025-12") == 0.0
    assert browser_cash_position(late_anchor, {"2026": 50_000.0}, "2026-12") == 53_000.0
    # A partial-year view rolls forward from January, stops at its endpoint,
    # and does not accidentally include later rows.
    assert browser_cash_position(
        [
            {"month": "2024-01", "cash_balance_net": 50.0},
            {"month": "2024-07", "cash_balance_net": -10.0},
            {"month": "2024-11", "cash_balance_net": 20.0},
        ],
        {"2024": 100.0},
        "2024-07",
    ) == 140.0
    # An anchor is carried through an otherwise empty later period.
    assert browser_cash_position([], {"2008": 100.0}, "2026-12") == 100.0
    # A partially cached global file must not count an embedded new anchor and
    # the legacy separate anchor at the same time.
    assert browser_cash_position(
        [
            {"month": "2025-01", "cash_balance_net": 100.0},
            {"month": "2026-01", "contributions": 10.0},
        ],
        {"2025": 100.0},
        None,
    ) == 110.0

    app_js = (Path(__file__).parent.parent / "docs" / "app.js").read_text()
    assert "function timelineCashNet(row)" in app_js
    assert 'typeof row.cash_balance_net === "number"' in app_js
    assert "function timelineCashPosition(rows, beginningBalances, throughMonth)" in app_js
    assert "const exactCashSchema = timelineCashSchemaIsComplete(rows);" in app_js
    assert "? timelineCashNet(row)" in app_js
    assert ": timelineLegacyCashNet(row);" in app_js
    assert "const cashOnHand = timelineCashPosition(" in app_js
    assert "rows.every(row =>" in app_js
    assert "timelineCashSchemaIsComplete(fullGlobalTl)" in app_js
    assert "hasDate ? activeCashThroughMonth() : null" in app_js
    assert "endingCalc = timelineCashPosition(" in app_js
    assert (
        "if (!timeline.length && "
        "!Object.keys(profile.beginning_balances || {}).length) return null;"
    ) in app_js
    assert "syncTitle + (treatmentText" in app_js
    assert "function cashTreatmentNoteText(profile)" in app_js
    assert "compute: d => d.cash_treatment_adjustment" in app_js
    assert '"cash_treatment_adjustment", "net_cash_flow"' in app_js
    assert "compute: d => d.net_cash_flow" in app_js
    assert "compute: d => d.ending_cash_balance" in app_js


def test_consistency_audit_uses_one_cash_contract_for_the_whole_timeline() -> None:
    exact_detail = {
        "name": "Exact cash",
        "total_in": 100.0,
        "total_out": 0.0,
        "cash_on_hand": 0.0,
        "beginning_balances": {"2025": 0.0, "2026": 0.0},
        "timeline": [
            {
                "month": "2025-01",
                "contributions": 100.0,
                "cash_balance_net": 0.0,
            },
            {"month": "2026-01", "cash_balance_net": 0.0},
        ],
    }
    assert audit_filer("exact-cash", exact_detail) == []

    mixed_detail = {
        "name": "Mixed cache",
        "total_in": 10.0,
        "total_out": 0.0,
        "cash_on_hand": 110.0,
        "beginning_balances": {"2025": 100.0},
        "timeline": [
            {"month": "2025-01", "cash_balance_net": 100.0},
            {"month": "2026-01", "contributions": 10.0},
        ],
    }
    assert audit_filer("mixed-cache", mixed_detail) == []

    legacy_adjustment = {
        "name": "Legacy adjustment",
        "total_in": 0.0,
        "total_out": 0.0,
        "cash_on_hand": 105.0,
        "beginning_balances": {"2025": 100.0},
        "timeline": [
            {"month": "2025-01", "balance_adjustments": 5.0},
        ],
    }
    assert audit_filer("legacy-adjustment", legacy_adjustment) == []
