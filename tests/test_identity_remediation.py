"""Regression tests for exact-identity missing-row remediation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

SCRAPER_DIR = Path(__file__).parent.parent / "scraper"
sys.path.insert(0, str(SCRAPER_DIR))

import diff_coverage as DC  # noqa: E402
import fetch as F  # noqa: E402


class _ResultPage:
    def __init__(self, texts=None):
        self.url = f"{F.BASE_URL}/gotoPublicTransactionSearchResults.do"
        self._texts = list(texts or ["1 records found"])

    def fill(self, *_args, **_kwargs):
        return None

    def select_option(self, *_args, **_kwargs):
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None

    def click(self, *_args, **_kwargs):
        return None

    def wait_for_url(self, *_args, **_kwargs):
        return None

    def inner_text(self, *_args, **_kwargs):
        if len(self._texts) > 1:
            return self._texts.pop(0)
        return self._texts[0]

    def evaluate(self, *_args, **_kwargs):
        return "token"


class _Context:
    def cookies(self):
        return []


class _Response:
    headers = {"Content-Type": "application/octet-stream"}
    content = b"fresh export"


def test_browser_setup_retries_transient_initial_navigation(monkeypatch) -> None:
    expected = (object(), object(), object())
    attempts = []
    sleeps = []

    def setup(_playwright):
        attempts.append(True)
        if len(attempts) < 3:
            raise F.PlaywrightTimeout("Page.goto timed out")
        return expected

    monkeypatch.setattr(F, "setup_browser", setup)
    monkeypatch.setattr(F.time, "sleep", sleeps.append)

    assert F.setup_browser_retrying(object()) is expected
    assert len(attempts) == 3
    assert sleeps == [15, 30]


def test_results_count_polls_and_accepts_zero() -> None:
    delayed = _ResultPage(["Loading…", "Still loading…", "1,234 records found"])
    assert F._read_results_count(delayed) == 1234
    assert F._read_results_count(_ResultPage(["No records found"])) == 0


def test_record_counts_replace_stale_observations(tmp_path, monkeypatch) -> None:
    raw = tmp_path / "_raw"
    raw.mkdir()
    monkeypatch.setattr(F, "RAW_DIR", raw)
    key = ("C", "2026-01-01", "2026-01-02", "None", "None", "None", "1")
    other = ("E", "2026-01-01", "2026-01-02", "None", "None", "None", "2")
    (tmp_path / "record_counts.json").write_text(json.dumps([
        {"key": list(key), "reported": 10},
        {"key": list(other), "reported": 7},
    ]))
    F.RECORD_COUNTS.clear()
    F.RECORD_COUNTS[key] = 12

    F._flush_record_counts()

    rows = {tuple(row["key"]): row["reported"] for row in json.loads(
        (tmp_path / "record_counts.json").read_text()
    )}
    assert rows == {key: 12, other: 7}
    F.RECORD_COUNTS.clear()


def test_identity_progress_round_trip_and_targeted_reset(tmp_path, monkeypatch) -> None:
    path = tmp_path / "identity.json"
    monkeypatch.setattr(F, "IDENTITY_PROGRESS_FILE", path)
    one = ("C", "2026-01-01", "2026-01-01", "None", "None", "None", "1")
    two = ("E", "2026-01-01", "2026-01-01", "None", "None", "None", "2")
    F._save_identity_progress({two: 4, one: 3})

    assert F._identity_progress() == {one: 3, two: 4}
    assert F._identity_progress(reset_filers=["1"]) == {two: 4}
    assert json.loads(path.read_text()) == [{"key": list(two), "reported": 4}]


def test_partition_tree_requires_every_child_and_exact_sum() -> None:
    parent = ("ALL", "a", "b", "None", "None", "None", "1")
    left = ("C", "a", "b", "None", "None", "None", "1")
    right = ("E", "a", "b", "None", "None", "None", "1")
    branches = {parent: [left, right]}

    missing = F._identity_tree_failures({parent: 5, left: 2}, branches)
    mismatch = F._identity_tree_failures(
        {parent: 5, left: 2, right: 2}, branches
    )
    assert missing[0][1] == "missing"
    assert mismatch[0][1] == "mismatch"
    assert F._identity_tree_errors({parent: 5, left: 2, right: 3}, branches) == []


def test_invalid_partition_subtree_is_removed_for_fresh_retry() -> None:
    parent = ("ALL", "a", "b", "None", "None", "None", "1")
    left = ("C", "a", "b", "None", "None", "None", "1")
    right = ("E", "a", "b", "None", "None", "None", "1")
    unrelated = ("ALL", "a", "b", "None", "None", "None", "2")
    progress = {parent: 5, left: 2, right: 2, unrelated: 7}

    removed = F._discard_identity_subtrees(
        progress, {parent: [left, right]}, [parent]
    )

    assert removed == 3
    assert progress == {unrelated: 7}


def test_repeated_partition_failure_deactivates_entire_filer() -> None:
    one_root = ("ALL", "a", "b", "None", "None", "None", "1")
    one_child = ("C", "a", "b", "None", "None", "None", "1")
    other = ("ALL", "a", "b", "None", "None", "None", "2")
    progress = {one_root: 5, one_child: 5, other: 7}

    assert F._discard_identity_filer(progress, "1") == 2
    assert progress == {other: 7}


def test_identity_failure_counts_round_trip_and_clear_with_progress(
    tmp_path, monkeypatch,
) -> None:
    progress_path = tmp_path / "progress.json"
    failure_path = tmp_path / "failures.json"
    monkeypatch.setattr(F, "IDENTITY_PROGRESS_FILE", progress_path)
    monkeypatch.setattr(F, "IDENTITY_FAILURE_FILE", failure_path)
    one = ("ALL", "a", "b", "None", "None", "None", "1")
    two = ("ALL", "a", "b", "None", "None", "None", "2")
    F._save_identity_progress({one: 5, two: 7})
    F._save_identity_failures({one: 2, two: 1})

    F.clear_identity_progress(["1"])

    assert F._identity_progress() == {two: 7}
    assert F._identity_failures() == {two: 1}


def test_exact_cap_is_complete_only_with_matching_fresh_count() -> None:
    assert not F._needs_filer_split(Path("leaf"), F.ORESTAR_ROW_CAP,
                                    F.ORESTAR_ROW_CAP)
    assert F._needs_filer_split(Path("leaf"), F.ORESTAR_ROW_CAP, None)
    assert F._needs_filer_split(F.CAPPED, F.ORESTAR_ROW_CAP,
                                F.ORESTAR_ROW_CAP + 1)


def test_force_cap_uses_live_count_without_held_skip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(F, "_return_to_search", lambda _page: None)
    monkeypatch.setattr(F, "_read_results_count", lambda _page: 5000)
    monkeypatch.setattr(F, "_held_rows", lambda *_args: pytest.fail("held skip used"))
    monkeypatch.setattr(F.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_k: pytest.fail("export requested"))

    result = F.download_filer_window(
        _ResultPage(), _Context(), "1", date(2026, 1, 1), date(2026, 1, 2),
        tmp_path, force=True,
    )

    assert result is F.CAPPED


def test_force_mismatch_quarantines_old_cache(tmp_path, monkeypatch) -> None:
    canonical = F._filer_window_path(
        tmp_path, "1", "ALL", date(2026, 1, 1), date(2026, 1, 2),
        None, None, None,
    )
    canonical.write_bytes(b"old export")
    monkeypatch.setattr(F, "_return_to_search", lambda _page: None)
    monkeypatch.setattr(F, "_read_results_count", lambda _page: 2)
    monkeypatch.setattr(F, "_validate_download", lambda _path: 1)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_k: _Response())

    result = F.download_filer_window(
        _ResultPage(), _Context(), "1", date(2026, 1, 1), date(2026, 1, 2),
        tmp_path, force=True,
    )

    assert result is None
    assert not canonical.exists()
    assert (tmp_path / ".identity_stale" / canonical.name).read_bytes() == b"old export"


def test_proved_exact_cap_is_marked_for_merger(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(F, "_return_to_search", lambda _page: None)
    monkeypatch.setattr(F, "_read_results_count", lambda _page: F.ORESTAR_ROW_CAP)
    monkeypatch.setattr(F, "_validate_download", lambda _path: F.ORESTAR_ROW_CAP)
    monkeypatch.setattr(F.requests, "get", lambda *_a, **_k: _Response())

    result = F.download_filer_window(
        _ResultPage(), _Context(), "1", date(2026, 1, 1), date(2026, 1, 2),
        tmp_path, force=True,
    )

    assert result is not None
    assert result.name.startswith("verified_filer1_")
    assert result.exists()


def _run_selector(
    tmp_path: Path,
    diff_rows,
    survey_rows=None,
    done="",
    incomplete="",
    progress_rows=None,
):
    data = tmp_path / "data"
    (data / "aggregated" / "filers").mkdir(parents=True)
    (data / "aggregated" / "filer_index.json").write_text("[]")
    (data / "coverage_diff.json").write_text(json.dumps(diff_rows))
    if survey_rows is not None:
        (data / "coverage_survey.json").write_text(json.dumps(survey_rows))
    if done:
        (data / "backfilled_filers.txt").write_text(done)
    if incomplete:
        (data / "incomplete_backfills.txt").write_text(incomplete)
    if progress_rows is not None:
        (data / "identity_remediation_windows.json").write_text(
            json.dumps(progress_rows)
        )
    ids_path = tmp_path / "ids.txt"
    mode_path = tmp_path / "mode.txt"
    end_path = tmp_path / "end.txt"
    resume_path = tmp_path / "resume.txt"
    env = os.environ.copy()
    env["AUTO_BACKFILL_OUTPUT"] = str(ids_path)
    env["AUTO_BACKFILL_MODE_OUTPUT"] = str(mode_path)
    env["AUTO_BACKFILL_END_DATE_OUTPUT"] = str(end_path)
    env["AUTO_BACKFILL_RESUME_OUTPUT"] = str(resume_path)
    subprocess.run(
        [sys.executable, str(SCRAPER_DIR / "auto_backfill_ids.py")],
        cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
    )
    return (
        ids_path.read_text() if ids_path.exists() else None,
        mode_path.read_text().strip(),
        end_path.read_text().strip() if end_path.exists() else None,
        resume_path.read_text().strip(),
    )


def test_exact_selector_overrides_historical_done_and_batches_one(tmp_path) -> None:
    ids, mode, end, resume = _run_selector(tmp_path, [
        {"filer_id": "1", "complete": False, "missing": ["a"], "name": "one"},
        {"filer_id": "2", "complete": False, "missing": ["a", "b"], "name": "two"},
    ], done="2\n")
    assert ids == "2"
    assert mode == "identity"
    assert end is None
    assert resume == "false"


def test_usable_exact_result_suppresses_stale_count_survey(tmp_path) -> None:
    ids, mode, _end, _resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": [], "surplus": ["x"]}],
        [{"filer_id": "1", "missing": 99, "name": "stale"}],
    )
    assert ids is None
    assert mode == "identity"


def test_unknown_exact_result_falls_back_to_count_survey(tmp_path) -> None:
    ids, mode, _end, _resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": None, "missing": []}],
        [{"filer_id": "1", "missing": 3, "name": "unknown"}],
    )
    assert ids == "1"
    assert mode == "count"


def test_selector_resumes_active_tree_before_larger_new_candidate(tmp_path) -> None:
    root = {
        "key": ["ALL", "2006-01-01", "2026-08-31", "None", "None", "None", "2"],
        "reported": 6000,
    }
    ids, mode, end, resume = _run_selector(
        tmp_path,
        [
            {"filer_id": "1", "complete": False, "missing": list("abcdefghij")},
            {"filer_id": "2", "complete": False, "missing": ["x"]},
        ],
        incomplete="2:5\n",
        progress_rows=[root],
    )
    assert ids == "2"
    assert mode == "identity"
    assert end == "2026-08-31"
    assert resume == "true"
    assert (tmp_path / "data" / "incomplete_backfills.txt").read_text() == "2:5\n"


def test_newer_survey_is_not_suppressed_by_stale_clean_diff(tmp_path) -> None:
    ids, mode, _end, _resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": True, "missing": [],
          "checked": "2026-08-01"}],
        [{"filer_id": "1", "missing": 2, "checked": "2026-09-01"}],
    )
    assert ids == "1"
    assert mode == "count"


def test_deferred_exact_missing_is_retried_after_other_identity_work(tmp_path) -> None:
    ids, mode, end, resume = _run_selector(
        tmp_path,
        [{"filer_id": "1", "complete": False, "missing": ["x"]}],
        incomplete="1:3\n",
    )
    assert ids == "1"
    assert mode == "identity"
    assert end is None
    assert resume == "false"


def test_identity_cli_rejects_partial_history_scope() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "fetch.py"),
            "--filer-ids", "1",
            "--identity-remediation",
            "--end-date", "2026-09-02",
            "--start-year", "2020",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --start-year=2006" in result.stderr


def test_verification_cli_requires_fresh_full_history() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
            "--recheck",
            "--start-year", "2020",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --start-year=2006" in result.stderr


def test_verification_cli_cannot_disable_recheck() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "requires --recheck" in result.stderr


@pytest.mark.parametrize(
    ("limit", "message"),
    [
        ("-1", "--limit must be zero or positive"),
        ("1", "--require-no-missing cannot be combined with --limit"),
    ],
)
def test_verification_cli_cannot_limit_targets(limit, message) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRAPER_DIR / "diff_coverage.py"),
            "--filer-ids", "1",
            "--require-no-missing",
            "--recheck",
            "--limit", limit,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert message in result.stderr


def test_remediation_gate_allows_surplus_but_requires_fresh_zero_missing() -> None:
    entries = {
        "1": {"complete": False, "missing": [], "surplus": ["old"]},
        "2": {"complete": False, "missing": ["still-missing"]},
    }
    assert DC._remediation_verification_failures(entries, ["1"], {"1"}) == []
    assert DC._remediation_verification_failures(entries, ["1"], set()) == [
        "1: no fresh usable diff"
    ]
    assert DC._remediation_verification_failures(entries, ["2"], {"2"}) == [
        "2: 1 missing IDs remain"
    ]
