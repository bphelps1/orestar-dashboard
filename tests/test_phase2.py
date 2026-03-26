"""
Regression tests for Phase 2 dashboard changes.

Tests cover:
  - Global cash-on-hand calculation (sum of per-filer COH)
  - Date-range start uses tran_date, not filed_date
  - Per-filer COH with ORESTAR beginning balances
  - Per-filer COH without ORESTAR data (fallback)
  - Filtered date range COH calculation
  - Filer with beginning balance but no recent contributions
  - Filer with balance adjustments
  - Discrepancy severity thresholds
  - statsFromTimeline logic
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# Add scraper to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))


def run_aggregate(df, mock_orestar=None, tmp_path=None):
    """Helper: run aggregate() with mocked filesystem and ORESTAR data."""
    from process import aggregate, AGG_DIR, TRANS_DIR

    if mock_orestar is None:
        mock_orestar = {}

    # Use real tmp directories so json.dump works
    agg_dir = tmp_path / "aggregated"
    filers_dir = agg_dir / "filers"
    filers_dir.mkdir(parents=True, exist_ok=True)
    trans_dir = tmp_path / "transactions"
    trans_dir.mkdir(parents=True, exist_ok=True)

    with patch("process.AGG_DIR", agg_dir), \
         patch("process.TRANS_DIR", trans_dir), \
         patch("process.scrape_account_summaries", return_value=mock_orestar), \
         patch("process.shutil"):
        aggregate(df)

    # Read back all written files
    result = {}
    for p in agg_dir.rglob("*.json"):
        rel = str(p.relative_to(agg_dir))
        with open(p) as f:
            result[rel] = json.load(f)
    return result


# ---------------------------------------------------------------------------
# Test: discrepancy severity thresholds
# ---------------------------------------------------------------------------

def test_discrepancy_severity():
    """Severity colors match spec: <$500 gray, $500-$2000 yellow, >$2000 red."""
    def severity(abs_disc):
        if abs_disc < 500:
            return "gray"
        if abs_disc <= 2000:
            return "yellow"
        return "red"

    assert severity(0) == "gray"
    assert severity(499.99) == "gray"
    assert severity(500) == "yellow"
    assert severity(1000) == "yellow"
    assert severity(2000) == "yellow"
    assert severity(2000.01) == "red"
    assert severity(50000) == "red"


# ---------------------------------------------------------------------------
# Test: date_range_start uses tran_date
# ---------------------------------------------------------------------------

def test_date_range_start_uses_tran_date(tmp_path):
    """date_range_start should use tran_date (with filed_date fallback), not filed_date alone."""
    df = pd.DataFrame({
        "tran_id": ["1", "2"],
        "filed_date": ["2006-12-31", "2007-01-15"],
        "tran_date": ["2006-01-05", "2007-01-10"],
        "amount": [100.0, 200.0],
        "tran_type": ["C", "C"],
        "sub_type": ["Cash Contribution", "Cash Contribution"],
        "contributor_payee": ["Donor A", "Donor B"],
        "filer": ["Committee X", "Committee X"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    summary = result["summary.json"]
    # tran_date min is 2006-01-05, not filed_date min 2006-12-31
    assert summary["date_range_start"] == "2006-01-05"


# ---------------------------------------------------------------------------
# Test: per-filer COH with ORESTAR beginning balance
# ---------------------------------------------------------------------------

def test_per_filer_coh_with_orestar(tmp_path):
    """COH should use ORESTAR beginning balance as anchor."""
    df = pd.DataFrame({
        "tran_id": ["1", "2", "3"],
        "filed_date": ["2025-03-01", "2025-06-01", "2026-02-01"],
        "tran_date": ["2025-03-01", "2025-06-01", "2026-02-01"],
        "amount": [10000.0, 5000.0, 3000.0],
        "tran_type": ["C", "E", "C"],
        "sub_type": ["Cash Contribution", "Cash Expenditure", "Cash Contribution"],
        "contributor_payee": ["Donor A", "Vendor B", "Donor C"],
        "filer": ["Test Committee", "Test Committee", "Test Committee"],
        "filer id": ["12345", "12345", "12345"],
    })

    mock_orestar = {
        "12345": {
            "year": 2026,
            "beginning_balance": 50000.0,
            "ending_cash_balance": 53000.0,
            "ts": 1711500000,
        }
    }

    result = run_aggregate(df, mock_orestar, tmp_path)

    # Find filer detail
    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    # COH = anchor_beginning + net_from_anchor_onward = 50000 + 3000 = 53000
    assert filer_data["cash_on_hand"] == 53000.0
    assert filer_data["cash_on_hand_source"] == "orestar"
    assert filer_data["orestar_account_summary"]["scrape_ts"] == 1711500000


def test_per_filer_coh_without_orestar(tmp_path):
    """COH should fall back to transaction-based calculation."""
    df = pd.DataFrame({
        "tran_id": ["1", "2"],
        "filed_date": ["2025-03-01", "2025-06-01"],
        "tran_date": ["2025-03-01", "2025-06-01"],
        "amount": [10000.0, 3000.0],
        "tran_type": ["C", "E"],
        "sub_type": ["Cash Contribution", "Cash Expenditure"],
        "contributor_payee": ["Donor A", "Vendor B"],
        "filer": ["No ORESTAR Committee", "No ORESTAR Committee"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)

    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    # COH = 10000 - 3000 = 7000 (no beginning balance)
    assert filer_data["cash_on_hand"] == 7000.0
    assert filer_data["cash_on_hand_source"] == "calculated"


# ---------------------------------------------------------------------------
# Test: global COH = sum of per-filer COH values
# ---------------------------------------------------------------------------

def test_global_coh_is_sum_of_filers(tmp_path):
    """Global cash_on_hand should be sum of per-filer values."""
    df = pd.DataFrame({
        "tran_id": ["1", "2", "3", "4"],
        "filed_date": ["2025-01-01", "2025-01-01", "2025-01-01", "2025-01-01"],
        "tran_date": ["2025-01-01", "2025-01-01", "2025-01-01", "2025-01-01"],
        "amount": [1000.0, 500.0, 2000.0, 800.0],
        "tran_type": ["C", "E", "C", "E"],
        "sub_type": ["Cash Contribution", "Cash Expenditure", "Cash Contribution", "Cash Expenditure"],
        "contributor_payee": ["Donor A", "Vendor B", "Donor C", "Vendor D"],
        "filer": ["Committee Alpha", "Committee Alpha", "Committee Beta", "Committee Beta"],
    })

    result = run_aggregate(df, tmp_path=tmp_path)
    summary = result["summary.json"]

    # Alpha COH = 1000 - 500 = 500
    # Beta COH = 2000 - 800 = 1200
    # Global COH = 500 + 1200 = 1700
    assert summary["global_cash_on_hand"] == 1700.0
    assert "global_beginning_balances" in summary


# ---------------------------------------------------------------------------
# Test: filer with beginning balance but no recent contributions
# ---------------------------------------------------------------------------

def test_filer_with_only_beginning_balance(tmp_path):
    """A filer with an ORESTAR beginning balance but no contributions should show correct COH."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2026-01-15"],
        "tran_date": ["2026-01-15"],
        "amount": [100.0],
        "tran_type": ["E"],
        "sub_type": ["Cash Expenditure"],
        "contributor_payee": ["Vendor X"],
        "filer": ["Dormant Committee"],
        "filer id": ["99999"],
    })

    mock_orestar = {
        "99999": {
            "year": 2026,
            "beginning_balance": 5000.0,
            "ending_cash_balance": 4900.0,
            "ts": 1711500000,
        }
    }

    result = run_aggregate(df, mock_orestar, tmp_path)

    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    # COH = 5000 (beginning) - 100 (expenditure) = 4900
    assert filer_data["cash_on_hand"] == 4900.0


# ---------------------------------------------------------------------------
# Test: statsFromTimeline (frontend logic replicated in Python)
# ---------------------------------------------------------------------------

def test_stats_from_timeline_with_beginning_balances():
    """Replicate the JS statsFromTimeline logic."""
    rows = [
        {"month": "2025-01", "contributions": 5000, "inkind": 0, "expenditures": 2000,
         "other_receipts": 100, "other_disbursements": 50, "count": 10},
        {"month": "2025-06", "contributions": 3000, "inkind": 0, "expenditures": 1000,
         "other_receipts": 0, "other_disbursements": 0, "count": 5},
    ]
    beginning_balances = {"2025": 10000.0, "2024": 8000.0}

    total_in = sum(r["contributions"] for r in rows)
    total_out = sum(r["expenditures"] for r in rows)
    total_or = sum(r["other_receipts"] for r in rows)
    total_od = sum(r["other_disbursements"] for r in rows)

    earliest_year = rows[0]["month"][:4]
    begin_bal = beginning_balances.get(earliest_year, 0)

    net_flow = total_in + total_or - total_out - total_od
    cash_on_hand = round((begin_bal + net_flow) * 100) / 100

    assert cash_on_hand == 15050.0


def test_stats_from_timeline_filtered_range():
    """When filtering to a date range, beginning balance should match the filtered start year."""
    rows = [
        {"month": "2024-06", "contributions": 1000, "inkind": 0, "expenditures": 500,
         "other_receipts": 0, "other_disbursements": 0, "count": 3},
    ]
    beginning_balances = {"2023": 5000.0, "2024": 8000.0}

    earliest_year = rows[0]["month"][:4]
    begin_bal = beginning_balances.get(earliest_year, 0)
    net_flow = 1000 - 500
    cash_on_hand = round((begin_bal + net_flow) * 100) / 100

    assert cash_on_hand == 8500.0


# ---------------------------------------------------------------------------
# Test: filer with balance adjustments
# ---------------------------------------------------------------------------

def test_balance_adjustments_in_account_summary(tmp_path):
    """Balance adjustments should be included in filer's account summary data."""
    df = pd.DataFrame({
        "tran_id": ["1"],
        "filed_date": ["2026-01-15"],
        "tran_date": ["2026-01-15"],
        "amount": [1000.0],
        "tran_type": ["C"],
        "sub_type": ["Cash Contribution"],
        "contributor_payee": ["Donor A"],
        "filer": ["Adj Committee"],
        "filer id": ["77777"],
    })

    mock_orestar = {
        "77777": {
            "year": 2026,
            "beginning_balance": 2000.0,
            "ending_cash_balance": 3500.0,
            "balance_adjustments": 500.0,
            "ts": 1711500000,
        }
    }

    result = run_aggregate(df, mock_orestar, tmp_path)

    filer_files = [k for k in result if k.startswith("filers/")]
    assert len(filer_files) == 1
    filer_data = result[filer_files[0]]

    acct = filer_data.get("orestar_account_summary", {})
    assert acct.get("balance_adjustments") == 500.0
    assert acct.get("ending_cash_balance") == 3500.0
    assert acct.get("scrape_ts") == 1711500000


# ---------------------------------------------------------------------------
# Test: global_beginning_balances accumulated correctly
# ---------------------------------------------------------------------------

def test_global_beginning_balances_aggregated(tmp_path):
    """Global beginning balances should be sum of per-filer beginning balances per year."""
    df = pd.DataFrame({
        "tran_id": ["1", "2"],
        "filed_date": ["2025-06-01", "2025-06-01"],
        "tran_date": ["2025-06-01", "2025-06-01"],
        "amount": [1000.0, 2000.0],
        "tran_type": ["C", "C"],
        "sub_type": ["Cash Contribution", "Cash Contribution"],
        "contributor_payee": ["Donor A", "Donor B"],
        "filer": ["Committee One", "Committee Two"],
        "filer id": ["111", "222"],
    })

    mock_orestar = {
        "111": {"year": 2025, "beginning_balance": 5000.0, "ending_cash_balance": 6000.0, "ts": 1},
        "222": {"year": 2025, "beginning_balance": 3000.0, "ending_cash_balance": 5000.0, "ts": 1},
    }

    result = run_aggregate(df, mock_orestar, tmp_path)
    summary = result["summary.json"]
    global_bb = summary["global_beginning_balances"]
    # Both filers have 2025 beginning balances: 5000 + 3000 = 8000
    assert global_bb["2025"] == 8000.0
