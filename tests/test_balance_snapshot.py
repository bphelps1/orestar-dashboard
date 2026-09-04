"""Regression tests for matched ORESTAR/app balance captures."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch


SCRAPER = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER))

from balance_snapshot import (  # noqa: E402
    CAPTURE_KEY,
    build_source,
    evidence_is_current,
    make_summary_capture,
    paired_comparison,
    transaction_snapshot_id,
)


def _source(snapshot_id="sha256:one", filer_ids=None, cash=125.0, count=3):
    return build_source(
        snapshot_id,
        [{
            "filer_ids": filer_ids or ["10"],
            "name": "Committee",
            "slug": "committee",
            "cash_on_hand": cash,
            "tran_count": count,
        }],
        created_at="2026-09-04T12:00:00",
    )


def test_transaction_snapshot_fingerprint_changes_with_shard_bytes(tmp_path):
    assert transaction_snapshot_id(tmp_path) is None
    shard = tmp_path / "txn_2026.csv.gz"
    shard.write_bytes(b"first")
    first = transaction_snapshot_id(tmp_path)
    assert first == transaction_snapshot_id(tmp_path)
    shard.write_bytes(b"second")
    assert transaction_snapshot_id(tmp_path) != first


def test_capture_refuses_stale_app_source():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        _source(), "sha256:two",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_source_stale"


def test_capture_refuses_old_app_calculation_source():
    source = _source()
    source["calculation_version"] = "cash-balance-v0"
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        source, "sha256:one",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_calculation_version"


def test_duplicate_app_scope_is_explicitly_unpairable():
    source = build_source(
        "sha256:one",
        [
            {"filer_ids": ["10"], "name": "First", "slug": "first",
             "cash_on_hand": 100, "tran_count": 1},
            {"filer_ids": ["10"], "name": "Second", "slug": "second",
             "cash_on_hand": 900, "tran_count": 9},
        ],
        created_at="2026-09-04T12:00:00+00:00",
    )
    assert source["scopes"]["10"]["status"] == "ambiguous"
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        source, "sha256:one",
    )
    assert capture["status"] == "unpaired"
    assert capture["reason"] == "app_snapshot_scope_missing_or_ambiguous"


def test_paired_delta_is_immutable_when_app_data_later_changes():
    # The ORESTAR page and the app's $125 balance are frozen together.  A later
    # transaction snapshot does not get reconstructed from filed_date and does
    # not alter the $25 discrepancy.
    source = _source()
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000.25,
        source, "sha256:one",
    )
    comparison = paired_comparison(
        ["10"], {"10": {CAPTURE_KEY: capture}},
        current_transaction_id="sha256:later",
    )
    assert comparison["status"] == "paired"
    assert comparison["app_cash_on_hand"] == 125.0
    assert comparison["orestar_cash_on_hand"] == 100.0
    assert comparison["delta_at_capture"] == 25.0
    assert comparison["app_data_changed_since_capture"] is True


def test_legacy_summary_is_unknown_not_a_live_discrepancy():
    comparison = paired_comparison(
        ["10"], {"10": {"ts": 1000, "years": {"2026": {}}}},
        current_transaction_id="sha256:now",
    )
    assert comparison == {"status": "legacy_unpaired", "reason": "capture_missing"}


def test_multi_filer_scope_requires_every_component_on_same_app_snapshot():
    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    first = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 210}, 1000,
        source, "sha256:one",
    )
    second = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 275}, 1010,
        source, "sha256:one",
    )
    first["scope_capture_id"] = "10|20@test"
    second["scope_capture_id"] = "10|20@test"
    yearly = {"10": {CAPTURE_KEY: first}, "20": {CAPTURE_KEY: second}}
    comparison = paired_comparison(
        ["20", "10"], yearly, current_transaction_id="sha256:one"
    )
    assert comparison["status"] == "paired"
    assert comparison["capture_started_at"] == 1000
    assert comparison["captured_at"] == 1010
    assert comparison["app_cash_on_hand"] == 500
    assert comparison["orestar_cash_on_hand"] == 485
    assert comparison["delta_at_capture"] == 15

    second["app_transaction_snapshot_id"] = "sha256:different"
    assert paired_comparison(["10", "20"], yearly)["reason"] == "component_snapshot_mismatch"


def test_old_calculation_version_is_not_actionable():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        _source(), "sha256:one",
    )
    capture["calculation_version"] = "cash-balance-v0"
    result = paired_comparison(["10"], {"10": {CAPTURE_KEY: capture}})
    assert result == {"status": "unpaired", "reason": "component_snapshot_mismatch"}


def test_old_capture_format_is_not_actionable():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        _source(), "sha256:one",
    )
    capture["version"] = 0
    result = paired_comparison(["10"], {"10": {CAPTURE_KEY: capture}})
    assert result == {"status": "unpaired", "reason": "capture_version_mismatch"}


def test_newer_unpaired_summary_makes_old_pair_refresh_only():
    capture = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 1000,
        _source(), "sha256:one",
    )
    attempt = {
        "status": "unpaired",
        "reason": "app_snapshot_source_stale",
        "captured_at": 2000,
        "orestar_ending_cash_balance": 125,
    }
    result = paired_comparison(
        ["10"],
        {"10": {CAPTURE_KEY: capture, "comparison_capture_attempt": attempt}},
        current_transaction_id="sha256:one",
    )
    assert result["status"] == "paired"
    assert result["delta_at_capture"] == 25
    assert result["orestar_data_changed_since_capture"] is True
    assert result["latest_unpaired_capture_at"] == 2000


def test_current_page_timestamp_is_taken_before_historical_paging():
    import fetch_earliest_balances as feb

    html = "Account Summary Information for the year 2026"

    class NoPrev:
        @property
        def first(self):
            return self

        def count(self):
            return 0

    class Page:
        def locator(self, _selector):
            return NoPrev()

        def content(self):
            return html

    summary = {"ending_cash_balance": 42.5, "beginning_balance": 1.0}
    with patch.object(feb, "_load_summary_page", return_value=html), \
         patch.object(feb, "_parse_yearly_summary", return_value=summary), \
         patch.object(feb, "_parse_beginning_balance", return_value=1.0), \
         patch.object(feb.time, "time", return_value=1234.5):
        earliest, yearly, capture = feb._scrape_filer_earliest(Page(), "10")

    assert capture["captured_at"] == 1234.5
    assert capture["summary"] == summary
    assert yearly["2026"] == summary
    assert earliest["reached_earliest"] is True


def test_heading_only_partial_page_creates_no_capture():
    import fetch_earliest_balances as feb

    html = "Account Summary Information for the year 2026"
    with patch.object(feb, "_load_summary_page", return_value=html):
        earliest, yearly, capture = feb._scrape_filer_earliest(
            object(), "10", current_only=True
        )

    assert earliest is None
    assert yearly == {}
    assert capture is None


def test_required_amount_cannot_be_borrowed_from_the_next_html_row():
    import fetch_earliest_balances as feb
    import orestar_parse

    def row(label, value):
        return f"<tr><td>{label}</td><td>{value}</td></tr>"

    html = "".join([
        row("Beginning Balance (Previous Year)", "$10.00"),
        row("Total Contributions", ""),
        row("Total Expenditures", "$500.00"),
        row("Other Receipts", "$2.00"),
        row("Other Disbursements", "$3.00"),
        row("Balance Adjustments", "$0.00"),
        row("Ending Cash Balance", "$9.00"),
    ])

    assert orestar_parse.parse_dollar(
        html, "Total Contributions", None
    ) is None
    assert feb._parse_yearly_summary(html) is None


def test_missing_optional_loan_value_stays_unknown_not_synthetic_zero():
    import fetch_earliest_balances as feb

    def row(label, value):
        return f"<tr><td>{label}</td><td>{value}</td></tr>"

    html = "".join([
        row("Beginning Balance (Previous Year)", "$10.00"),
        row("Total Contributions", "$2.00"),
        row("Total Expenditures", "$1.00"),
        row("Other Receipts", "$0.00"),
        row("Other Disbursements", "$0.00"),
        row("Balance Adjustments", "$0.00"),
        row("Ending Cash Balance", "$11.00"),
        row("Loans Received (Non-Exempt)", ""),
        row("Loans Received (Exempt)", "$50.00"),
    ])

    summary = feb._parse_yearly_summary(html)
    assert summary is not None
    assert summary["summary_field_version"] == feb.SUMMARY_FIELD_VERSION
    assert summary["loans_received"] is None
    assert summary["loans_received_exempt"] == 50.0


def test_noop_prev_click_does_not_bank_current_year_as_earliest():
    import fetch_earliest_balances as feb

    html = "Account Summary Information for the year 2026"

    class Prev:
        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self):
            return None

    class Page:
        def locator(self, _selector):
            return Prev()

        def content(self):
            return html

    summary = {"beginning_balance": 777.0, "ending_cash_balance": 777.0}
    with patch.object(feb, "_load_summary_page", return_value=html), \
         patch.object(feb, "_parse_yearly_summary", return_value=summary), \
         patch.object(feb, "CHALLENGE_WAIT", 0), \
         patch.object(feb.time, "sleep"):
        earliest, _, _ = feb._scrape_filer_earliest(Page(), "10")

    assert earliest["earliest_year"] == 2026
    assert earliest["reached_earliest"] is False


def test_stale_member_expands_to_complete_multi_filer_scope():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    assert feb._group_ids_by_source_scope(["20"], source) == [["10", "20"]]


def test_overlapping_source_scopes_are_wholly_ineligible():
    import fetch_earliest_balances as feb

    source = {
        "version": feb.FORMAT_VERSION,
        "calculation_version": feb.CALCULATION_VERSION,
        "transaction_snapshot_id": "sha256:one",
        "scopes": {
            "10|20": {"filer_ids": ["10", "20"]},
            "20|30": {"filer_ids": ["20", "30"]},
        },
    }
    assert feb._eligible_source_scopes(source) == {}
    assert feb._snapshot_source_ready(source, "sha256:one") is False


def test_partial_multi_scope_refresh_preserves_last_common_pair():
    import fetch_earliest_balances as feb

    source = _source(filer_ids=["10", "20"], cash=500, count=8)
    old_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 210}, 1000,
        source, "sha256:one",
    )
    old_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 275}, 1010,
        source, "sha256:one",
    )
    old_a["scope_capture_id"] = "10|20@old"
    old_b["scope_capture_id"] = "10|20@old"
    yearly = {
        "10": {"years": {}, CAPTURE_KEY: old_a},
        "20": {"years": {}, CAPTURE_KEY: old_b},
    }

    # First attempt reads A but F5 refuses B; a later attempt does the reverse.
    # Neither can be spliced together into a new authoritative pair.
    new_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 2000,
        source, "sha256:one",
    )
    assert feb._commit_scope_captures(["10", "20"], {"10": new_a}, yearly) is False
    assert yearly["10"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"
    assert yearly["20"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"
    comparison = paired_comparison(["10", "20"], yearly)
    assert comparison["status"] == "paired"
    assert comparison["orestar_data_changed_since_capture"] is True

    new_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 280}, 3000,
        source, "sha256:one",
    )
    assert feb._commit_scope_captures(["10", "20"], {"20": new_b}, yearly) is False
    assert yearly["10"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"
    assert yearly["20"][CAPTURE_KEY]["scope_capture_id"] == "10|20@old"

    # Only one complete group attempt promotes, with a common attempt identity.
    final_a = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 220}, 4000,
        source, "sha256:one",
    )
    final_b = make_summary_capture(
        "20", 2026, {"ending_cash_balance": 280}, 4010,
        source, "sha256:one",
    )
    assert feb._commit_scope_captures(
        ["10", "20"], {"10": final_a, "20": final_b}, yearly
    ) is True
    assert (yearly["10"][CAPTURE_KEY]["scope_capture_id"]
            == yearly["20"][CAPTURE_KEY]["scope_capture_id"])
    assert "comparison_capture_attempt" not in yearly["10"]
    assert "comparison_capture_attempt" not in yearly["20"]


def test_recent_old_version_capture_is_still_requeued():
    import fetch_earliest_balances as feb

    entry = {
        CAPTURE_KEY: {
            "version": 0,
            "calculation_version": "cash-balance-v0",
            "status": "paired",
            "captured_at": 9999,
        }
    }
    assert feb._current_capture_needs_refresh(entry, cutoff=1000) is True


def test_partial_scope_capture_and_newer_attempt_stay_requeued():
    import fetch_earliest_balances as feb

    partial = {
        CAPTURE_KEY: {
            "version": feb.FORMAT_VERSION,
            "calculation_version": feb.CALCULATION_VERSION,
            "status": "unpaired",
            "reason": "scope_capture_incomplete",
            "captured_at": 2000,
        }
    }
    assert feb._current_capture_needs_refresh(partial, cutoff=1000) is True

    paired = make_summary_capture(
        "10", 2026, {"ending_cash_balance": 100}, 2000,
        _source(), "sha256:one",
    )
    with_attempt = {
        CAPTURE_KEY: paired,
        "comparison_capture_attempt": {
            "status": "unpaired",
            "reason": "scope_capture_incomplete",
            "captured_at": 3000,
        },
    }
    assert feb._current_capture_needs_refresh(with_attempt, cutoff=1000) is True


def test_failed_historical_recrawl_preserves_proven_opening_anchor_for_retry():
    import fetch_earliest_balances as feb

    previous = {
        "earliest_year": 2006,
        "beginning_balance": 123.45,
        "reached_earliest": True,
        "ts": 1000,
    }
    failed = {
        "earliest_year": 2021,
        "beginning_balance": 9000.0,
        "reached_earliest": False,
        "ts": 2000,
    }

    merged = feb._merge_earliest_result(previous, failed)
    assert merged["earliest_year"] == 2006
    assert merged["beginning_balance"] == 123.45
    assert merged["reached_earliest"] is True
    # The old timestamp stays old, so the fixed sweep cutoff selects it again.
    assert merged["ts"] < 1500
    assert merged["incomplete_refresh_attempt"] == failed


def test_newer_reported_floor_cannot_erase_proven_older_anchor():
    import fetch_earliest_balances as feb

    previous = {
        "earliest_year": 2006,
        "beginning_balance": 123.45,
        "reached_earliest": True,
        "ts": 1000,
    }
    suspicious = {
        "earliest_year": 2026,
        "beginning_balance": 9999.0,
        "reached_earliest": True,
        "ts": 2000,
    }

    merged = feb._merge_earliest_result(previous, suspicious)
    assert merged["earliest_year"] == 2006
    assert merged["beginning_balance"] == 123.45
    assert merged["ts"] == 1000
    assert merged["inconsistent_refresh_attempt"] == suspicious


def test_empty_or_all_ambiguous_source_cannot_start_current_sweep():
    import fetch_earliest_balances as feb

    empty = build_source(
        "sha256:one", [], created_at="2026-09-04T12:00:00+00:00"
    )
    ambiguous = build_source(
        "sha256:one",
        [
            {"filer_ids": ["10"], "name": "A", "cash_on_hand": 1},
            {"filer_ids": ["10"], "name": "B", "cash_on_hand": 2},
        ],
        created_at="2026-09-04T12:00:00+00:00",
    )
    assert feb._snapshot_source_ready(empty, "sha256:one") is False
    assert feb._snapshot_source_ready(ambiguous, "sha256:one") is False


def test_supporting_count_must_postdate_summary_capture():
    captured = 1_788_220_800  # 2026-09-01T00:00:00Z
    assert evidence_is_current({"checked": "2026-08-31"}, captured) is False
    # Legacy date-only evidence on the same day is midnight and cannot prove it
    # followed a later page read.
    assert evidence_is_current({"checked": "2026-09-01"}, captured + 3600) is False
    assert evidence_is_current(
        {"checked_at": "2026-09-01T02:00:00+00:00"}, captured + 3600
    ) is True
