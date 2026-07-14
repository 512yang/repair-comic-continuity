"""Full-resolution, hash-bound dual-audit evidence contracts."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pipeline_contracts import canonical_hash, normalize_relative_image_path


HIGH_CONFIDENCE = 0.90
MEDIUM_CONFIDENCE = 0.60

REQUIRED_AUDIT_CHECKS = frozenset(
    {
        "identity",
        "facial_hair",
        "anatomy",
        "costume",
        "prop",
        "scene",
        "style",
        "text",
        "sfx",
    }
)

NON_DEFECT_CODES = frozenset(
    {
        "NON_CRITICAL_ACTION_VARIATION",
        "CAMERA_VARIATION",
        "EXPRESSION_VARIATION",
    }
)

_KNOWN_DEFECT_CODES = frozenset(
    {
        "ANATOMY_ERROR",
        "COSTUME_DRIFT",
        "FACIAL_HAIR_DRIFT",
        "IDENTITY_DRIFT",
        "PROP_DRIFT",
        "SCENE_DRIFT",
        "SFX_ERROR",
        "STYLE_DRIFT",
        "TEXT_ERROR",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PERSPECTIVES = frozenset({"continuity", "source"})
_CLASSIFICATIONS = frozenset({"unchanged", "defect", "evidence_blocked"})
_LOG_LOCKS: dict[str, threading.Lock] = {}
_LOG_LOCKS_GUARD = threading.Lock()


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256")
    return value


def _require_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _require_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a finite number in [0,1]")
    result = float(value)
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        raise ValueError("confidence must be a finite number in [0,1]")
    return result


def _require_nonempty_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_timestamp(value: object) -> str:
    text = _require_nonempty_string(value, "reviewed_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reviewed_at must be an ISO-8601 timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError("reviewed_at must include a timezone")
    return text


def _validate_inspection_rows(
    value: object, field: str, identity_key: str, required_detail: str
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ValueError(f"{field} must be a non-empty structured list")
    rows: list[dict[str, Any]] = []
    identities: set[str] = set()
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise ValueError(f"{field}[{index}] must be structured")
        identity = _require_nonempty_string(row.get(identity_key), f"{field}.{identity_key}")
        details = row.get(required_detail)
        if (
            isinstance(details, (str, bytes))
            or not isinstance(details, Sequence)
            or not details
            or any(not isinstance(item, str) or not item.strip() for item in details)
        ):
            raise ValueError(f"{field}[{index}].{required_detail} must be non-empty")
        if identity in identities:
            raise ValueError(f"duplicate {field} identity: {identity}")
        identities.add(identity)
        rows.append(copy.deepcopy(dict(row)))
    return rows


def _validate_findings(value: object) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ValueError("findings must be a list")
    findings: list[dict[str, Any]] = []
    signatures: set[tuple[str, str, bool]] = set()
    for index, finding in enumerate(value):
        if not isinstance(finding, Mapping):
            raise ValueError(f"finding {index} must be structured")
        code = finding.get("code")
        category = finding.get("category")
        blocking = finding.get("blocking")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"finding {index} code must be non-empty")
        if category not in {"visual", "text", "source"}:
            raise ValueError(f"finding {index} category is invalid")
        if not isinstance(blocking, bool):
            raise ValueError(f"finding {index} blocking must be boolean")
        if code in NON_DEFECT_CODES and blocking:
            raise ValueError(f"finding {index} non-defect code cannot be blocking")
        signature = (code, category, blocking)
        if signature in signatures:
            raise ValueError(f"duplicate finding: {code}")
        signatures.add(signature)
        findings.append(copy.deepcopy(dict(finding)))
    return findings


def record_page_audit(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize one full-resolution audit without mutating input."""
    if not isinstance(audit, Mapping):
        raise ValueError("audit must be a mapping")
    result = copy.deepcopy(dict(audit))
    try:
        result["page"] = normalize_relative_image_path(result.get("page"))
    except ValueError as exc:
        raise ValueError(f"invalid page: {exc}") from exc
    result["source_sha256"] = _require_sha256(
        result.get("source_sha256"), "source_sha256"
    )
    result["width"] = _require_positive_int(result.get("width"), "width")
    result["height"] = _require_positive_int(result.get("height"), "height")
    if result.get("perspective") not in _PERSPECTIVES:
        raise ValueError("perspective must be continuity or source")
    result["reviewer"] = _require_nonempty_string(result.get("reviewer"), "reviewer")
    result["reviewed_at"] = _validate_timestamp(result.get("reviewed_at"))
    result["confidence"] = _require_confidence(result.get("confidence"))
    result["inspected_panels"] = _validate_inspection_rows(
        result.get("inspected_panels"), "inspected_panels", "panel", "checks"
    )
    result["inspected_entities"] = _validate_inspection_rows(
        result.get("inspected_entities"), "inspected_entities", "entity", "regions"
    )

    checks = result.get("checks")
    if not isinstance(checks, Mapping) or set(checks) != REQUIRED_AUDIT_CHECKS:
        raise ValueError("checks must contain exactly the required audit checks")
    if any(not isinstance(value, bool) for value in checks.values()):
        raise ValueError("checks values must be booleans")
    result["checks"] = dict(checks)
    result["findings"] = _validate_findings(result.get("findings"))

    classification = result.get("classification")
    if classification not in _CLASSIFICATIONS:
        raise ValueError("classification is invalid")
    evidence = result.get("classification_evidence")
    if (
        isinstance(evidence, (str, bytes))
        or not isinstance(evidence, Sequence)
        or not evidence
        or any(not isinstance(item, str) or not item.strip() for item in evidence)
    ):
        raise ValueError("classification_evidence must be non-empty")
    result["classification_evidence"] = [item.strip() for item in evidence]
    blocking = [finding for finding in result["findings"] if finding["blocking"]]
    if classification == "unchanged":
        if not all(result["checks"].values()):
            raise ValueError("unchanged requires all required checks to pass")
        if blocking:
            raise ValueError("unchanged cannot contain blocking findings")
    elif classification == "defect" and not blocking:
        raise ValueError("defect classification requires a blocking finding")

    artifact = result.get("artifact")
    if not isinstance(artifact, Mapping):
        raise ValueError("full-resolution artifact is required")
    try:
        artifact_path = normalize_relative_image_path(artifact.get("path"))
    except ValueError as exc:
        raise ValueError(f"invalid artifact path: {exc}") from exc
    if artifact.get("kind") != "full_resolution_page":
        raise ValueError("full-resolution artifact is required; thumbnails are orientation only")
    result["artifact"] = {
        "path": artifact_path,
        "sha256": _require_sha256(artifact.get("sha256"), "artifact sha256"),
        "kind": "full_resolution_page",
    }
    if "perspective_disagreement" in result and not isinstance(
        result["perspective_disagreement"], bool
    ):
        raise ValueError("perspective_disagreement must be boolean")

    result.pop("evidence_hash", None)
    result["evidence_hash"] = canonical_hash(result)
    return result


def route_page_decision(
    findings: Sequence[Mapping[str, Any]],
    confidence: object,
    *,
    perspective_disagreement: bool = False,
) -> str:
    """Route audited evidence; uncertainty is never silently treated as clean."""
    confidence_value = _require_confidence(confidence)
    if not isinstance(perspective_disagreement, bool):
        raise ValueError("perspective_disagreement must be boolean")
    normalized = _validate_findings(findings)
    known_codes = _KNOWN_DEFECT_CODES | NON_DEFECT_CODES
    if perspective_disagreement or any(row["code"] not in known_codes for row in normalized):
        return "evidence_blocked"
    if confidence_value < MEDIUM_CONFIDENCE:
        return "evidence_blocked"
    if confidence_value < HIGH_CONFIDENCE:
        return "second_review_required"
    blocking = [row for row in normalized if row["blocking"]]
    if not blocking:
        return "unchanged"
    if any(row["category"] == "visual" for row in blocking):
        return "full_page_redraw"
    if all(row["category"] == "text" for row in blocking):
        return "text_only"
    return "evidence_blocked"


def aggregate_page_audits(audits: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Enforce the independent continuity/source review gate for one page."""
    if isinstance(audits, (str, bytes)) or not isinstance(audits, Sequence):
        raise ValueError("audits must be a sequence")
    records = [record_page_audit(audit) for audit in audits]
    perspectives = {record["perspective"] for record in records}
    if len(records) != 2 or perspectives != _PERSPECTIVES:
        raise ValueError("both continuity and source perspectives are required")
    by_perspective = {record["perspective"]: record for record in records}
    continuity = by_perspective["continuity"]
    source = by_perspective["source"]
    if continuity["reviewer"] == source["reviewer"]:
        raise ValueError("dual audits require an independent reviewer")
    if continuity["page"] != source["page"]:
        raise ValueError("dual audits must bind the same page")
    if continuity["source_sha256"] != source["source_sha256"]:
        raise ValueError("dual audits must bind the same source_sha256")
    if (continuity["width"], continuity["height"]) != (
        source["width"],
        source["height"],
    ):
        raise ValueError("dual audits must bind the same dimensions")

    findings = continuity["findings"] + source["findings"]
    disagreement = any(
        record.get("perspective_disagreement", False) for record in records
    )
    decision = route_page_decision(
        findings,
        min(record["confidence"] for record in records),
        perspective_disagreement=disagreement,
    )
    result = {
        "page": continuity["page"],
        "source_sha256": continuity["source_sha256"],
        "reviewers": {
            "continuity": continuity["reviewer"],
            "source": source["reviewer"],
        },
        "audit_evidence_hashes": {
            "continuity": continuity["evidence_hash"],
            "source": source["evidence_hash"],
        },
        "decision": decision,
    }
    result["aggregate_hash"] = canonical_hash(result)
    return result


def append_review_event(path: str | Path, event: Mapping[str, Any]) -> dict[str, Any]:
    """Append one canonical event after verifying the complete existing chain.

    Writes occur under a per-path process lock.  If write/flush/fsync fails, only
    the attempted suffix is truncated back to the verified boundary.
    """
    log_path = _validate_log_path(path)
    normalized_event = _normalize_event(event)
    lock_key = str(log_path.resolve(strict=False)).casefold()
    with _LOG_LOCKS_GUARD:
        lock = _LOG_LOCKS.setdefault(lock_key, threading.Lock())
    with lock:
        if log_path.exists() and log_path.is_symlink():
            raise ValueError("review log path must not be a symlink")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a+b") as handle:
            handle.seek(0)
            existing = handle.read()
            rows = _parse_review_log(existing)
            previous = rows[-1]["event_hash"] if rows else None
            row = {**normalized_event, "previous_event_hash": previous}
            row["event_hash"] = canonical_hash(row)
            payload = (
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            original_size = len(existing)
            try:
                handle.seek(0, os.SEEK_END)
                written = handle.write(payload)
                if written != len(payload):
                    raise OSError("short append")
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                handle.seek(original_size)
                handle.truncate(original_size)
                handle.flush()
                os.fsync(handle.fileno())
                raise
    return copy.deepcopy(row)


def validate_review_log(path: str | Path) -> list[dict[str, Any]]:
    """Verify every event hash/link and return defensive copies of the rows."""
    log_path = _validate_log_path(path)
    if not log_path.exists() or not log_path.is_file() or log_path.is_symlink():
        raise ValueError("review log does not exist as a regular file")
    try:
        payload = log_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"review log cannot be read: {exc}") from exc
    return copy.deepcopy(_parse_review_log(payload))


def _validate_log_path(value: object) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError("review log path must be path-like")
    path = Path(value)
    if path.suffix.casefold() != ".jsonl":
        raise ValueError("review log path must use .jsonl")
    if not path.name or any(part == ".." for part in path.parts):
        raise ValueError("review log path is unsafe")
    return path


def _normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ValueError("review event must be a mapping")
    if "event_hash" in event or "previous_event_hash" in event:
        raise ValueError("review event contains reserved chain fields")
    result = copy.deepcopy(dict(event))
    result["type"] = _require_nonempty_string(result.get("type"), "event type")
    if "page" in result:
        try:
            result["page"] = normalize_relative_image_path(result["page"])
        except ValueError as exc:
            raise ValueError(f"invalid event page: {exc}") from exc
    if "evidence_hash" in result:
        result["evidence_hash"] = _require_sha256(
            result["evidence_hash"], "event evidence_hash"
        )
    # This both proves serializability and rejects NaN/Infinity before any write.
    canonical_hash(result)
    return result


def _parse_review_log(payload: bytes) -> list[dict[str, Any]]:
    if not payload:
        return []
    if not payload.endswith(b"\n"):
        raise ValueError("review log is truncated (missing final newline)")
    raw_lines = payload.split(b"\n")[:-1]
    if not raw_lines or any(not line for line in raw_lines):
        raise ValueError("review log contains a blank line")
    rows: list[dict[str, Any]] = []
    expected_previous: str | None = None
    for index, raw_line in enumerate(raw_lines, start=1):
        try:
            decoded = raw_line.decode("utf-8")
            row = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"review log line {index} is malformed") from exc
        if not isinstance(row, dict):
            raise ValueError(f"review log line {index} must be an object")
        if row.get("previous_event_hash") != expected_previous:
            raise ValueError(f"review log line {index} has a broken previous hash")
        claimed = row.get("event_hash")
        try:
            _require_sha256(claimed, f"review log line {index} event_hash")
        except ValueError as exc:
            raise ValueError(f"review log line {index} has an invalid event hash") from exc
        hash_payload = dict(row)
        del hash_payload["event_hash"]
        if canonical_hash(hash_payload) != claimed:
            raise ValueError(f"review log line {index} event hash mismatch")
        # Canonical bytes make equivalent-but-rewritten history detectable.
        canonical_line = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if canonical_line != raw_line:
            raise ValueError(f"review log line {index} is not canonical")
        rows.append(row)
        expected_previous = claimed
    return rows
