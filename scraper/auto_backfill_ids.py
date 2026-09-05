#!/usr/bin/env python3
"""
Find filers with ORESTAR discrepancies that still need work.

An exact identity diff is the only evidence allowed to select a missing-ID
filer for mutation. Count surveys and dollar differences remain useful for
prioritizing which committees to diff, but never authorize a backfill. Selected
IDs are written to /tmp/auto_backfill_ids.txt.

Incomplete filers from previous failed runs are retried. Ordinary retries come
up naturally by exact missing-ID count; only a partially validated identity
tree is priority-boosted so its frozen snapshot can finish before another tree
starts. A historical "done" marker never overrides newer exact evidence.

Used by the backfill workflow in auto mode.
"""

import csv
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from balance_snapshot import (
    COVERAGE_EVIDENCE_VERSION,
    evidence_is_current,
    exact_coverage_result_shape_is_valid,
    exact_evidence_identifier_is_valid,
    transaction_filer_snapshots,
    transaction_snapshot_id,
)

FILERS_DIR = Path("data/aggregated/filers")
INDEX_FILE = Path("data/aggregated/filer_index.json")
INCOMPLETE_FILE = Path("data/incomplete_backfills.txt")
OUTPUT_FILE = Path(os.environ.get("AUTO_BACKFILL_OUTPUT", "/tmp/auto_backfill_ids.txt"))
MODE_FILE = Path(os.environ.get("AUTO_BACKFILL_MODE_OUTPUT",
                                "/tmp/auto_backfill_mode.txt"))
END_DATE_FILE = Path(os.environ.get("AUTO_BACKFILL_END_DATE_OUTPUT",
                                    "/tmp/auto_backfill_end_date.txt"))
RESUME_FILE = Path(os.environ.get("AUTO_BACKFILL_RESUME_OUTPUT",
                                  "/tmp/auto_backfill_resume.txt"))
STATUS_FILE = Path(os.environ.get("AUTO_BACKFILL_STATUS_OUTPUT",
                                  "/tmp/auto_backfill_status.txt"))
IDENTITY_BATCH_SIZE = 1

print(f"Working directory: {Path.cwd()}")
print(f"Filers dir exists: {FILERS_DIR.exists()}")
print(f"Index file exists: {INDEX_FILE.exists()}")

# Build slug -> filer_id mapping from the index
slug_to_fid = {}
if INDEX_FILE.exists():
    with open(INDEX_FILE) as f:
        for entry in json.load(f):
            if entry.get("filer_id") and entry.get("slug"):
                slug_to_fid[entry["slug"]] = str(entry["filer_id"])
print(f"Filer IDs in index: {len(slug_to_fid)}")

# Incomplete filers from previous runs are retried.
# Format: "fid" or "fid:count". Filers with 3+ retries are skipped for now
# (they'll be retried after all other filers are processed).
MAX_RETRIES = 3
incomplete = {}  # fid -> retry count
if INCOMPLETE_FILE.exists():
    for line in INCOMPLETE_FILE.read_text().strip().split("\n"):
        if ":" in line:
            fid_str, cnt = line.split(":", 1)
            fid_str = fid_str.strip()
            incomplete[fid_str] = int(cnt)
        elif line.strip():
            incomplete[line.strip()] = 1
    retryable = {fid for fid, cnt in incomplete.items() if cnt < MAX_RETRIES}
    deferred = {fid for fid, cnt in incomplete.items() if cnt >= MAX_RETRIES}
    if retryable:
        print(f"Incomplete filers to retry: {len(retryable)}")
    if deferred:
        print(f"Deferred filers (>={MAX_RETRIES} retries, skipping for now): {len(deferred)} — {sorted(deferred)}")

# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------
#
# Rank by ROWS MISSING, not by dollar discrepancy.
#
# The dollar ranking sent the backfill after committees it could not help. Of
# the 50 largest deltas measured by the coverage survey, 32 had nothing to
# fetch at all: Committee for SAIF Keeping tops the list at $665,242 and holds
# all seven of the seven transactions ORESTAR has for it. Meanwhile Oregon
# Right to Life is $614,666 adrift and short only 42 rows, while Local 48 is
# short 13,626. Rows missing and dollars adrift turn out to be close to
# independent, so ranking by one to find the other never worked.
#
# The survey asks ORESTAR for its own record count -- one search, no export --
# and records the shortfall. Fetching what it found missing is the only part of
# the discrepancy a backfill can address; the rest is balance reconciliation
# for dormant and pre-ORESTAR committees, which is a different job.
SURVEY_FILE = Path("data/coverage_survey.json")
DIFF_FILE = Path("data/coverage_diff.json")
IDENTITY_PROGRESS_FILE = Path("data/identity_remediation_windows.json")
TRANSACTION_DIR = Path("data/transactions")
FULL_HISTORY_START = "2006-01-01"
USABLE_HISTORY_KEY = "usable_history"


def _exact_row_shape_is_valid(row):
    """Reject malformed/old identity rows before they can authorize writes."""
    return exact_coverage_result_shape_is_valid(row)


def _collection_started(row):
    """Comparable precise UTC query start; invalid values sort nowhere."""
    value = row.get("collection_started_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _checked_at(row):
    value = row.get("checked_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _result_signature(row):
    """ORESTAR verdict fields used to reject tied conflicting observations."""
    return (
        row.get("complete"),
        tuple(row.get("missing") or []),
        tuple(row.get("surplus") or []),
        tuple(row.get("superseded") or []),
        row.get("orestar"),
        row.get("held"),
    )


def _observation_is_well_formed(row, filer_id):
    started = _collection_started(row) if isinstance(row, dict) else None
    completed = _checked_at(row) if isinstance(row, dict) else None
    try:
        range_start = date.fromisoformat(str(row.get("range_start") or ""))
        range_end = date.fromisoformat(str(row.get("range_end") or ""))
    except (AttributeError, ValueError):
        return False
    return (
        isinstance(row, dict)
        and _exact_row_shape_is_valid(row)
        and str(row.get("filer_id") or "") == filer_id
        and row.get("evidence_version") == COVERAGE_EVIDENCE_VERSION
        and exact_evidence_identifier_is_valid(
            row.get("transaction_snapshot_id")
        )
        and started is not None
        and completed is not None
        and started <= completed
        and range_start == date(2006, 1, 1)
        and range_end >= range_start
    )


def _usable_observation(row, requirement, filer_id):
    return (
        _observation_is_well_formed(row, filer_id)
        and evidence_is_current(
            row,
            requirement["captured_at"],
            require_precise=True,
            require_collection_started=True,
            strictly_after=True,
            range_start=FULL_HISTORY_START,
            minimum_range_end=requirement["capture_day"],
        )
    )


def _anchored_observation_lanes(row, requirement, filer_id):
    """Newest ORESTAR result in every capture-anchored range/digest lane.

    The top-level row is the latest successful write, but out-of-order merges
    can still leave a newer observation in history. The paired global hash is
    required only on an anchor proving which per-filer digest/range belonged
    to the summary capture. The verdict comes from the newest usable query for
    that identical digest and range.
    """
    # Never fall back around a malformed or pre-summary top-level result. It is
    # the last successful write and could otherwise hide a newer observation
    # merely because its provenance is unsafe.
    if not _usable_observation(row, requirement, filer_id):
        return None
    history = row.get(USABLE_HISTORY_KEY, [])
    if not isinstance(history, list) or any(
        not isinstance(item, dict) for item in history
    ):
        return None
    if any(
        (claimed_owner := str(item.get("filer_id") or "").strip())
        and claimed_owner != filer_id
        for item in history
    ):
        return None
    # Legacy pre-history rows have no collection start and remain displayable.
    # A malformed record claiming the structured schema could hide a newer
    # ORESTAR verdict, so fail closed rather than silently stepping around it.
    if any(
        item.get("collection_started_at") is not None
        and not _observation_is_well_formed(item, filer_id)
        for item in history
    ):
        return None
    usable = [
        item for item in [row, *history]
        if _usable_observation(item, requirement, filer_id)
    ]
    anchor_digests = {}
    for item in usable:
        if (item.get("transaction_snapshot_id")
                != requirement["transaction_snapshot_id"]):
            continue
        bounds = (
            str(item.get("range_start") or ""),
            str(item.get("range_end") or ""),
        )
        anchor_digests.setdefault(bounds, set()).add(
            item.get("filer_transaction_digest")
        )
    if any(len(digests) != 1 for digests in anchor_digests.values()):
        return None

    lanes = {}
    for bounds, digests in anchor_digests.items():
        digest = next(iter(digests))
        observations = [
            item for item in usable
            if (
                str(item.get("range_start") or ""),
                str(item.get("range_end") or ""),
            ) == bounds
            and item.get("filer_transaction_digest") == digest
        ]
        newest_start = max(_collection_started(item) for item in observations)
        newest = [
            item for item in observations
            if _collection_started(item) == newest_start
        ]
        if len({_result_signature(item) for item in newest}) != 1:
            return None
        # Completion time breaks a same-query-start tie only after we prove the
        # verdict is identical; it cannot turn a pre-summary query into evidence.
        lanes[bounds] = max(newest, key=_checked_at)
    return lanes


def _paired_requirements():
    """Return paired scope requirements, ambiguities, and dollar priorities."""
    scopes = {}
    ownership = {}
    balance_candidates = set()
    invalid_members = set()
    for path in FILERS_DIR.glob("*.json"):
        try:
            detail = json.loads(path.read_text())
        except Exception:
            continue
        comparison = detail.get("orestar_comparison") or {}
        if comparison.get("status") != "paired":
            continue
        detail_id_values = detail.get("filer_ids")
        captured_id_values = comparison.get("filer_ids")
        ids = sorted({
            text for fid in detail_id_values
            if (text := str(fid or "").strip())
        }) if isinstance(detail_id_values, list) else []
        captured_ids = sorted({
            text for fid in captured_id_values
            if (text := str(fid or "").strip())
        }) if isinstance(captured_id_values, list) else []
        if not ids or captured_ids != ids:
            malformed_members = set(ids) | set(captured_ids)
            invalid_members.update(malformed_members)
            try:
                malformed_delta = abs(float(
                    comparison.get("delta_at_capture") or 0
                ))
            except (TypeError, ValueError, OverflowError):
                malformed_delta = 0
            if (comparison.get("actionable") and not detail.get("closed")
                    and malformed_delta > 0.01):
                balance_candidates.update(malformed_members)
            continue
        captured_at = comparison.get("captured_at")
        transaction_id = comparison.get("app_transaction_snapshot_id")
        if (not ids or not captured_at
                or not exact_evidence_identifier_is_valid(transaction_id)):
            continue
        try:
            capture_day = datetime.fromtimestamp(
                float(captured_at), tz=timezone.utc
            ).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            continue
        requirement = {
            "captured_at": captured_at,
            "capture_day": capture_day,
            "transaction_snapshot_id": transaction_id,
            "scope_ids": ids,
        }
        key = tuple(ids)
        existing = scopes.get(key)
        if existing is not None:
            # Two detail files claiming the same physical scope are ambiguous
            # even when their captured values happen to match. There is no
            # unique canonical owner for an automatic mutation.
            requirement = {**requirement, "ambiguous": True}
        scopes[key] = requirement
        for fid in ids:
            ownership.setdefault(fid, set()).add(key)
        try:
            delta = abs(float(comparison.get("delta_at_capture") or 0))
        except (TypeError, ValueError, OverflowError):
            delta = 0
        if (comparison.get("actionable")
                and not detail.get("closed") and delta > 0.01):
            balance_candidates.update(ids)

    ambiguous_members = set(invalid_members)
    for fid, keys in ownership.items():
        if len(keys) > 1 or any(scopes[key].get("ambiguous") for key in keys):
            for key in keys:
                ambiguous_members.update(key)
    requirements = {
        fid: scopes[next(iter(keys))]
        for fid, keys in ownership.items()
        if len(keys) == 1 and fid not in ambiguous_members
    }
    return requirements, ambiguous_members, balance_candidates


def _automation_exact_rows(diff_rows, candidate_ids=(), active_ranges=None):
    """Return exact rows safe enough to authorize automatic remediation.

    An observation bearing the paired global fingerprint anchors the exact
    per-filer digest/range that existed at summary capture. A later query may
    supply the verdict after an unrelated filer changes the global hash, but
    only in that anchored digest/range lane. Current liveness comes from
    recomputing every member's deterministic per-filer digest.
    """
    requirements, ambiguous_members, balance_candidates = _paired_requirements()
    active_ranges = active_ranges or {}
    candidates = {str(fid) for fid in candidate_ids if str(fid)} | balance_candidates
    rows_by_id = {}
    duplicate_ids = set()
    for row in diff_rows:
        fid = str(row.get("filer_id") or "").strip()
        if not fid:
            continue
        if fid in rows_by_id:
            duplicate_ids.add(fid)
        else:
            rows_by_id[fid] = row

    relevant_scopes = {}
    blocked = set()
    for fid in candidates:
        requirement = requirements.get(fid)
        if requirement is None:
            blocked.add(fid)
            if fid in ambiguous_members:
                blocked.update(ambiguous_members)
            continue
        relevant_scopes[tuple(requirement["scope_ids"])] = requirement

    pending = []
    for members, requirement in relevant_scopes.items():
        if any(member in ambiguous_members for member in members):
            blocked.update(members)
            continue
        member_lanes = {}
        invalid = False
        for member in members:
            row = rows_by_id.get(member)
            lanes = (
                None if row is None or member in duplicate_ids
                else _anchored_observation_lanes(row, requirement, member)
            )
            if not lanes:
                invalid = True
                break
            member_lanes[member] = lanes
        common_bounds = (
            set.intersection(*(set(lanes) for lanes in member_lanes.values()))
            if member_lanes else set()
        )
        if invalid or not common_bounds:
            blocked.update(members)
            continue
        # Prefer the lane whose least-recent member observation is newest. This
        # avoids a greedy per-member choice and keeps a multi-ID scope atomic.
        active_ends = {
            active_ranges[member] for member in members if member in active_ranges
        }
        if len(active_ends) > 1:
            blocked.update(members)
            continue
        active_bounds = (
            (FULL_HISTORY_START, next(iter(active_ends))) if active_ends else None
        )
        if active_bounds in common_bounds:
            chosen_bounds = active_bounds
        else:
            chosen_bounds = max(
                common_bounds,
                key=lambda bounds: (
                    min(
                        _collection_started(member_lanes[member][bounds])
                        for member in members
                    ),
                    bounds,
                ),
            )
        rows = [
            (member, member_lanes[member][chosen_bounds]) for member in members
        ]
        range_start, range_end = chosen_bounds
        try:
            start = date.fromisoformat(range_start)
            end = date.fromisoformat(range_end)
        except ValueError:
            blocked.update(members)
            continue
        pending.append((members, rows, start, end))

    # Usually every row in a rolling slice has one range. Grouping preserves
    # correctness when old and new exact evidence use different frozen ends
    # without scanning the 143 MB shard set once per committee.
    grouped = {}
    for members, rows, start, end in pending:
        group = grouped.setdefault((start, end), {"ids": set(), "scopes": []})
        group["ids"].update(members)
        group["scopes"].append((members, rows))

    valid = {}
    schema_error = None
    for (start, end), group in grouped.items():
        snapshot_before = transaction_snapshot_id(TRANSACTION_DIR)
        try:
            current = transaction_filer_snapshots(
                TRANSACTION_DIR, group["ids"], start, end,
            )
        except (OSError, EOFError, csv.Error, UnicodeError, ValueError) as exc:
            schema_error = str(exc)
            for members, _rows in group["scopes"]:
                blocked.update(members)
            continue
        if (not snapshot_before
                or transaction_snapshot_id(TRANSACTION_DIR) != snapshot_before):
            schema_error = "transaction shards changed during certification"
            for members, _rows in group["scopes"]:
                blocked.update(members)
            continue
        for members, rows in group["scopes"]:
            if all(
                current.get(member, {}).get("filer_transaction_digest")
                == row.get("filer_transaction_digest")
                for member, row in rows
            ):
                valid.update(rows)
            else:
                blocked.update(members)
    return valid, blocked, schema_error


def _active_identity_roots():
    """Return filer -> frozen end date for resumable top-level windows."""
    try:
        rows = json.loads(IDENTITY_PROGRESS_FILE.read_text()) \
            if IDENTITY_PROGRESS_FILE.exists() else []
    except Exception:
        rows = []
    roots = {}
    for row in rows:
        key = row.get("key") if isinstance(row, dict) else None
        if (not isinstance(key, list) or len(key) != 7 or key[0] != "ALL"
                or key[3:6] != ["None", "None", "None"]):
            continue
        fid, end = str(key[-1]), str(key[2])
        if end > roots.get(fid, ""):
            roots[fid] = end
    return roots


def _row_requests_exact_resolution(row):
    """Seed candidates from current or historical missing-ID observations."""
    if not isinstance(row, dict):
        return False
    if row.get("complete") is None or bool(row.get("missing")):
        return True
    history = row.get(USABLE_HISTORY_KEY) or []
    return isinstance(history, list) and any(
        isinstance(item, dict) and bool(item.get("missing"))
        for item in history
    )


survey_rows = []
if SURVEY_FILE.exists():
    try:
        survey_rows = json.loads(SURVEY_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {SURVEY_FILE}: {exc}")
active_roots = _active_identity_roots()

filers = []
deferred_exact = []
exact_blocked = set()
exact_range_ends = {}
diff_rows = []
if DIFF_FILE.exists():
    try:
        diff_rows = json.loads(DIFF_FILE.read_text())
    except Exception as exc:
        print(f"Could not read {DIFF_FILE}: {exc}")

candidate_ids = {
    str(row.get("filer_id") or "")
    for row in diff_rows
    if row.get("filer_id")
    and _row_requests_exact_resolution(row)
}
survey_short = {
    str(row.get("filer_id") or ""): int(row.get("missing") or 0)
    for row in survey_rows
    if row.get("filer_id") and int(row.get("missing") or 0) > 0
}
candidate_ids.update(survey_short)

automation_rows, exact_blocked, schema_error = _automation_exact_rows(
    diff_rows, candidate_ids, active_roots,
)
if exact_blocked:
    print(
        f"Exact authorization blocked for {len(exact_blocked)} physical filer(s); "
        "count and dollar evidence remain report-only"
    )
if schema_error:
    print(f"Transaction snapshot could not be certified: {schema_error}")
if survey_short:
    print(
        f"Count survey prioritizes {len(survey_short)} short filer(s), but does "
        "not authorize mutation without current exact identity evidence"
    )

for r in automation_rows.values():
    fid = str(r.get("filer_id") or "")
    missing = len(r.get("missing") or [])
    exact_range_ends[fid] = str(r["range_end"])
    if missing <= 0:
        continue
    # A partially completed forced tree is resumable even after the ordinary
    # retry limit. Its validated leaves must not be thrown away merely because
    # one runner made no progress.
    resumable = active_roots.get(fid) == exact_range_ends[fid]
    if incomplete.get(fid, 0) >= MAX_RETRIES and not resumable:
        deferred_exact.append((missing, fid, r.get("name", "")))
        continue
    # Precise exact identity evidence overrides the historical done list. A
    # count can say "done" while a withdrawn row cancels the missing row.
    filers.append((missing, fid, r.get("name", "")))

# "Deferred" means after the other exact-missing committees, not abandoned.
# Without this pass the workflow eventually emitted no IDs and reported all
# discrepancies addressed while known missing transaction IDs remained.
if not filers and deferred_exact:
    filers = deferred_exact
    print(f"Retrying {len(filers)} deferred exact-missing filer(s); "
          "no non-deferred identity work remains")

if filers:
    # Finish an active frozen tree before starting another committee. This is
    # what turns a later `filer_ids=auto` dispatch into a true resume.
    filers.sort(
        key=lambda row: (
            active_roots.get(row[1]) == exact_range_ends.get(row[1]),
            row[0],
            row[1],
        ),
        reverse=True,
    )
    batch = filers[:IDENTITY_BATCH_SIZE]
    mode = "identity"
    print(f"{len(filers)} filer(s) have current exact missing transaction IDs")
else:
    batch = []
    mode = "identity"

if batch:
    print(f"Selecting {len(batch)} filer(s) by exact missing transaction IDs:")
    for score, fid, name in batch:
        retry = " (RETRY)" if fid in incomplete else ""
        shown = f"{score:,} exact IDs missing"
        print(f"  {fid}: {shown} — {name}{retry}")
    OUTPUT_FILE.write_text(" ".join(fid for _, fid, _ in batch))
    STATUS_FILE.write_text("selected\n")
    MODE_FILE.write_text(mode + "\n")
    if (mode == "identity"
            and active_roots.get(batch[0][1]) == exact_range_ends[batch[0][1]]):
        END_DATE_FILE.write_text(active_roots[batch[0][1]] + "\n")
        RESUME_FILE.write_text("true\n")
        print(f"Resuming frozen identity tree through {active_roots[batch[0][1]]}")
    elif mode == "identity":
        # The exact diff and the forced fetch must cover the identical
        # inclusive window. Defaulting independently to "today" lets a chain
        # crossing midnight verify a different range from the one that
        # authorized it.
        END_DATE_FILE.write_text(exact_range_ends[batch[0][1]] + "\n")
        RESUME_FILE.write_text("false\n")
else:
    OUTPUT_FILE.unlink(missing_ok=True)
    END_DATE_FILE.unlink(missing_ok=True)
    RESUME_FILE.write_text("false\n")
    MODE_FILE.write_text("identity\n")
    if exact_blocked:
        STATUS_FILE.write_text("blocked\n")
        print(
            "Automatic remediation paused: candidate scopes lack current exact "
            "identity provenance. No data was mutated and this is not a "
            "completion signal."
        )
    else:
        STATUS_FILE.write_text("idle\n")
        print(
            "No automatic repair selected: current exact identity evidence "
            "contains no authorized missing rows."
        )
