"""Fail-closed selection of exact ORESTAR identity evidence.

Coverage results are durable observations, not permanent facts.  A result can
authorize an automatic action only when it belongs to the same paired app
snapshot, was collected after that ORESTAR summary, covers the full required
date range, and still matches the current per-filer transaction digest.

The selector is shared by missing-row remediation and balance-only omission of
rows ORESTAR no longer returns.  Keeping one implementation prevents the cash
calculation from accepting weaker evidence than the mutation guard.
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from balance_snapshot import (
    COVERAGE_EVIDENCE_VERSION,
    evidence_is_current,
    exact_coverage_result_shape_is_valid,
    exact_evidence_identifier_is_valid,
    transaction_filer_snapshots,
    transaction_snapshot_id,
)


FULL_HISTORY_START = "2006-01-01"
USABLE_HISTORY_KEY = "usable_history"


def _collection_started(row: dict) -> datetime | None:
    """Return a precise UTC collection start, or ``None``."""
    value = row.get("collection_started_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _checked_at(row: dict) -> datetime | None:
    """Return a precise UTC completion time, or ``None``."""
    value = row.get("checked_at")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed.astimezone(timezone.utc)


def _result_signature(row: dict) -> tuple:
    """ORESTAR verdict fields used to reject tied conflicting observations."""
    return (
        row.get("complete"),
        tuple(row.get("missing") or []),
        tuple(row.get("surplus") or []),
        tuple(row.get("superseded") or []),
        row.get("orestar"),
        row.get("held"),
    )


def _observation_is_well_formed(row: Any, filer_id: str) -> bool:
    """Whether one exact observation has the complete structured schema."""
    if not isinstance(row, dict):
        return False
    started = _collection_started(row)
    completed = _checked_at(row)
    try:
        range_start = date.fromisoformat(str(row.get("range_start") or ""))
        range_end = date.fromisoformat(str(row.get("range_end") or ""))
    except ValueError:
        return False
    return (
        exact_coverage_result_shape_is_valid(row)
        and exact_evidence_identifier_is_valid(row.get("filer_id"))
        and row.get("filer_id") == filer_id
        and row.get("evidence_version") == COVERAGE_EVIDENCE_VERSION
        and exact_evidence_identifier_is_valid(
            row.get("transaction_snapshot_id")
        )
        and started is not None
        and completed is not None
        and started <= completed
        and range_start == date.fromisoformat(FULL_HISTORY_START)
        and range_end >= range_start
    )


def _usable_observation(row: Any, requirement: dict, filer_id: str) -> bool:
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


def _looks_structured(row: dict) -> bool:
    """Whether a history row claims the precise evidence schema."""
    return any(key in row for key in (
        "evidence_version",
        "collection_started_at",
        "checked_at",
        "transaction_snapshot_id",
        "filer_transaction_digest",
        "range_start",
        "range_end",
    ))


def _anchored_observation_lanes(
    row: dict,
    requirement: dict,
    filer_id: str,
) -> dict[tuple[str, str], dict] | None:
    """Newest verdict in every capture-anchored range/digest lane.

    The observation carrying the paired global fingerprint proves which local
    per-filer state existed at capture.  A later query after an unrelated shard
    change may supply the verdict, but only for that same digest and range.
    """
    # Never fall back around a malformed or stale top-level result.  It is the
    # latest successful write and could otherwise conceal conflicting evidence.
    if not _usable_observation(row, requirement, filer_id):
        return None
    history = row.get(USABLE_HISTORY_KEY, [])
    if not isinstance(history, list) or any(
        not isinstance(item, dict) for item in history
    ):
        return None
    # Any claimed owner must be this physical filer, including legacy records.
    if any(
        (claimed_owner := str(item.get("filer_id") or "").strip())
        and claimed_owner != filer_id
        for item in history
    ):
        return None
    # Legacy rows remain displayable but cannot authorize anything.  A record
    # claiming any structured provenance field must be completely valid; do
    # not step around a malformed record that could hide a newer verdict.
    if any(
        _looks_structured(item)
        and not _observation_is_well_formed(item, filer_id)
        for item in history
    ):
        return None

    usable = [
        item for item in [row, *history]
        if _usable_observation(item, requirement, filer_id)
    ]
    anchor_digests: dict[tuple[str, str], set[str]] = {}
    for item in usable:
        if item.get("transaction_snapshot_id") != requirement["transaction_snapshot_id"]:
            continue
        bounds = (
            str(item.get("range_start") or ""),
            str(item.get("range_end") or ""),
        )
        anchor_digests.setdefault(bounds, set()).add(
            item.get("filer_transaction_digest")
        )
    # One paired snapshot cannot truthfully anchor two states for the same
    # physical filer and range.
    if any(len(digests) != 1 for digests in anchor_digests.values()):
        return None

    lanes: dict[tuple[str, str], dict] = {}
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
        # Completion time breaks a same-query-start tie only after identical
        # verdicts are proved.  It cannot rescue a pre-summary query.
        lanes[bounds] = max(newest, key=_checked_at)
    return lanes


def _requirement_signature(requirement: Any) -> tuple | None:
    """Normalize and validate one paired-snapshot scope requirement."""
    if not isinstance(requirement, dict):
        return None
    raw_members = requirement.get("scope_ids")
    if not isinstance(raw_members, (list, tuple)) or not raw_members:
        return None
    if any(not exact_evidence_identifier_is_valid(fid) for fid in raw_members):
        return None
    members = tuple(sorted(set(raw_members)))
    if len(members) != len(raw_members):
        return None
    fingerprint = requirement.get("transaction_snapshot_id")
    if not exact_evidence_identifier_is_valid(fingerprint):
        return None
    try:
        captured_at = float(requirement["captured_at"])
        capture_day = datetime.fromtimestamp(
            captured_at, tz=timezone.utc,
        ).date().isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if requirement.get("capture_day") != capture_day:
        return None
    return members, captured_at, capture_day, fingerprint


def rows_requesting_surplus(diff_rows: Iterable[dict]) -> set[str]:
    """Physical filer IDs with a current or historical surplus observation."""
    candidates: set[str] = set()
    for row in diff_rows:
        if not isinstance(row, dict):
            continue
        fid = row.get("filer_id")
        if not exact_evidence_identifier_is_valid(fid):
            continue
        history = row.get(USABLE_HISTORY_KEY, [])
        observations = [row]
        if isinstance(history, list):
            observations.extend(item for item in history if isinstance(item, dict))
        if any(isinstance(item.get("surplus"), list) and item["surplus"]
               for item in observations):
            candidates.add(fid)
    return candidates


def certify_exact_scope_rows(
    diff_rows: Iterable[dict],
    requirements: dict[str, dict],
    candidate_ids: Iterable[str],
    transaction_dir: Path,
    *,
    active_ranges: dict[str, str] | None = None,
    ambiguous_members: Iterable[str] = (),
    require_no_missing: bool = False,
) -> tuple[dict[str, dict], set[str], str | None]:
    """Return exact rows safe to act on, blocked IDs, and a scan error.

    Certification is atomic at canonical-scope level.  Every physical member
    must share a valid anchored range, and every selected row must match a
    deterministic digest recomputed from the current transaction shards.
    """
    active_ranges = active_ranges or {}
    ambiguous = {str(fid) for fid in ambiguous_members if str(fid)}
    candidates = {str(fid) for fid in candidate_ids if str(fid)}
    source_rows = diff_rows.values() if isinstance(diff_rows, dict) else diff_rows
    rows_by_id: dict[str, dict] = {}
    duplicate_ids: set[str] = set()
    for row in source_rows:
        if not isinstance(row, dict):
            continue
        fid = row.get("filer_id")
        if not exact_evidence_identifier_is_valid(fid):
            continue
        if fid in rows_by_id:
            duplicate_ids.add(fid)
        else:
            rows_by_id[fid] = row

    relevant_scopes: dict[tuple[str, ...], dict] = {}
    blocked: set[str] = set()
    for fid in candidates:
        requirement = requirements.get(fid)
        signature = _requirement_signature(requirement)
        if signature is None:
            blocked.add(fid)
            continue
        members = signature[0]
        if fid not in members:
            blocked.update(members)
            blocked.add(fid)
            continue
        # Every member must independently point to precisely the same paired
        # scope.  This rejects partial or overlapping canonical ownership.
        if any(_requirement_signature(requirements.get(member)) != signature
               for member in members):
            blocked.update(members)
            continue
        relevant_scopes[members] = requirement

    pending = []
    for members, requirement in relevant_scopes.items():
        if any(member in ambiguous for member in members):
            blocked.update(members)
            continue
        member_lanes: dict[str, dict[tuple[str, str], dict]] = {}
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
            # Prefer the lane whose least-recent member observation is newest;
            # greedy per-member choices could mix unrelated ranges.
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
        try:
            start = date.fromisoformat(chosen_bounds[0])
            end = date.fromisoformat(chosen_bounds[1])
        except ValueError:
            blocked.update(members)
            continue
        pending.append((members, rows, start, end))

    # Group ranges so a normal rolling slice scans the shards only once.
    grouped: dict[tuple[date, date], dict] = {}
    for members, rows, start, end in pending:
        group = grouped.setdefault((start, end), {"ids": set(), "scopes": []})
        group["ids"].update(members)
        group["scopes"].append((members, rows))

    valid: dict[str, dict] = {}
    schema_error = None
    for (start, end), group in grouped.items():
        snapshot_before = transaction_snapshot_id(transaction_dir)
        try:
            current = transaction_filer_snapshots(
                transaction_dir, group["ids"], start, end,
            )
        except (OSError, EOFError, csv.Error, UnicodeError, ValueError) as exc:
            schema_error = str(exc)
            for members, _rows in group["scopes"]:
                blocked.update(members)
            continue
        if (not snapshot_before
                or transaction_snapshot_id(transaction_dir) != snapshot_before):
            schema_error = "transaction shards changed during certification"
            for members, _rows in group["scopes"]:
                blocked.update(members)
            continue
        for members, rows in group["scopes"]:
            digest_matches = all(
                current.get(member, {}).get("filer_transaction_digest")
                == row.get("filer_transaction_digest")
                for member, row in rows
            )
            surplus_is_held = all(
                set(row.get("surplus") or []).issubset(
                    current.get(member, {}).get("held_ids") or set()
                )
                for member, row in rows
            )
            scope_has_no_missing = (
                not require_no_missing
                or all(not row.get("missing") for _member, row in rows)
            )
            if digest_matches and surplus_is_held and scope_has_no_missing:
                valid.update(rows)
            else:
                blocked.update(members)
    return valid, blocked, schema_error
