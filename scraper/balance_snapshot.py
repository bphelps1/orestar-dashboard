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

import csv
import gzip
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


FORMAT_VERSION = 2
CALCULATION_VERSION = "cash-balance-v1"
SOURCE_FILENAME = "balance_snapshot_source.json"
CAPTURE_KEY = "comparison_capture"
COVERAGE_EVIDENCE_VERSION = 2
FILER_DIGEST_VERSION = "orestar-filer-transaction-v1"


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


def _normalized_identifier(value: Any) -> str:
    """Canonicalize identifiers as process.py/Supabase do."""
    text = str(value or "").strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def _transaction_date(value: Any, path: Path, line: int) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unparseable transaction date {text!r} at {path}:{line}")


def transaction_filer_snapshots(
    transaction_dir: Path,
    filer_ids,
    start: date,
    end: date,
) -> dict[str, dict]:
    """Logical per-filer snapshots over one inclusive transaction range.

    The global shard hash proves which checkout a diff used. It cannot remain
    a liveness test after an unrelated committee changes, because any changed
    gzip byte replaces that global hash. These digests hash the canonical CSV
    content for each physical filer instead. Row order, gzip metadata, and the
    scraper's ``_source_file`` bookkeeping do not affect the result; any
    transaction identity or substantive field does.

    Every shard must expose recognized filer/original-ID columns even when it
    has no requested rows. Silently treating a schema-mismatched shard as empty
    would produce a plausible but incomplete digest, so schema omissions raise.
    """
    if end < start:
        raise ValueError("transaction snapshot range end precedes start")
    targets = {
        _normalized_identifier(fid) for fid in filer_ids
        if _normalized_identifier(fid)
    }
    records: dict[str, list[tuple[str, bytes]]] = {fid: [] for fid in targets}
    held: dict[str, set[str]] = {fid: set() for fid in targets}
    superseded: dict[str, set[str]] = {fid: set() for fid in targets}
    paths = sorted(transaction_dir.glob("txn_*.csv.gz"))
    if not paths:
        raise ValueError("no local transaction shards")

    for path in paths:
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            filer_column = "filer id" if "filer id" in fields else (
                "filer_id" if "filer_id" in fields else None
            )
            original_column = "original id" if "original id" in fields else (
                "original_id" if "original_id" in fields else None
            )
            missing = [
                name for name, present in (
                    ("tran_id", "tran_id" in fields),
                    ("tran_date", "tran_date" in fields),
                    ("filer id/filer_id", filer_column is not None),
                    ("original id/original_id", original_column is not None),
                ) if not present
            ]
            if missing:
                raise ValueError(
                    f"transaction shard lacks required column(s) "
                    f"{', '.join(missing)}: {path}"
                )

            for line, row in enumerate(reader, start=2):
                fid = _normalized_identifier(row.get(filer_column))
                if fid not in targets:
                    continue
                tran_date = _transaction_date(row.get("tran_date"), path, line)
                if tran_date is None:
                    raise ValueError(
                        f"target transaction has no date at {path}:{line}"
                    )
                if tran_date < start or tran_date > end:
                    continue
                tran_id = _normalized_identifier(row.get("tran_id"))
                if not tran_id:
                    raise ValueError(f"target transaction has no ID at {path}:{line}")
                original_id = _normalized_identifier(row.get(original_column))
                held[fid].add(tran_id)
                if original_id and original_id != tran_id:
                    superseded[fid].add(original_id)

                canonical = {}
                for key, value in row.items():
                    if key is None or key == "_source_file":
                        continue
                    canonical_key = (
                        "filer_id" if key in {"filer id", "filer_id"}
                        else "original_id" if key in {"original id", "original_id"}
                        else key
                    )
                    canonical[canonical_key] = (
                        _normalized_identifier(value)
                        if canonical_key in {"tran_id", "filer_id", "original_id"}
                        else str(value or "")
                    )
                encoded = json.dumps(
                    canonical, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                records[fid].append((tran_id, encoded))

    out = {}
    for fid in targets:
        digest = hashlib.sha256(f"{FILER_DIGEST_VERSION}\0".encode("ascii"))
        for component in (fid, start.isoformat(), end.isoformat()):
            digest.update(component.encode("utf-8"))
            digest.update(b"\0")
        for tran_id, encoded in sorted(records[fid]):
            digest.update(tran_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(encoded)
            digest.update(b"\0")
        out[fid] = {
            "held_ids": held[fid],
            "superseded_ids": superseded[fid],
            "filer_transaction_digest": f"sha256:{digest.hexdigest()}",
        }
    return out


def exact_evidence_identifier_is_valid(value: Any) -> bool:
    """Identifiers in exact provenance must be real, canonical strings."""
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def exact_coverage_result_shape_is_valid(row: Any) -> bool:
    """Whether an exact-diff result has a coherent identity-set verdict."""
    if not isinstance(row, dict) or type(row.get("complete")) is not bool:
        return False
    missing = row.get("missing")
    surplus = row.get("surplus")
    superseded = row.get("superseded")
    if not all(isinstance(values, list) for values in (
        missing, surplus, superseded,
    )):
        return False
    sets = []
    for values in (missing, surplus, superseded):
        if any(not isinstance(value, str) or not value.strip() for value in values):
            return False
        value_set = set(values)
        if len(values) != len(value_set):
            return False
        sets.append(value_set)
    if sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2]:
        return False
    digest = row.get("filer_transaction_digest")
    if not exact_evidence_identifier_is_valid(digest):
        return False
    return row["complete"] == (not missing and not surplus)


def utc_timestamp() -> str:
    """A precise, unambiguous UTC timestamp for persisted evidence."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _as_utc_datetime(value: Any, *, precise: bool = False) -> datetime | None:
    """Parse an epoch or ISO timestamp, optionally requiring an explicit UTC time.

    Historical coverage files contain ``YYYY-MM-DD`` values.  They remain
    parseable for display and conservative freshness ordering, but they cannot
    establish which of two same-day snapshots came first.  ``precise=True`` is
    the automation boundary: it requires a time, an explicit timezone, and a
    zero UTC offset.
    """
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            if precise:
                return None
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        text = str(value).strip()
        has_explicit_zone = (
            text.endswith("Z") or "+" in text[10:] or "-" in text[10:]
        )
        if precise and ("T" not in text or not has_explicit_zone):
            return None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            if precise:
                return None
            parsed = parsed.replace(tzinfo=timezone.utc)
        if precise and parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            return None
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _as_iso_date(value: Any) -> str | None:
    """Return a canonical ISO date, rejecting timestamps and loose strings."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        text = str(value).strip()
        return date.fromisoformat(text).isoformat()
    except (TypeError, ValueError):
        return None


def evidence_is_current(
    evidence: dict | None,
    captured_at: Any,
    *,
    require_precise: bool = False,
    require_collection_started: bool = False,
    strictly_after: bool = False,
    transaction_snapshot_id: str | None = None,
    filer_transaction_digest: str | None = None,
    range_start: Any = None,
    range_end: Any = None,
    minimum_range_end: Any = None,
) -> bool:
    """Whether supporting evidence is current for the snapshot and range.

    With no keyword constraints this intentionally preserves the legacy
    reader: date-only ``checked`` values still sort conservatively for UI and
    history.  Automation supplies ``require_precise=True`` plus the immutable
    transaction fingerprint and query bounds.  A legacy row, a mismatched
    snapshot, or a partial/different range then fails closed.
    """
    row = evidence or {}
    if require_precise:
        checked_dt = _as_utc_datetime(row.get("checked_at"), precise=True)
        if row.get("evidence_version") != COVERAGE_EVIDENCE_VERSION:
            return False
    else:
        checked_dt = _as_utc_datetime(
            row.get("checked_at") or row.get("checked"), precise=False
        )
    captured_dt = _as_utc_datetime(captured_at, precise=False)
    if checked_dt is None or captured_dt is None:
        return False
    too_old = (
        checked_dt <= captured_dt if strictly_after else checked_dt < captured_dt
    )
    if too_old:
        return False
    if require_collection_started:
        collection_started = _as_utc_datetime(
            row.get("collection_started_at"), precise=True,
        )
        # Completion after a summary is insufficient when the query itself
        # began against ORESTAR before that summary was captured. Also reject
        # impossible start/completion ordering as corrupt provenance.
        if (collection_started is None
                or collection_started <= captured_dt
                or collection_started > checked_dt):
            return False

    if transaction_snapshot_id is not None:
        if (not exact_evidence_identifier_is_valid(transaction_snapshot_id)
                or row.get("transaction_snapshot_id") != transaction_snapshot_id):
            return False
    if filer_transaction_digest is not None:
        if (not exact_evidence_identifier_is_valid(filer_transaction_digest)
                or row.get("filer_transaction_digest")
                != filer_transaction_digest):
            return False

    expected_start = _as_iso_date(range_start) if range_start is not None else None
    expected_end = _as_iso_date(range_end) if range_end is not None else None
    minimum_end = (
        _as_iso_date(minimum_range_end) if minimum_range_end is not None else None
    )
    actual_start = _as_iso_date(row.get("range_start"))
    actual_end = _as_iso_date(row.get("range_end"))
    if (range_start is not None
            and (expected_start is None or actual_start != expected_start)):
        return False
    if (range_end is not None
            and (expected_end is None or actual_end != expected_end)):
        return False
    if minimum_range_end is not None:
        if minimum_end is None or actual_end is None or actual_end < minimum_end:
            return False
    return True


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
