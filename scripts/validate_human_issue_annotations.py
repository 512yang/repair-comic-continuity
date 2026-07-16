"""Validate user-confirmed visual issue regions for controlled comic repair learning."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from closed_loop_controller import TRAIT_CODES
from failure_learning import FAILURE_CODES


MANIFEST_KEYS = frozenset(
    {
        "version",
        "status",
        "mode",
        "confirmed_by",
        "confirmed_at",
        "annotations",
        "learning_policy",
        "persistence_policy",
    }
)
ANNOTATION_KEYS = frozenset(
    {
        "annotation_id",
        "page",
        "regions",
        "targets",
        "defect_codes",
        "trait_codes",
        "observed_state",
        "required_state",
        "instruction",
    }
)
REQUIRED_ANNOTATION_KEYS = ANNOTATION_KEYS - {"trait_codes"}
PAGE_KEYS = frozenset({"path", "sha256"})
REGION_KEYS = frozenset({"region_id", "bbox_norm", "description"})
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a nonempty string")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if not normalized:
        raise ValueError(f"{field} must be a nonempty string")
    return normalized


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("annotation page must be a safe relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("annotation page must be a safe relative path")
    return value


def _sha256(path: Path) -> str:
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


def _bbox(value: object, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"{field} bbox_norm must contain four numbers")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError(f"{field} bbox_norm must contain four numbers")
        number = float(item)
        if not math.isfinite(number) or number < 0.0 or number > 1.0:
            raise ValueError(f"{field} bbox_norm must remain within [0, 1]")
        result.append(number)
    left, top, right, bottom = result
    if left >= right or top >= bottom:
        raise ValueError(f"{field} bbox_norm must have positive area")
    return result


def validate_human_issue_annotations(
    document: dict[str, Any],
    input_dir: Path,
    input_names: list[str],
    selected_pages: list[str],
) -> dict[str, Any]:
    """Return normalized annotation evidence after strict schema and source checks."""
    if not isinstance(document, dict) or set(document) != MANIFEST_KEYS:
        raise ValueError("human issue annotations must contain exact keys")
    if (
        document.get("version") != 1
        or document.get("status") != "confirmed"
        or document.get("mode") != "human_visual_auto_text"
        or document.get("learning_policy") != "evidence_gated"
        or document.get("persistence_policy")
        != "page_cluster_project_skill_candidate"
    ):
        raise ValueError("human issue annotation contract mismatch")

    confirmed_by = _text(document.get("confirmed_by"), "confirmed_by")
    confirmed_at = _timestamp(document.get("confirmed_at"))
    rows = document.get("annotations")
    if not isinstance(rows, list) or not rows:
        raise ValueError("human issue annotations must be nonempty")

    positions = {name: index for index, name in enumerate(input_names)}
    selected = set(selected_pages)
    seen_annotations: set[str] = set()
    normalized_rows: list[dict[str, Any]] = []
    page_positions: list[int] = []
    annotated_pages: list[str] = []
    region_count = 0
    target_count = 0

    for index, row in enumerate(rows):
        field = f"annotations[{index}]"
        if (
            not isinstance(row, dict)
            or not REQUIRED_ANNOTATION_KEYS.issubset(row)
            or not set(row).issubset(ANNOTATION_KEYS)
        ):
            raise ValueError(f"{field} must contain exact keys")
        annotation_id = _text(row.get("annotation_id"), f"{field}.annotation_id")
        if annotation_id in seen_annotations:
            raise ValueError(f"duplicate annotation_id: {annotation_id}")
        seen_annotations.add(annotation_id)

        page = row.get("page")
        if not isinstance(page, dict) or set(page) != PAGE_KEYS:
            raise ValueError(f"{field}.page must contain exact keys")
        page_path = _safe_relative_path(page.get("path"))
        if page_path not in positions:
            raise ValueError(f"annotation references unknown input page: {page_path}")
        if page_path not in selected:
            raise ValueError(
                f"annotation page is not in human visual selection: {page_path}"
            )
        digest = page.get("sha256")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"annotation page sha256 is invalid: {page_path}")
        source = Path(input_dir) / Path(*PurePosixPath(page_path).parts)
        if not source.is_file() or _sha256(source) != digest.lower():
            raise ValueError(f"annotation page sha256 mismatch: {page_path}")

        regions = row.get("regions")
        if not isinstance(regions, list) or not regions:
            raise ValueError(f"{field}.regions must be nonempty")
        seen_regions: set[str] = set()
        normalized_regions: list[dict[str, Any]] = []
        for region_index, region in enumerate(regions):
            region_field = f"{field}.regions[{region_index}]"
            if not isinstance(region, dict) or set(region) != REGION_KEYS:
                raise ValueError(f"{region_field} must contain exact keys")
            region_id = _text(region.get("region_id"), f"{region_field}.region_id")
            if region_id in seen_regions:
                raise ValueError(f"duplicate region_id in {annotation_id}: {region_id}")
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

        targets = row.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError(f"{field}.targets must be nonempty")
        normalized_targets = [
            _text(target, f"{field}.targets") for target in targets
        ]
        if len(set(normalized_targets)) != len(normalized_targets):
            raise ValueError(f"duplicate target in {annotation_id}")

        codes = row.get("defect_codes")
        if not isinstance(codes, list) or not codes:
            raise ValueError(f"{field}.defect_codes must be nonempty")
        normalized_codes = []
        for code in codes:
            normalized_code = _text(code, f"{field}.defect_codes")
            if normalized_code not in FAILURE_CODES:
                raise ValueError(f"unknown defect code: {normalized_code}")
            normalized_codes.append(normalized_code)

        traits = row.get("trait_codes", [])
        if not isinstance(traits, list):
            raise ValueError(f"{field}.trait_codes must be a list")
        normalized_traits = [
            _text(trait, f"{field}.trait_codes") for trait in traits
        ]
        if len(set(normalized_traits)) != len(normalized_traits):
            raise ValueError(f"duplicate trait code in {annotation_id}")
        unknown_traits = [
            trait for trait in normalized_traits if trait not in TRAIT_CODES
        ]
        if unknown_traits:
            raise ValueError(f"unknown trait code: {unknown_traits[0]}")

        normalized_rows.append(
            {
                "annotation_id": annotation_id,
                "page": {"path": page_path, "sha256": digest.lower()},
                "regions": normalized_regions,
                "targets": normalized_targets,
                "defect_codes": sorted(set(normalized_codes)),
                "trait_codes": sorted(normalized_traits),
                "observed_state": _text(
                    row.get("observed_state"), f"{field}.observed_state"
                ),
                "required_state": _text(
                    row.get("required_state"), f"{field}.required_state"
                ),
                "instruction": _text(row.get("instruction"), f"{field}.instruction"),
            }
        )
        page_positions.append(positions[page_path])
        if page_path not in annotated_pages:
            annotated_pages.append(page_path)
        region_count += len(normalized_regions)
        target_count += len(normalized_targets)

    if page_positions != sorted(page_positions):
        raise ValueError("annotations must follow natural input order")
    return {
        "mode": "human_visual_auto_text",
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
        "annotations": normalized_rows,
        "annotated_pages": annotated_pages,
        "annotation_count": len(normalized_rows),
        "region_count": region_count,
        "target_count": target_count,
        "learning_policy": "evidence_gated",
        "persistence_policy": "page_cluster_project_skill_candidate",
    }
