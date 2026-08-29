"""Focused regression tests for coverage-diff refusal hardening.

These tests exercise only pure ordering/parsing/state helpers.  They make no
ORESTAR requests and do not require a database.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import diff_coverage as DC  # noqa: E402
import process as P  # noqa: E402


def _ids(targets: list[dict]) -> list[str]:
    return [str(target["filer_id"]) for target in targets]


def _assert_attempt_is_today(value: str) -> None:
    """Accept either a date or an ISO datetime for diagnostic timestamps."""
    assert datetime.fromisoformat(value).date() == date.today()


def test_split_can_divide_a_two_day_window() -> None:
    start = date(2026, 8, 27)
    end = start + timedelta(days=1)

    assert DC._split(start, end) == [(start, start), (end, end)]


def test_parse_rows_accepts_accounting_parentheses_for_negative_amounts() -> None:
    text = (
        "3865001\t08/27/2026\tOriginal\tTest Committee\tState Bank\t"
        "Cash Balance Adjustment\t($1,268.50)"
    )

    assert DC._parse_rows(text) == {
        "3865001": {
            "date": "08/27/2026",
            "status": "Original",
            "payee": "State Bank",
            "sub_type": "Cash Balance Adjustment",
            "amount": -1268.50,
        }
    }


def test_prioritise_skips_a_usable_surplus_checked_today() -> None:
    targets = [
        {"filer_id": "recent", "name": "Recent surplus"},
        {"filer_id": "fresh", "name": "Never measured"},
    ]
    entries = {
        "recent": {
            "filer_id": "recent",
            "complete": False,
            "surplus": ["withdrawn-1"],
            "missing": [],
            "checked": date.today().isoformat(),
        }
    }

    ordered = _ids(DC._prioritise(targets, entries))

    assert "recent" not in ordered
    assert ordered == ["fresh"]


def test_prioritise_interleaves_null_failures_with_fresh_targets() -> None:
    targets = [
        {"filer_id": "failed-1", "name": "Failed one"},
        {"filer_id": "failed-2", "name": "Failed two"},
        {"filer_id": "fresh-1", "name": "Fresh one"},
        {"filer_id": "fresh-2", "name": "Fresh two"},
        {"filer_id": "usable", "name": "Usable clean"},
    ]
    entries = {
        # A legacy null may carry `checked`; use it as attempt timing without
        # ever treating it as usable evidence.
        "failed-1": {
            "filer_id": "failed-1",
            "complete": None,
            "checked": (date.today() - timedelta(days=2)).isoformat(),
        },
        "failed-2": {
            "filer_id": "failed-2",
            "complete": None,
            "last_attempt": (date.today() - timedelta(days=1)).isoformat(),
        },
        "usable": {
            "filer_id": "usable",
            "complete": True,
            "surplus": [],
            "missing": [],
            "checked": (date.today() - timedelta(days=1)).isoformat(),
        },
    }

    ordered = _ids(DC._prioritise(targets, entries))
    failure_positions = [ordered.index(fid) for fid in ("failed-1", "failed-2")]
    fresh_positions = [ordered.index(fid) for fid in ("fresh-1", "fresh-2")]

    assert set(ordered[:4]) == {"failed-1", "failed-2", "fresh-1", "fresh-2"}
    # Their position ranges overlap: neither category is exhausted before the
    # other begins.  This specifies interleaving without overfitting its ratio.
    assert min(failure_positions) < max(fresh_positions)
    assert min(fresh_positions) < max(failure_positions)
    assert ordered[-1] == "usable"


def test_prioritise_defers_a_failure_already_attempted_today() -> None:
    targets = [
        {"filer_id": "failed-today", "name": "Failed today"},
        {"filer_id": "fresh", "name": "Never measured"},
    ]
    entries = {
        "failed-today": {
            "filer_id": "failed-today",
            "complete": None,
            "last_attempt": date.today().isoformat(),
            "last_failure": "unusable_window",
        }
    }

    assert _ids(DC._prioritise(targets, entries)) == ["fresh"]


def test_record_failure_preserves_existing_usable_evidence() -> None:
    checked = (date.today() - timedelta(days=2)).isoformat()
    entries = {
        "19050": {
            "filer_id": "19050",
            "name": "Friends of Christine Drazan",
            "orestar": 4300,
            "held": 4273,
            "complete": False,
            "surplus": ["withdrawn-1", "withdrawn-2"],
            "missing": ["missing-1"],
            "superseded": ["superseded-1"],
            "checked": checked,
            "failure_count": 2,
        }
    }
    reason = "collected 42 of 97 rows"

    DC._record_failure(
        entries,
        {"filer_id": "19050", "name": "Friends of Christine Drazan"},
        reason,
    )

    saved = entries["19050"]
    assert saved["complete"] is False
    assert saved["surplus"] == ["withdrawn-1", "withdrawn-2"]
    assert saved["missing"] == ["missing-1"]
    assert saved["superseded"] == ["superseded-1"]
    assert saved["checked"] == checked
    assert saved["last_failure"] == reason
    assert saved["failure_count"] == 3
    _assert_attempt_is_today(saved["last_attempt"])


def test_record_failure_creates_an_unchecked_null_for_first_failure() -> None:
    entries: dict[str, dict] = {}
    reason = "results count could not be read"

    DC._record_failure(
        entries,
        {"filer_id": "24173", "name": "New committee"},
        reason,
    )

    saved = entries["24173"]
    assert saved["filer_id"] == "24173"
    assert saved["name"] == "New committee"
    assert saved["complete"] is None
    assert "checked" not in saved
    assert saved["last_failure"] == reason
    assert saved["failure_count"] == 1
    _assert_attempt_is_today(saved["last_attempt"])


def test_row_diff_keeps_null_as_an_explicit_override(monkeypatch, tmp_path) -> None:
    rows = [
        {
            "filer_id": "failed-with-date",
            "complete": None,
            "checked": "2026-08-28",
        },
        {
            "filer_id": "failed-without-date",
            "complete": None,
            "last_attempt": "2026-08-28T12:34:56",
        },
        {
            "filer_id": "usable-surplus",
            "complete": False,
            "checked": "2026-08-27",
            "surplus": ["11", "12"],
        },
        {
            "filer_id": "usable-clean",
            "complete": True,
            "checked": "2026-08-26",
            "surplus": [],
        },
    ]
    (tmp_path / "coverage_diff.json").write_text(json.dumps(rows))
    monkeypatch.setattr(P, "DATA_DIR", tmp_path)

    complete, withdrawn = P._row_diff()

    assert complete["failed-with-date"] == (None, date(2026, 8, 28))
    assert complete["failed-without-date"] == (None, None)
    assert complete["usable-surplus"] == (False, date(2026, 8, 27))
    assert complete["usable-clean"] == (True, date(2026, 8, 26))
    assert withdrawn == {"usable-surplus": {"11", "12"}}


def _page_text(start: int, count: int) -> str:
    return "\n".join(
        f"{i}\t08/28/2026\tOriginal\tTest Committee\tPayee {i}\t"
        f"Cash Contribution\t$1.00"
        for i in range(start, start + count)
    )


class _NextButton:
    def __init__(self, page) -> None:
        self.page = page

    def is_enabled(self) -> bool:
        return True

    def click(self) -> None:
        self.page.clicked = True


class _DelayedNextPage:
    """First page remains visible for one poll after Next is clicked."""

    def __init__(self, first: str, second: str | None) -> None:
        self.first = first
        self.second = second
        self.clicked = False
        self.after_click_reads = 0

    def inner_text(self, _selector: str) -> str:
        if not self.clicked:
            return self.first
        self.after_click_reads += 1
        if self.after_click_reads == 1 or self.second is None:
            return self.first
        return self.second

    def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    def query_selector_all(self, _selector: str) -> list:
        return [] if self.second is None else [_NextButton(self)]


def test_collect_window_waits_for_new_ids_after_next(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 50), _page_text(51, 1))
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args: 51)

    result = DC._collect_window(page, "123", date(2006, 1, 1), date(2026, 8, 28))

    assert result is not None
    assert result["reported"] == 51
    assert len(result["rows"]) == 51
    assert "51" in result["rows"]


def test_collect_window_refuses_a_short_result_without_next(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 50), None)
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args: 51)

    assert DC._collect_window(
        page, "123", date(2006, 1, 1), date(2026, 8, 28)
    ) is None


def test_collect_window_requires_exact_reported_count(monkeypatch) -> None:
    page = _DelayedNextPage(_page_text(1, 2), None)
    monkeypatch.setattr(DC.SC, "orestar_count", lambda *_args: 1)

    assert DC._collect_window(
        page, "123", date(2006, 1, 1), date(2026, 8, 28)
    ) is None


def test_aggregate_evidence_covers_every_filer() -> None:
    summary_ts = datetime(2026, 8, 28, 12, 0).timestamp()
    current = date(2026, 8, 28)
    stale = date(2026, 8, 27)

    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts, {"a": (True, current)}
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (None, current)},
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (True, stale)},
    ) is None
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (True, current)},
    ) is True
    assert P._aggregate_row_verdict(
        ["a", "b"], summary_ts,
        {"a": (True, current), "b": (False, current)},
    ) is False


def test_withdrawn_evidence_is_unioned_across_every_filer() -> None:
    evidence = {"a": {"1", "2"}, "b": {"2", "3"}}

    assert P._withdrawn_for_filers(["a", "b"], evidence) == {"1", "2", "3"}
