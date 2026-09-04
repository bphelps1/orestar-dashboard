"""Paired ORESTAR/app balance snapshots.

An ORESTAR account summary and the dashboard transaction set are both
snapshots.  Comparing today's dashboard balance with an older ORESTAR page
creates a difference even when both snapshots were correct when collected.

This module makes the comparison explicit and immutable:

* ``transaction_snapshot_id`` fingerprints the exact transaction shards used
  by an aggregation.
* ``make_summary_capture`` pairs a freshly-read ORESTAR balance with the app
  balance already calculated from that same fingerprint.
* ``paired_comparison`` combines physical filer IDs only when every component
  was paired to the same app snapshot.

Filing dates are deliberately absent.  They describe when a filer submitted a
row, not when this application first collected it, and therefore cannot
reconstruct the application's historical state.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FORMAT_VERSION = 2
CALCULATION_VERSION = "cash-balance-v1"
SOURCE_FILENAME = "balance_snapshot_source.json"
CAPTURE_KEY = "comparison_capture"


def normalize_filer_id(value: Any) -> str:
    """Return a stable filer id without pandas' occasional ``.0`` suffix."""
    text = str(value or "").strip()
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def scope_key(filer_ids: list[str] | tuple[str, ...]) -> str:
    """Stable key for one canonical committee scope."""
    return "|".join(sorted({normalize_filer_id(fid) for fid in filer_ids if normalize_filer_id(fid)}))


def transaction_snapshot_id(transaction_dir: Path) -> str | None:
    """Fingerprint the exact compressed transaction shards in use.

    Hashing the files, rather than using a git revision, also works for a
    freshly merged (not yet committed) dataset and detects stale aggregates in
    a checkout that advanced while an ORESTAR job waited for its turn.
    """
    paths = sorted(transaction_dir.glob("txn_*.csv.gz"))
    if not paths:
        return None

    digest = hashlib.sha256(b"orestar-transaction-snapshot-v1\0")
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def evidence_is_current(evidence: dict | None, captured_at: Any) -> bool:
    """Whether timestamped supporting evidence postdates a captured summary."""
    checked = (evidence or {}).get("checked_at") or (evidence or {}).get("checked")
    if not checked or not captured_at:
        return False
    try:
        checked_dt = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
        if checked_dt.tzinfo is None:
            checked_dt = checked_dt.replace(tzinfo=timezone.utc)
        captured_dt = datetime.fromtimestamp(float(captured_at), tz=timezone.utc)
        return checked_dt >= captured_dt
    except (TypeError, ValueError, OverflowError):
        return False


def build_source(
    transaction_id: str | None,
    scopes: list[dict],
    *,
    created_at: str,
) -> dict:
    """Build the small app-side source read by the account-summary scraper."""
    records: dict[str, dict] = {}
    for raw in scopes:
        ids = sorted({normalize_filer_id(fid) for fid in raw.get("filer_ids", [])
                      if normalize_filer_id(fid)})
        key = scope_key(ids)
        if not key:
            continue
        record = {
            "filer_ids": ids,
            "name": str(raw.get("name") or ""),
            "slug": str(raw.get("slug") or ""),
            "cash_on_hand": round(float(raw.get("cash_on_hand") or 0.0), 2),
            "tran_count": int(raw.get("tran_count") or 0),
        }
        previous = records.get(key)
        if previous is None:
            records[key] = record
        else:
            # One physical filer can appear under several unresolved canonical
            # names.  Each profile then contains only a slice of that filer's
            # transactions, while ORESTAR exposes one indivisible balance.  A
            # dict overwrite silently paired every slice to whichever profile
            # happened to be written last.  Preserve the collision and refuse
            # the comparison until those names are canonicalized.
            if previous.get("status") == "ambiguous":
                profiles = previous["profiles"]
            else:
                profiles = [previous]
            profiles.append(record)
            records[key] = {
                "status": "ambiguous",
                "reason": "duplicate_app_scope",
                "filer_ids": ids,
                "profiles": profiles,
            }
    return {
        "version": FORMAT_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "created_at": created_at,
        "transaction_snapshot_id": transaction_id,
        "scopes": records,
    }


def _scope_for_filer(source: dict, filer_id: str) -> tuple[str, dict] | None:
    fid = normalize_filer_id(filer_id)
    matches = []
    for key, record in (source.get("scopes") or {}).items():
        ids = {normalize_filer_id(value) for value in record.get("filer_ids", [])}
        if fid in ids:
            matches.append((str(key), record))
    # A filer must belong to exactly one canonical scope.  Ambiguity is safer
    # to leave unpaired than to compare against the wrong aggregate.
    if len(matches) != 1 or matches[0][1].get("status") == "ambiguous":
        return None
    return matches[0]


def make_summary_capture(
    filer_id: str,
    year: int,
    summary: dict,
    captured_at: float,
    source: dict | None,
    current_transaction_id: str | None,
) -> dict:
    """Pair a freshly parsed ORESTAR page with a precomputed app snapshot."""
    capture = {
        "version": FORMAT_VERSION,
        "status": "unpaired",
        "captured_at": float(captured_at),
        "orestar_year": int(year),
        "orestar_ending_cash_balance": round(
            float(summary.get("ending_cash_balance") or 0.0), 2
        ),
    }

    if not source:
        capture["reason"] = "app_snapshot_source_missing"
        return capture
    if source.get("version") != FORMAT_VERSION:
        capture["reason"] = "app_snapshot_source_version"
        return capture
    if source.get("calculation_version") != CALCULATION_VERSION:
        capture["reason"] = "app_snapshot_calculation_version"
        return capture
    source_transaction_id = source.get("transaction_snapshot_id")
    if not current_transaction_id or source_transaction_id != current_transaction_id:
        capture["reason"] = "app_snapshot_source_stale"
        capture["app_transaction_snapshot_id"] = source_transaction_id
        return capture

    match = _scope_for_filer(source, filer_id)
    if not match:
        capture["reason"] = "app_snapshot_scope_missing_or_ambiguous"
        return capture
    key, record = match
    ids = sorted({normalize_filer_id(value) for value in record.get("filer_ids", [])
                  if normalize_filer_id(value)})

    capture.update({
        "status": "paired",
        "app_scope_key": key,
        "app_scope_filer_ids": ids,
        "app_cash_on_hand": round(float(record.get("cash_on_hand") or 0.0), 2),
        "app_tran_count": int(record.get("tran_count") or 0),
        "app_transaction_snapshot_id": current_transaction_id,
        "app_snapshot_created_at": source.get("created_at"),
        "calculation_version": source.get("calculation_version"),
    })
    return capture


def paired_comparison(
    filer_ids: list[str],
    yearly_cache: dict,
    *,
    current_transaction_id: str | None = None,
) -> dict:
    """Return one authoritative comparison for a canonical filer scope.

    Multi-ID canonical names are comparable only when every physical filer was
    captured against the same app-side aggregate.  This prevents summing
    ORESTAR balances collected at unrelated app revisions and pretending the
    newest component timestamp applies to all of them.
    """
    ids = sorted({normalize_filer_id(fid) for fid in filer_ids if normalize_filer_id(fid)})
    if not ids:
        return {"status": "unpaired", "reason": "no_filer_ids"}

    captures = []
    newer_unpaired_attempts = []
    for fid in ids:
        entry = yearly_cache.get(fid) or {}
        capture = entry.get(CAPTURE_KEY)
        if not capture:
            return {"status": "legacy_unpaired", "reason": "capture_missing"}
        if capture.get("version") != FORMAT_VERSION:
            return {"status": "unpaired", "reason": "capture_version_mismatch"}
        if capture.get("status") != "paired":
            return {
                "status": "unpaired",
                "reason": capture.get("reason") or "component_unpaired",
            }
        captures.append(capture)
        attempt = entry.get("comparison_capture_attempt") or {}
        if (attempt
                and float(attempt.get("captured_at") or 0)
                > float(capture.get("captured_at") or 0)):
            newer_unpaired_attempts.append(attempt)

    scope = scope_key(ids)
    transaction_ids = {str(c.get("app_transaction_snapshot_id") or "") for c in captures}
    scope_keys = {str(c.get("app_scope_key") or "") for c in captures}
    scope_members = {
        scope_key(c.get("app_scope_filer_ids") or [])
        for c in captures
    }
    calculation_versions = {str(c.get("calculation_version") or "") for c in captures}
    scope_capture_ids = {str(c.get("scope_capture_id") or "") for c in captures}
    app_balances = {round(float(c.get("app_cash_on_hand") or 0.0), 2) for c in captures}
    app_counts = {int(c.get("app_tran_count") or 0) for c in captures}

    if (len(transaction_ids) != 1 or "" in transaction_ids
            or scope_keys != {scope} or scope_members != {scope}
            or calculation_versions != {CALCULATION_VERSION}
            or (len(ids) > 1
                and (len(scope_capture_ids) != 1 or "" in scope_capture_ids))
            or len(app_balances) != 1 or len(app_counts) != 1):
        return {"status": "unpaired", "reason": "component_snapshot_mismatch"}

    app_cash = next(iter(app_balances))
    orestar_cash = round(sum(float(c.get("orestar_ending_cash_balance") or 0.0)
                             for c in captures), 2)
    captured = [float(c.get("captured_at") or 0.0) for c in captures]
    transaction_id = next(iter(transaction_ids))
    return {
        "status": "paired",
        "basis": "paired_collection_snapshot",
        "capture_started_at": min(captured),
        "captured_at": max(captured),
        "app_cash_on_hand": app_cash,
        "orestar_cash_on_hand": orestar_cash,
        "delta_at_capture": round(app_cash - orestar_cash, 2),
        "app_tran_count": next(iter(app_counts)),
        "app_transaction_snapshot_id": transaction_id,
        "current_transaction_snapshot_id": current_transaction_id,
        "app_data_changed_since_capture": bool(
            current_transaction_id and transaction_id != current_transaction_id
        ),
        "orestar_data_changed_since_capture": bool(newer_unpaired_attempts),
        "latest_unpaired_capture_at": (
            max(float(a.get("captured_at") or 0) for a in newer_unpaired_attempts)
            if newer_unpaired_attempts else None
        ),
        "app_snapshot_created_at": captures[0].get("app_snapshot_created_at"),
        "calculation_version": next(iter(calculation_versions)),
        "scope_capture_id": next(iter(scope_capture_ids), None) or None,
        "filer_ids": ids,
    }
