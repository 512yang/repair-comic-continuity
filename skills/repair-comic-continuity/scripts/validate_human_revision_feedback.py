"""Validate and persist immutable user feedback from output review rounds."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_KEYS = frozenset(
    {
        "version",
        "status",
        "confirmed_by",
        "confirmed_at",
        "attempt",
        "feedback",
        "learning_policy",
    }
)
FEEDBACK_KEYS = frozenset(
    {
        "feedback_id",
        "origin",
        "attempt",
        "page",
        "candidate",
        "parent_annotation_ids",
        "regions",
        "user_note",
    }
)
ARTIFACT_KEYS = frozenset({"path", "sha256"})
REGION_KEYS = frozenset({"region_id", "bbox_norm", "description"})
ORIGINS = frozenset(
    {
        "missed_detection",
        "unresolved",
        "introduced_error",
        "damaged_correct_content",
        "text_result_error",
    }
)
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise ValueError(f"{field} must be a nonempty string")
    return normalized


def _safe_relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} must be a safe relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"{field} must be a safe relative path")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: object) -> str:
    text = _text(value, "confirmed_at")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("confirmed_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("confirmed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _positive_attempt(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _artifact(
    value: object,
    field: str,
    *,
    root: Path,
    mismatch_label: str,
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != ARTIFACT_KEYS:
        raise ValueError(f"{field} must contain exact path and sha256 keys")
    relative = _safe_relative_path(value.get("path"), f"{field}.path")
    digest = value.get("sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{field}.sha256 is invalid")
    path = root.joinpath(*PurePosixPath(relative).parts)
    if not path.is_file() or _sha256_file(path) != digest.lower():
        raise ValueError(f"{mismatch_label} sha256 mismatch: {relative}")
    return {"path": relative, "sha256": digest.lower()}


def _bbox(value: object, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{field}.bbox_norm must contain four numbers")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{field}.bbox_norm must contain four numbers")
        number = float(item)
        if not math.isfinite(number) or number < 0 or number > 1:
            raise ValueError(f"{field}.bbox_norm must remain within [0, 1]")
        result.append(number)
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError(f"{field}.bbox_norm must have positive area")
    return result


def validate_human_revision_feedback(
    document: dict[str, Any],
    input_dir: Path,
    input_names: list[str],
    run_root: Path,
) -> dict[str, Any]:
    if not isinstance(document, dict) or set(document) != MANIFEST_KEYS:
        raise ValueError("human revision feedback must contain exact keys")
    if (
        document.get("version") != 1
        or document.get("status") != "confirmed"
        or document.get("learning_policy") != "evidence_gated"
    ):
        raise ValueError("human revision feedback contract mismatch")
    attempt = _positive_attempt(document.get("attempt"), "attempt")
    confirmed_by = _text(document.get("confirmed_by"), "confirmed_by")
    confirmed_at = _timestamp(document.get("confirmed_at"))
    rows = document.get("feedback")
    if not isinstance(rows, list) or not rows:
        raise ValueError("human revision feedback must be nonempty")

    positions = {name: index for index, name in enumerate(input_names)}
    seen_feedback: set[str] = set()
    page_positions: list[int] = []
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        field = f"feedback[{index}]"
        if not isinstance(row, dict) or set(row) != FEEDBACK_KEYS:
            raise ValueError(f"{field} must contain exact keys")
        feedback_id = _text(row.get("feedback_id"), f"{field}.feedback_id")
        if feedback_id in seen_feedback:
            raise ValueError(f"duplicate feedback_id: {feedback_id}")
        seen_feedback.add(feedback_id)
        row_attempt = _positive_attempt(row.get("attempt"), f"{field}.attempt")
        if row_attempt != attempt:
            raise ValueError(f"{field}.attempt does not match manifest attempt")
        origin = row.get("origin")
        if not isinstance(origin, str) or origin not in ORIGINS:
            raise ValueError(f"{field}.origin is unsupported")

        page_value = row.get("page")
        if not isinstance(page_value, dict) or set(page_value) != ARTIFACT_KEYS:
            raise ValueError(f"{field}.page must contain exact path and sha256 keys")
        page_path = _safe_relative_path(page_value.get("path"), f"{field}.page.path")
        if page_path not in positions:
            raise ValueError(f"revision feedback references unknown input page: {page_path}")
        page = _artifact(
            page_value,
            f"{field}.page",
            root=Path(input_dir),
            mismatch_label="source page",
        )
        candidate = _artifact(
            row.get("candidate"),
            f"{field}.candidate",
            root=Path(run_root),
            mismatch_label="candidate",
        )

        parents = row.get("parent_annotation_ids")
        if not isinstance(parents, list):
            raise ValueError(f"{field}.parent_annotation_ids must be a list")
        normalized_parents = [
            _text(parent, f"{field}.parent_annotation_ids") for parent in parents
        ]
        if len(normalized_parents) != len(set(normalized_parents)):
            raise ValueError(f"{field}.parent_annotation_ids must be unique")

        regions = row.get("regions")
        if not isinstance(regions, list) or not regions:
            raise ValueError(f"{field}.regions must be nonempty")
        normalized_regions = []
        seen_regions: set[str] = set()
        for region_index, region in enumerate(regions):
            region_field = f"{field}.regions[{region_index}]"
            if not isinstance(region, dict) or set(region) != REGION_KEYS:
                raise ValueError(f"{region_field} must contain exact keys")
            region_id = _text(region.get("region_id"), f"{region_field}.region_id")
            if region_id in seen_regions:
                raise ValueError(f"duplicate region_id in {feedback_id}: {region_id}")
            seen_regions.add(region_id)
            normalized_regions.append(
                {
                    "region_id": region_id,
                    "bbox_norm": _bbox(region.get("bbox_norm"), region_field),
                    "description": _text(
                        region.get("description"), f"{region_field}.description"
                    ),
                }
            )
        normalized_rows.append(
            {
                "feedback_id": feedback_id,
                "origin": origin,
                "attempt": attempt,
                "page": page,
                "candidate": candidate,
                "parent_annotation_ids": normalized_parents,
                "regions": normalized_regions,
                "user_note": _text(row.get("user_note"), f"{field}.user_note"),
            }
        )
        page_positions.append(positions[page_path])
    if page_positions != sorted(page_positions):
        raise ValueError("revision feedback must follow natural input order")
    return {
        "mode": "revision_feedback",
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
        "attempt": attempt,
        "feedback": normalized_rows,
        "feedback_count": len(normalized_rows),
        "learning_policy": "evidence_gated",
    }


def write_revision_feedback_manifest(path: Path, document: dict[str, Any]) -> None:
    target = Path(path)
    if target.exists():
        raise ValueError(f"revision feedback attempt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=target.name + ".", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ValueError(f"revision feedback attempt already exists: {target}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
