"""Deterministic machine preflight and independent review for comic candidates."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageStat,
    UnidentifiedImageError,
)

from pipeline_contracts import (
    canonical_hash,
    normalize_relative_image_path,
    validate_bijection,
)
from prompt_compiler import validate_v4_text_repair_request


SCHEMA_VERSION = "1.0"
REVIEW_SCHEMA_VERSION = "1.0"
REQUIRED_CHECKS = (
    "decodable",
    "dimensions",
    "blank_page",
    "edge_density_delta",
    "rgb_mean_delta",
    "rgb_std_delta",
    "text_policy",
)
CHECK_STATUSES = frozenset({"pass", "fail", "blocked", "not_applicable"})
REPORT_STATUSES = frozenset({"pass", "fail", "blocked"})
TEXT_POLICIES = frozenset(
    {
        "textless",
        "deterministic_text",
        "preserve_art_text",
        "preserve_text",
        "preserve_source_text",
    }
)
PAGE_CLASSES = frozenset({"unchanged", "text_only", "full_page_redraw"})
REVIEW_DECISIONS = frozenset({"accepted", "rejected", "needs_review"})
REVIEW_MATRIX_CHECKS = (
    "full_resolution",
    "panel_topology",
    "composition",
    "identity",
    "facial_hair",
    "anatomy",
    "costume",
    "prop",
    "scene",
    "style",
    "text",
    "sfx",
    "artifact_damage",
    "candidate_binding",
    "preflight_binding",
)
TEXT_REVIEW_CHECKS: tuple[str, ...] = ()
_STABLE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{1,62}[a-z0-9])?\Z")
DEFAULT_THRESHOLDS = {
    "min_grayscale_variance": 20.0,
    "max_edge_density_delta": 0.12,
    "max_rgb_mean_delta": 20.0,
    "max_rgb_std_delta": 15.0,
    "edge_pixel_threshold": 24,
}

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "paths",
        "hashes",
        "expected_size",
        "text_policy",
        "ocr_metadata",
        "thresholds",
        "metrics",
        "checks",
        "evaluation_inputs",
        "provenance",
        "preflight_id",
    }
)
_V4_REPORT_KEYS = _REPORT_KEYS | {
    "page_class",
    "change_mask",
    "text_declaration",
    "redraw_evidence",
    "candidate_stage",
    "text_spec",
    "text_request",
    "text_request_binding",
}
_REVIEW_KEYS = frozenset(
    {
        "schema_version",
        "preflight_id",
        "preflight_status",
        "candidate_hash",
        "generator",
        "reviewer",
        "decision",
        "reviewed_at",
        "notes",
        "review_id",
    }
)
_V4_REVIEW_KEYS = _REVIEW_KEYS | {
    "page_class",
    "candidate_created_at",
    "blind",
    "review_artifacts",
    "inspected_panels",
    "inspected_entities",
    "check_matrix",
    "glyph_review",
}
_METRIC_KEYS = frozenset(
    {
        "candidate_size",
        "original_size",
        "grayscale_variance",
        "original_grayscale_variance",
        "edge_density",
        "original_edge_density",
        "edge_density_delta",
        "rgb_mean",
        "original_rgb_mean",
        "rgb_mean_delta",
        "rgb_std",
        "original_rgb_std",
        "rgb_std_delta",
    }
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a nonempty string")
    result = unicodedata.normalize("NFKC", value).strip()
    if not result:
        raise ValueError(f"{name} must be a nonempty string")
    return result


def _optional_text(value: object, name: str) -> str | None:
    return None if value is None else _text(value, name)


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} keys must be strings")
    return value


def _exact_keys(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise ValueError(
            f"{name} fields mismatch: missing={missing!r}, unknown={unknown!r}"
        )


def _path(value: object, name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{name} must be a file path")
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{name} must be a file path")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{name} must exist and be a file")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_value(value: object, name: str) -> str:
    digest = _text(value, name)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{name} must be SHA-256")
    return digest


def _canonical_bound_mapping(value: object, name: str, hash_field: str) -> dict[str, Any]:
    source = _mapping(value, name)
    digest = _sha256_value(source.get(hash_field), f"{name}.{hash_field}")
    body = {key: source[key] for key in source if key != hash_field}
    if digest != canonical_hash(body):
        raise ValueError(f"{name}.{hash_field} does not match canonical content")
    return dict(source)


def _normalize_text_declaration(value: object, original_hash: str, size: list[int]) -> dict[str, Any]:
    declaration = _canonical_bound_mapping(
        value, "text_declaration", "declaration_hash"
    )
    source_page = _mapping(declaration.get("source_page"), "text_declaration.source_page")
    source_relative_path = normalize_relative_image_path(source_page.get("path"))
    if _sha256_value(
        source_page.get("sha256"), "text_declaration.source_page.sha256"
    ) != original_hash:
        raise ValueError("text declaration source page hash mismatch")
    if [source_page.get("width"), source_page.get("height")] != size:
        raise ValueError("text declaration source page dimensions mismatch")
    if declaration.get("only_declared_blocks") is not True:
        raise ValueError("text declaration must allow only declared blocks")
    if not isinstance(declaration.get("source_has_ordinary_text"), bool):
        raise ValueError("text declaration source_has_ordinary_text must be boolean")
    canvas = _mapping(declaration.get("canvas_size"), "text_declaration.canvas_size")
    if canvas.get("width") != size[0] or canvas.get("height") != size[1]:
        raise ValueError("text declaration canvas does not match source dimensions")
    current_target = _mapping(
        declaration.get("current_target"), "text_declaration.current_target"
    )
    if (
        current_target.get("path") != source_relative_path
        or current_target.get("sha256") != original_hash
    ):
        raise ValueError("text declaration current_target does not match current source")
    inventory = _canonical_bound_mapping(
        declaration.get("source_text_inventory"),
        "text_declaration.source_text_inventory",
        "inventory_sha256",
    )
    if _sha256_value(
        inventory.get("source_page_sha256"),
        "text_declaration.source_text_inventory.source_page_sha256",
    ) != original_hash:
        raise ValueError("text inventory source page hash mismatch")
    regions = inventory.get("regions")
    blocks = declaration.get("blocks")
    if not isinstance(regions, list) or not isinstance(blocks, list):
        raise ValueError("text declaration inventory regions and blocks must be lists")
    region_ids: dict[str, Mapping[str, Any]] = {}
    for index, region in enumerate(regions):
        row = _mapping(region, f"text inventory regions[{index}]")
        region_id = _text(row.get("region_id"), f"text inventory regions[{index}].region_id")
        if region_id in region_ids:
            raise ValueError("text inventory region_id must be unique")
        region_ids[region_id] = row
    seen_blocks: set[str] = set()
    seen_regions: set[str] = set()
    for index, block in enumerate(blocks):
        row = _mapping(block, f"text declaration blocks[{index}]")
        block_id = _text(row.get("block_id"), f"text declaration blocks[{index}].block_id")
        if block_id in seen_blocks:
            raise ValueError("text declaration block_id must be unique")
        seen_blocks.add(block_id)
        region_id = _text(
            row.get("source_region_id"),
            f"text declaration blocks[{index}].source_region_id",
        )
        if region_id in seen_regions or region_id not in region_ids:
            raise ValueError("text declaration must bind each existing source region once")
        seen_regions.add(region_id)
        if list(row.get("bbox", [])) != list(region_ids[region_id].get("bbox", [])):
            raise ValueError("text declaration block geometry must match inventory")
    if declaration["source_has_ordinary_text"] is False and blocks:
        raise ValueError("text-only repair cannot introduce text on a no-text page")
    if declaration["source_has_ordinary_text"] is True and set(region_ids) != seen_regions:
        raise ValueError("text declaration must cover every source text region")
    return declaration


def _validate_task7_binding(
    text_spec: object,
    text_request: object,
    *,
    original_hash: str,
    size: list[int],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(text_spec, Mapping) or not isinstance(text_request, Mapping):
        raise ValueError("complete Task 7 text_spec and text_request are required")
    validated = validate_v4_text_repair_request(text_spec, text_request)
    declaration = _normalize_text_declaration(
        validated.get("declaration"), original_hash, size
    )
    normalized_spec = json.loads(json.dumps(text_spec, ensure_ascii=False))
    binding = {
        "contract_version": "v4",
        "page_id": validated["page_id"],
        "prompt_hash": validated["prompt_hash"],
        "declaration_hash": validated["declaration_hash"],
        "source_page_sha256": original_hash,
        "source_relative_path": declaration["source_page"]["path"],
        "candidate_dimensions": size,
        "text_spec_sha256": canonical_hash(normalized_spec),
    }
    return normalized_spec, validated, binding


def _normalize_change_mask(
    value: object,
    *,
    size: list[int],
    declaration: Mapping[str, Any],
) -> tuple[dict[str, Any], Image.Image]:
    source = _mapping(value, "change_mask")
    _exact_keys(
        source,
        frozenset({"path", "sha256", "mode", "width", "height"}),
        "change_mask",
    )
    path = _path(source["path"], "change_mask.path")
    digest = _sha256_value(source["sha256"], "change_mask.sha256")
    if _sha256(path) != digest:
        raise ValueError("change_mask hash mismatch")
    if source["mode"] != "L":
        raise ValueError("change_mask mode must be L")
    if [source["width"], source["height"]] != size:
        raise ValueError("change_mask declared dimensions mismatch")
    try:
        with Image.open(path) as image:
            image.load()
            if image.mode != "L" or [image.width, image.height] != size:
                raise ValueError("change_mask image mode or dimensions mismatch")
            mask = image.copy()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("change_mask is not a decodable image") from exc
    approved = Image.new("L", tuple(size), 0)
    approved_draw = ImageDraw.Draw(approved)
    for block in declaration["blocks"]:
        bbox = block["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(not isinstance(item, int) or isinstance(item, bool) for item in bbox)
            or not (0 <= bbox[0] < bbox[2] <= size[0])
            or not (0 <= bbox[1] < bbox[3] <= size[1])
        ):
            raise ValueError("text declaration bbox is invalid")
        approved_draw.rectangle((bbox[0], bbox[1], bbox[2] - 1, bbox[3] - 1), fill=255)
    outside_declared = ImageChops.multiply(mask, ImageChops.invert(approved))
    if outside_declared.getbbox() is not None:
        raise ValueError("change_mask extends outside declared text regions")
    return {
        "path": str(path),
        "sha256": digest,
        "mode": "L",
        "width": size[0],
        "height": size[1],
    }, mask


def _outside_mask_check(
    candidate: Path,
    original: Path,
    mask: Image.Image,
) -> dict[str, Any]:
    try:
        with Image.open(candidate) as candidate_image, Image.open(original) as original_image:
            candidate_image.load()
            original_image.load()
            with candidate_image.convert("RGBA") as candidate_rgba, original_image.convert("RGBA") as original_rgba:
                difference_rgba = ImageChops.difference(candidate_rgba, original_rgba)
                channels = difference_rgba.split()
                difference = channels[0]
                for channel in channels[1:]:
                    difference = ImageChops.lighter(difference, channel)
                changed = difference.point(lambda value: 255 if value else 0)
                outside = ImageChops.multiply(changed, ImageChops.invert(mask))
                expanded = mask.filter(ImageFilter.MaxFilter(3))
                boundary = ImageChops.subtract(expanded, mask)
                boundary_changes = ImageChops.multiply(outside, boundary)
                far_outside = ImageChops.multiply(outside, ImageChops.invert(expanded))
                far_count = sum(1 for value in far_outside.getdata() if value)
                boundary_count = sum(1 for value in boundary_changes.getdata() if value)
                boundary_delta = ImageChops.multiply(
                    difference, boundary_changes
                ).getextrema()[1]
                outside_count = far_count + boundary_count
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return _check("blocked", None, {"maximum": 0}, "pixel comparison unavailable")
    return _check(
        "pass" if far_count == 0 and boundary_delta <= 8 else "fail",
        {
            "outside_changed_pixels": outside_count,
            "far_outside_changed_pixels": far_count,
            "one_pixel_boundary_changed_pixels": boundary_count,
            "one_pixel_boundary_max_delta": boundary_delta,
        },
        {
            "maximum_far_outside_changed_pixels": 0,
            "boundary_width_pixels": 1,
            "maximum_boundary_delta": 8,
        },
        "all changes are in-mask or limited to deterministic one-pixel antialiasing"
        if far_count == 0 and boundary_delta <= 8
        else "candidate changed artwork outside the approved text mask",
    )


def _normalize_evidence_artifact(
    value: object,
    name: str,
    *,
    source_hash: str,
    candidate_hash: str | None = None,
    expected_kind: str,
) -> dict[str, Any]:
    source = _mapping(value, name)
    required = {"path", "sha256", "kind", "source_page_sha256"}
    if candidate_hash is not None:
        required.add("candidate_sha256")
    _exact_keys(source, frozenset(required), name)
    path = _path(source["path"], f"{name}.path")
    digest = _sha256_value(source["sha256"], f"{name}.sha256")
    if _sha256(path) != digest:
        raise ValueError(f"{name} hash mismatch")
    if source["kind"] != expected_kind:
        raise ValueError(f"{name}.kind must be {expected_kind}")
    if source["source_page_sha256"] != source_hash:
        raise ValueError(f"{name} source page hash mismatch")
    result = {
        "path": str(path),
        "sha256": digest,
        "kind": expected_kind,
        "source_page_sha256": source_hash,
    }
    if candidate_hash is not None:
        if source["candidate_sha256"] != candidate_hash:
            raise ValueError(f"{name} candidate hash mismatch")
        result["candidate_sha256"] = candidate_hash
    return result


def _normalize_redraw_evidence(
    value: object,
    *,
    source_path: Path,
    source_hash: str,
    candidate_hash: str,
    candidate_stage: str,
) -> dict[str, Any]:
    source = _mapping(value, "redraw_evidence")
    _exact_keys(
        source,
        frozenset(
            {
                "source_page_sha256",
                "candidate_sha256",
                "candidate_stage",
                "source_has_ordinary_text",
                "panel_topology",
                "target_composition",
                "textless_request",
            }
        ),
        "redraw_evidence",
    )
    if source["source_page_sha256"] != source_hash:
        raise ValueError("redraw evidence source hash mismatch")
    if source["candidate_sha256"] != candidate_hash:
        raise ValueError("redraw evidence candidate hash mismatch")
    if source["candidate_stage"] != candidate_stage:
        raise ValueError("redraw evidence candidate stage mismatch")
    if not isinstance(source["source_has_ordinary_text"], bool):
        raise ValueError("redraw evidence source_has_ordinary_text must be boolean")
    topology = _normalize_evidence_artifact(
        source["panel_topology"],
        "redraw_evidence.panel_topology",
        source_hash=source_hash,
        candidate_hash=candidate_hash,
        expected_kind="panel_topology",
    )
    target = _normalize_evidence_artifact(
        source["target_composition"],
        "redraw_evidence.target_composition",
        source_hash=source_hash,
        expected_kind="target_composition",
    )
    if Path(target["path"]).resolve() != source_path or target["sha256"] != source_hash:
        raise ValueError("target_composition must be the exact current source page")
    request = _normalize_evidence_artifact(
        source["textless_request"],
        "redraw_evidence.textless_request",
        source_hash=source_hash,
        expected_kind="textless_redraw_request",
    )
    try:
        request_body = json.loads(Path(request["path"]).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("textless redraw request must be UTF-8 JSON") from exc
    if not isinstance(request_body, Mapping):
        raise ValueError("textless redraw request must be a JSON object")
    if (
        request_body.get("source_page_sha256") != source_hash
        or request_body.get("target_composition_sha256") != source_hash
        or request_body.get("textless") is not True
    ):
        raise ValueError("textless redraw request is not bound to current source target")
    return {
        "source_page_sha256": source_hash,
        "candidate_sha256": candidate_hash,
        "candidate_stage": candidate_stage,
        "source_has_ordinary_text": source["source_has_ordinary_text"],
        "panel_topology": topology,
        "target_composition": target,
        "textless_request": request,
    }


def _expected_size(value: object) -> list[int]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Iterable)
    ):
        raise ValueError("expected_size must contain width and height")
    result = list(value)
    if (
        len(result) != 2
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in result
        )
    ):
        raise ValueError("expected_size must contain two positive integers")
    return result


def _number(value: object, name: str, *, integer: bool = False) -> float | int:
    if integer:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} must be an integer")
        number: float | int = value
    else:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        number = float(value)
    if not math.isfinite(float(number)):
        raise ValueError(f"{name} must be finite")
    return number


def _thresholds(value: object) -> dict[str, float | int]:
    result: dict[str, float | int] = dict(DEFAULT_THRESHOLDS)
    if value is not None:
        source = _mapping(value, "thresholds")
        unknown = sorted(set(source) - set(DEFAULT_THRESHOLDS))
        if unknown:
            raise ValueError(f"unknown threshold field(s): {unknown!r}")
        for key, item in source.items():
            result[key] = _number(
                item,
                f"thresholds.{key}",
                integer=key == "edge_pixel_threshold",
            )
    for key in (
        "min_grayscale_variance",
        "max_edge_density_delta",
        "max_rgb_mean_delta",
        "max_rgb_std_delta",
    ):
        if float(result[key]) < 0:
            raise ValueError(f"thresholds.{key} must be nonnegative")
    edge_threshold = result["edge_pixel_threshold"]
    if not isinstance(edge_threshold, int) or not 0 <= edge_threshold <= 255:
        raise ValueError("thresholds.edge_pixel_threshold must be within 0..255")
    return result


def _normalize_ocr(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    source = _mapping(value, "ocr_metadata")
    required = frozenset({"ordinary_text_count", "unexpected_texts"})
    allowed = required | {"allowlisted_art_text", "detected_art_texts"}
    missing = sorted(required - set(source))
    unknown = sorted(set(source) - allowed)
    if missing or unknown:
        raise ValueError(
            f"ocr_metadata fields mismatch: missing={missing!r}, unknown={unknown!r}"
        )
    count = source["ordinary_text_count"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("ocr_metadata.ordinary_text_count must be a nonnegative integer")
    unexpected = source["unexpected_texts"]
    if not isinstance(unexpected, list):
        raise ValueError("ocr_metadata.unexpected_texts must be a list")
    texts = [
        _text(item, f"ocr_metadata.unexpected_texts[{index}]")
        for index, item in enumerate(unexpected)
    ]
    def normalized_art_texts(field: str) -> list[str]:
        raw = source.get(field, [])
        if not isinstance(raw, list):
            raise ValueError(f"ocr_metadata.{field} must be a list")
        normalized: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(raw):
            text = _text(item, f"ocr_metadata.{field}[{index}]")
            if text not in seen:
                seen.add(text)
                normalized.append(text)
        return normalized

    return {
        "ordinary_text_count": count,
        "unexpected_texts": texts,
        "allowlisted_art_text": normalized_art_texts("allowlisted_art_text"),
        "detected_art_texts": normalized_art_texts("detected_art_texts"),
    }


def _verified_metrics(path: Path, edge_threshold: int) -> dict[str, Any] | None:
    try:
        with Image.open(path) as source:
            source.verify()
        with Image.open(path) as source:
            source.load()
            if source.mode == "RGB":
                return _image_metrics(source, edge_threshold)
            with source.convert("RGB") as rgb:
                rgb.load()
                return _image_metrics(rgb, edge_threshold)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return None


def _round(value: float) -> float:
    return round(float(value), 6)


def _image_metrics(image: Image.Image, edge_threshold: int) -> dict[str, Any]:
    with image.convert("L") as grayscale:
        gray_stat = ImageStat.Stat(grayscale)
        with grayscale.filter(ImageFilter.FIND_EDGES) as edge_image:
            if edge_image.width > 2 and edge_image.height > 2:
                with edge_image.crop(
                    (1, 1, edge_image.width - 1, edge_image.height - 1)
                ) as measured_edges:
                    histogram = measured_edges.histogram()
                    total_pixels = measured_edges.width * measured_edges.height
            else:
                histogram = edge_image.histogram()
                total_pixels = edge_image.width * edge_image.height
    edge_pixels = sum(histogram[edge_threshold + 1 :])
    rgb_stat = ImageStat.Stat(image)
    return {
        "size": [image.width, image.height],
        "grayscale_variance": _round(gray_stat.var[0]),
        "edge_density": _round(edge_pixels / total_pixels),
        "rgb_mean": [_round(item) for item in rgb_stat.mean],
        "rgb_std": [_round(item) for item in rgb_stat.stddev],
    }


def _delta(left: list[float], right: list[float]) -> float:
    return _round(max(abs(a - b) for a, b in zip(left, right)))


def _check(status: str, value: object, threshold: object, reason: str) -> dict[str, Any]:
    if status not in CHECK_STATUSES:
        raise AssertionError("invalid internal check status")
    return {
        "status": status,
        "value": value,
        "threshold": threshold,
        "reason": reason,
    }


def _overall_status(checks: Mapping[str, Mapping[str, Any]]) -> str:
    statuses = {check["status"] for check in checks.values()}
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    return "pass"


def _report_id(body: Mapping[str, Any]) -> str:
    return "preflight-" + canonical_hash(body)


def _compute_preflight_body(
    candidate_path: object,
    original_path: object,
    expected_size: object = None,
    text_policy: str = "textless",
    ocr_metadata: object = None,
    thresholds: object = None,
    page_class: object = None,
    change_mask: object = None,
    text_declaration: object = None,
    redraw_evidence: object = None,
    candidate_stage: object = "final",
    text_spec: object = None,
    text_request: object = None,
) -> dict[str, Any]:
    """Compute a normalized report body without validation recursion."""
    candidate = _path(candidate_path, "candidate_path")
    original = _path(original_path, "original_path")
    explicit_v4 = page_class is not None
    normalized_class = None
    if explicit_v4:
        normalized_class = _text(page_class, "page_class")
        if normalized_class not in PAGE_CLASSES:
            raise ValueError("page_class is invalid")
    policy = _text(text_policy, "text_policy")
    if policy not in TEXT_POLICIES:
        raise ValueError(f"unknown text_policy: {policy!r}")
    ocr = _normalize_ocr(ocr_metadata)
    limits = _thresholds(thresholds)

    edge_threshold = int(limits["edge_pixel_threshold"])
    candidate_metrics = _verified_metrics(candidate, edge_threshold)
    original_metrics = _verified_metrics(original, edge_threshold)
    candidate_ok = candidate_metrics is not None
    original_ok = original_metrics is not None
    both_ok = candidate_ok and original_ok
    if original_metrics is None:
        if expected_size is None:
            if explicit_v4:
                raise ValueError("source dimensions unavailable")
            size = (
                list(candidate_metrics["size"])
                if candidate_metrics is not None
                else [896, 1200]
            )
        else:
            size = _expected_size(expected_size)
    else:
        source_size = original_metrics["size"]
        if expected_size is not None and _expected_size(expected_size) != source_size:
            raise ValueError("expected_size must exactly match source dimensions")
        size = list(source_size)

    metrics = {
        "candidate_size": candidate_metrics["size"] if candidate_metrics else None,
        "original_size": original_metrics["size"] if original_metrics else None,
        "grayscale_variance": candidate_metrics["grayscale_variance"] if candidate_metrics else None,
        "original_grayscale_variance": original_metrics["grayscale_variance"] if original_metrics else None,
        "edge_density": candidate_metrics["edge_density"] if candidate_metrics else None,
        "original_edge_density": original_metrics["edge_density"] if original_metrics else None,
        "edge_density_delta": None,
        "rgb_mean": candidate_metrics["rgb_mean"] if candidate_metrics else None,
        "original_rgb_mean": original_metrics["rgb_mean"] if original_metrics else None,
        "rgb_mean_delta": None,
        "rgb_std": candidate_metrics["rgb_std"] if candidate_metrics else None,
        "original_rgb_std": original_metrics["rgb_std"] if original_metrics else None,
        "rgb_std_delta": None,
    }
    if both_ok and candidate_metrics is not None and original_metrics is not None:
        metrics["edge_density_delta"] = _round(
            abs(candidate_metrics["edge_density"] - original_metrics["edge_density"])
        )
        metrics["rgb_mean_delta"] = _delta(
            candidate_metrics["rgb_mean"], original_metrics["rgb_mean"]
        )
        metrics["rgb_std_delta"] = _delta(
            candidate_metrics["rgb_std"], original_metrics["rgb_std"]
        )

    checks: dict[str, dict[str, Any]] = {}
    checks["decodable"] = _check(
        "pass" if both_ok else "fail",
        {"candidate": candidate_ok, "original": original_ok},
        {"candidate": True, "original": True},
        "candidate and original passed Pillow verify and reopen"
        if both_ok
        else "candidate or original failed Pillow verify/reopen",
    )
    if candidate_metrics is None:
        checks["dimensions"] = _check(
            "blocked", None, size, "candidate dimensions unavailable because decoding failed"
        )
        checks["blank_page"] = _check(
            "blocked",
            None,
            {"minimum": limits["min_grayscale_variance"]},
            "blank-page metric unavailable because decoding failed",
        )
    else:
        dimensions_ok = candidate_metrics["size"] == size
        checks["dimensions"] = _check(
            "pass" if dimensions_ok else "fail",
            candidate_metrics["size"],
            size,
            "candidate dimensions match expected size"
            if dimensions_ok
            else "candidate dimensions do not match expected size",
        )
        variance = candidate_metrics["grayscale_variance"]
        variance_ok = variance >= float(limits["min_grayscale_variance"])
        checks["blank_page"] = _check(
            "pass" if variance_ok else "fail",
            variance,
            {"minimum": limits["min_grayscale_variance"]},
            "grayscale variance is above the blank-page floor"
            if variance_ok
            else "grayscale variance is below the blank-page floor",
        )

    comparison_specs = (
        ("edge_density_delta", "max_edge_density_delta"),
        ("rgb_mean_delta", "max_rgb_mean_delta"),
        ("rgb_std_delta", "max_rgb_std_delta"),
    )
    for check_name, threshold_name in comparison_specs:
        value = metrics[check_name]
        limit = limits[threshold_name]
        if value is None:
            checks[check_name] = _check(
                "blocked",
                None,
                {"maximum": limit},
                f"{check_name} unavailable because candidate/original decoding failed",
            )
        else:
            passed = value <= float(limit)
            checks[check_name] = _check(
                "pass" if passed else "fail",
                value,
                {"maximum": limit},
                f"{check_name} is within threshold"
                if passed
                else f"{check_name} exceeds threshold",
            )

    text_threshold = {
        "ordinary_text_count": 0,
        "unexpected_texts": [],
        "detected_art_texts_subset_of_allowlisted_art_text": True,
    }
    if explicit_v4 and normalized_class == "unchanged":
        checks["text_policy"] = _check(
            "not_applicable", None, "N/A", "unchanged pages preserve source bytes"
        )
    elif policy != "textless":
        checks["text_policy"] = _check(
            "not_applicable",
            None,
            "N/A",
            f"{policy} preserve policy makes the textless OCR gate not applicable",
        )
    elif ocr is None:
        checks["text_policy"] = _check(
            "blocked",
            None,
            text_threshold,
            "textless policy requires OCR metadata",
        )
    else:
        unallowlisted_art_text = [
            item
            for item in ocr["detected_art_texts"]
            if item not in set(ocr["allowlisted_art_text"])
        ]
        text_ok = (
            ocr["ordinary_text_count"] == 0
            and not ocr["unexpected_texts"]
            and not unallowlisted_art_text
        )
        checks["text_policy"] = _check(
            "pass" if text_ok else "fail",
            ocr,
            text_threshold,
            "OCR found no ordinary/unexpected text and all detected art text is allowlisted"
            if text_ok
            else "OCR found ordinary, unexpected, or unallowlisted art text under textless policy",
        )

    candidate_hash = _sha256(candidate)
    original_hash = _sha256(original)
    normalized_mask = None
    normalized_declaration = None
    normalized_redraw = None
    normalized_stage = None
    normalized_text_spec = None
    normalized_text_request = None
    text_request_binding = None
    if explicit_v4:
        normalized_stage = _text(candidate_stage, "candidate_stage")
        if normalized_stage not in {"textless", "final"}:
            raise ValueError("candidate_stage must be textless or final")
        if normalized_class == "unchanged":
            if any(item is not None for item in (change_mask, text_declaration, redraw_evidence, text_spec, text_request)):
                raise ValueError("unchanged candidate cannot carry repair evidence")
            same = candidate_hash == original_hash
            checks["content_preserved"] = _check(
                "pass" if same else "fail",
                {"candidate_sha256": candidate_hash, "source_sha256": original_hash},
                {"equal": True},
                "unchanged candidate bytes equal source bytes"
                if same
                else "unchanged candidate bytes differ from source bytes",
            )
        elif normalized_class == "text_only":
            if redraw_evidence is not None:
                raise ValueError("text_only candidate cannot carry redraw evidence")
            if text_declaration is not None:
                raise ValueError("self-signed text_declaration is forbidden; provide complete Task 7 request")
            (
                normalized_text_spec,
                normalized_text_request,
                text_request_binding,
            ) = _validate_task7_binding(
                text_spec,
                text_request,
                original_hash=original_hash,
                size=size,
            )
            normalized_declaration = normalized_text_request["declaration"]
            normalized_mask, opened_mask = _normalize_change_mask(
                change_mask, size=size, declaration=normalized_declaration
            )
            try:
                checks["outside_mask_preserved"] = _outside_mask_check(
                    candidate, original, opened_mask
                )
            finally:
                opened_mask.close()
            checks["text_contract_bound"] = _check(
                "pass",
                {
                    "declaration_hash": normalized_declaration["declaration_hash"],
                    "inventory_sha256": normalized_declaration["source_text_inventory"]["inventory_sha256"],
                    "mask_sha256": normalized_mask["sha256"],
                },
                {"hash_bound": True, "only_declared_blocks": True},
                "text candidate is bound to declaration, inventory, and exact mask",
            )
        else:
            if change_mask is not None:
                raise ValueError("full_page_redraw cannot use source text-only mask")
            if redraw_evidence is None:
                checks["redraw_evidence"] = _check(
                    "blocked", None, {"complete": True}, "full-page redraw evidence missing"
                )
            else:
                normalized_redraw = _normalize_redraw_evidence(
                    redraw_evidence,
                    source_path=original,
                    source_hash=original_hash,
                    candidate_hash=candidate_hash,
                    candidate_stage=normalized_stage,
                )
                if normalized_stage == "textless" and text_declaration is not None:
                    raise ValueError("textless redraw stage cannot carry a text declaration")
                if normalized_stage == "textless" and (text_spec is not None or text_request is not None):
                    raise ValueError("textless redraw stage cannot carry Task 7 text inputs")
                if normalized_stage == "final" and normalized_redraw["source_has_ordinary_text"]:
                    if text_declaration is not None:
                        raise ValueError("self-signed text_declaration is forbidden; provide complete Task 7 request")
                    (
                        normalized_text_spec,
                        normalized_text_request,
                        text_request_binding,
                    ) = _validate_task7_binding(
                        text_spec,
                        text_request,
                        original_hash=original_hash,
                        size=size,
                    )
                    normalized_declaration = normalized_text_request["declaration"]
                elif text_declaration is not None:
                    raise ValueError("no-text redraw cannot introduce a text declaration")
                elif text_spec is not None or text_request is not None:
                    raise ValueError("no-text redraw cannot carry Task 7 text inputs")
                checks["redraw_evidence"] = _check(
                    "pass", normalized_redraw, {"complete": True}, "redraw evidence supplied"
                )
    evaluation_inputs = {
        "expected_size": size,
        "thresholds": limits,
        "text_policy": policy,
        "ocr_metadata": ocr,
    }
    if explicit_v4:
        evaluation_inputs.update(
            {
                "page_class": normalized_class,
                "change_mask": normalized_mask,
                "text_declaration": normalized_declaration,
                "redraw_evidence": normalized_redraw,
                "candidate_stage": normalized_stage,
                "text_spec": normalized_text_spec,
                "text_request": normalized_text_request,
                "text_request_binding": text_request_binding,
            }
        )
    provenance = {
        "candidate_path": str(candidate),
        "original_path": str(original),
        "candidate_sha256": candidate_hash,
        "original_sha256": original_hash,
    }
    body = {
        "schema_version": SCHEMA_VERSION,
        "status": _overall_status(checks),
        "paths": {"candidate": str(candidate), "original": str(original)},
        "hashes": {"candidate": candidate_hash, "original": original_hash},
        "expected_size": size,
        "text_policy": policy,
        "ocr_metadata": ocr,
        "thresholds": limits,
        "metrics": metrics,
        "checks": checks,
        "evaluation_inputs": evaluation_inputs,
        "provenance": provenance,
    }
    if explicit_v4:
        body.update(
            {
                "page_class": normalized_class,
                "change_mask": normalized_mask,
                "text_declaration": normalized_declaration,
                "redraw_evidence": normalized_redraw,
                "candidate_stage": normalized_stage,
                "text_spec": normalized_text_spec,
                "text_request": normalized_text_request,
                "text_request_binding": text_request_binding,
            }
        )
    return body


def run_candidate_preflight(
    candidate_path: object,
    original_path: object,
    expected_size: object = None,
    text_policy: str = "textless",
    ocr_metadata: object = None,
    thresholds: object = None,
    page_class: object = None,
    change_mask: object = None,
    text_declaration: object = None,
    redraw_evidence: object = None,
    candidate_stage: object = "final",
    text_spec: object = None,
    text_request: object = None,
) -> dict[str, Any]:
    """Build a deterministic, fully bound machine-preflight report."""
    body = _compute_preflight_body(
        candidate_path,
        original_path,
        expected_size=expected_size,
        text_policy=text_policy,
        ocr_metadata=ocr_metadata,
        thresholds=thresholds,
        page_class=page_class,
        change_mask=change_mask,
        text_declaration=text_declaration,
        redraw_evidence=redraw_evidence,
        candidate_stage=candidate_stage,
        text_spec=text_spec,
        text_request=text_request,
    )
    return {**body, "preflight_id": _report_id(body)}


def _validate_check(name: str, value: object) -> None:
    check = _mapping(value, f"checks.{name}")
    _exact_keys(
        check,
        frozenset({"status", "value", "threshold", "reason"}),
        f"checks.{name}",
    )
    if check["status"] not in CHECK_STATUSES:
        raise ValueError(f"checks.{name}.status is invalid")
    _text(check["reason"], f"checks.{name}.reason")


def validate_preflight_report(report: object, *, verify_files: bool = True) -> bool:
    """Deep-check a report and recompute its full-content preflight ID."""
    source = _mapping(report, "preflight report")
    explicit_v4 = "page_class" in source
    _exact_keys(
        source, _V4_REPORT_KEYS if explicit_v4 else _REPORT_KEYS, "preflight report"
    )
    if source["schema_version"] != SCHEMA_VERSION:
        raise ValueError("preflight schema_version is unsupported")
    if source["status"] not in REPORT_STATUSES:
        raise ValueError("preflight status is invalid")
    paths = _mapping(source["paths"], "paths")
    hashes = _mapping(source["hashes"], "hashes")
    _exact_keys(paths, frozenset({"candidate", "original"}), "paths")
    _exact_keys(hashes, frozenset({"candidate", "original"}), "hashes")
    for key in ("candidate", "original"):
        path_text = _text(paths[key], f"paths.{key}")
        if not Path(path_text).is_absolute():
            raise ValueError(f"paths.{key} must be absolute")
        digest = _text(hashes[key], f"hashes.{key}")
        if _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"hashes.{key} must be SHA-256")
    _expected_size(source["expected_size"])
    policy = _text(source["text_policy"], "text_policy")
    if policy not in TEXT_POLICIES:
        raise ValueError("text_policy is invalid")
    _normalize_ocr(source["ocr_metadata"])
    limits = _thresholds(source["thresholds"])
    if dict(source["thresholds"]) != limits:
        raise ValueError("thresholds are not canonical")
    metrics = _mapping(source["metrics"], "metrics")
    _exact_keys(metrics, _METRIC_KEYS, "metrics")
    checks = _mapping(source["checks"], "checks")
    class_checks: tuple[str, ...] = ()
    if explicit_v4:
        page_class = _text(source["page_class"], "page_class")
        if page_class not in PAGE_CLASSES:
            raise ValueError("page_class is invalid")
        class_checks = {
            "unchanged": ("content_preserved",),
            "text_only": ("outside_mask_preserved", "text_contract_bound"),
            "full_page_redraw": ("redraw_evidence",),
        }[page_class]
    if tuple(checks) != REQUIRED_CHECKS + class_checks:
        raise ValueError("required checks are missing or out of order")
    for name in REQUIRED_CHECKS + class_checks:
        _validate_check(name, checks[name])
    if source["status"] != _overall_status(checks):
        raise ValueError("preflight status does not match check statuses")

    evaluation_inputs = _mapping(source["evaluation_inputs"], "evaluation_inputs")
    expected_evaluation_keys = frozenset(
        {"expected_size", "thresholds", "text_policy", "ocr_metadata"}
    )
    if explicit_v4:
        expected_evaluation_keys |= {
            "page_class",
            "change_mask",
            "text_declaration",
            "redraw_evidence",
            "candidate_stage",
            "text_spec",
            "text_request",
            "text_request_binding",
        }
    _exact_keys(evaluation_inputs, expected_evaluation_keys, "evaluation_inputs")
    normalized_evaluation = {
        "expected_size": _expected_size(evaluation_inputs["expected_size"]),
        "thresholds": _thresholds(evaluation_inputs["thresholds"]),
        "text_policy": _text(evaluation_inputs["text_policy"], "evaluation_inputs.text_policy"),
        "ocr_metadata": _normalize_ocr(evaluation_inputs["ocr_metadata"]),
    }
    if explicit_v4:
        normalized_evaluation.update(
            {
                "page_class": source["page_class"],
                "change_mask": source["change_mask"],
                "text_declaration": source["text_declaration"],
                "redraw_evidence": source["redraw_evidence"],
                "candidate_stage": source["candidate_stage"],
                "text_spec": source["text_spec"],
                "text_request": source["text_request"],
                "text_request_binding": source["text_request_binding"],
            }
        )
    if normalized_evaluation["text_policy"] not in TEXT_POLICIES:
        raise ValueError("evaluation_inputs.text_policy is invalid")
    if dict(evaluation_inputs) != normalized_evaluation:
        raise ValueError("evaluation_inputs are not normalized")
    if (
        normalized_evaluation["expected_size"] != source["expected_size"]
        or normalized_evaluation["thresholds"] != source["thresholds"]
        or normalized_evaluation["text_policy"] != source["text_policy"]
        or normalized_evaluation["ocr_metadata"] != source["ocr_metadata"]
    ):
        raise ValueError("evaluation_inputs do not match report inputs")

    provenance = _mapping(source["provenance"], "provenance")
    _exact_keys(
        provenance,
        frozenset(
            {
                "candidate_path",
                "original_path",
                "candidate_sha256",
                "original_sha256",
            }
        ),
        "provenance",
    )
    expected_provenance = {
        "candidate_path": paths["candidate"],
        "original_path": paths["original"],
        "candidate_sha256": hashes["candidate"],
        "original_sha256": hashes["original"],
    }
    if dict(provenance) != expected_provenance:
        raise ValueError("provenance does not match report paths/hashes")

    identifier = _text(source["preflight_id"], "preflight_id")
    body = {key: source[key] for key in source if key != "preflight_id"}
    if identifier != _report_id(body):
        raise ValueError("preflight_id does not match full report content")
    if verify_files:
        try:
            recomputed = _compute_preflight_body(
                paths["candidate"],
                paths["original"],
                expected_size=normalized_evaluation["expected_size"],
                text_policy=normalized_evaluation["text_policy"],
                ocr_metadata=normalized_evaluation["ocr_metadata"],
                thresholds=normalized_evaluation["thresholds"],
                page_class=normalized_evaluation.get("page_class"),
                change_mask=normalized_evaluation.get("change_mask"),
                text_declaration=None,
                redraw_evidence=normalized_evaluation.get("redraw_evidence"),
                candidate_stage=normalized_evaluation.get("candidate_stage", "final"),
                text_spec=normalized_evaluation.get("text_spec"),
                text_request=normalized_evaluation.get("text_request"),
            )
        except ValueError as exc:
            raise ValueError("preflight source files cannot be strongly validated") from exc
        if dict(body) != recomputed:
            raise ValueError("preflight report does not match recomputed files and checks")
    return True


def candidate_is_machine_eligible(report: object) -> bool:
    validate_preflight_report(report)
    return bool(report["status"] == "pass")


def _timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("reviewed_at must be an ISO timestamp") from exc
    else:
        raise ValueError("reviewed_at must be a timezone-aware timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reviewed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _review_id(body: Mapping[str, Any]) -> str:
    return "review-" + canonical_hash(body)


def _stable_id(value: object, name: str) -> str:
    if not isinstance(value, str) or _STABLE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 3-64 lowercase ASCII stable id")
    return value


def _string_list(value: object, name: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{name} must be {'a nonempty ' if nonempty else 'a '}list")
    result: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, f"{name}[{index}]")
        if text in result:
            raise ValueError(f"{name} values must be unique")
        result.append(text)
    return result


def _review_artifacts(
    value: object,
    *,
    report: Mapping[str, Any],
    created_at: str,
    reviewed_at: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("review artifact evidence is required")
    result: list[dict[str, Any]] = []
    seen_kinds: set[str] = set()
    seen_paths: set[Path] = set()
    source_size = list(report["expected_size"])
    for index, artifact in enumerate(value):
        row = _mapping(artifact, f"review artifact[{index}]")
        _exact_keys(
            row,
            frozenset(
                {
                    "path",
                    "sha256",
                    "kind",
                    "candidate_sha256",
                    "source_sha256",
                    "preflight_id",
                    "created_at",
                }
            ),
            f"review artifact[{index}]",
        )
        path = _path(row["path"], f"review artifact[{index}].path")
        if path in seen_paths:
            raise ValueError("review artifact paths must be unique")
        seen_paths.add(path)
        digest = _sha256_value(row["sha256"], f"review artifact[{index}].sha256")
        if _sha256(path) != digest:
            raise ValueError("review artifact hash mismatch")
        if row["candidate_sha256"] != report["hashes"]["candidate"]:
            raise ValueError("review artifact candidate hash mismatch")
        if row["preflight_id"] != report["preflight_id"]:
            raise ValueError("review artifact preflight binding mismatch")
        artifact_time = _timestamp(row["created_at"])
        if not created_at < artifact_time <= reviewed_at:
            raise ValueError("review artifact time must follow candidate creation and not exceed review")
        kind = _text(row["kind"], f"review artifact[{index}].kind")
        if kind in seen_kinds:
            raise ValueError("review artifact kinds must be unique")
        seen_kinds.add(kind)
        if row["source_sha256"] != report["hashes"]["original"]:
            raise ValueError("review artifact source hash mismatch")
        metrics = _verified_metrics(path, int(DEFAULT_THRESHOLDS["edge_pixel_threshold"]))
        if metrics is None:
            raise ValueError("review artifact must be a decodable image")
        if kind == "full_resolution_original":
            if digest != report["hashes"]["original"] or metrics["size"] != source_size:
                raise ValueError("original review artifact must be exact full-size source image")
        elif kind == "full_resolution_candidate":
            if digest != report["hashes"]["candidate"] or metrics["size"] != source_size:
                raise ValueError("candidate review artifact must be exact current full-size candidate")
        elif kind == "full_resolution_comparison":
            expected_comparison_size = [source_size[0] * 2, source_size[1]]
            if metrics["size"] != expected_comparison_size:
                raise ValueError("comparison review artifact must be exact two-up full-size dimensions")
            try:
                with (
                    Image.open(path) as comparison_source,
                    Image.open(report["paths"]["original"]) as original_source,
                    Image.open(report["paths"]["candidate"]) as candidate_source,
                ):
                    comparison_source.load()
                    original_source.load()
                    candidate_source.load()
                    with (
                        comparison_source.convert("RGBA") as comparison_rgba,
                        original_source.convert("RGBA") as original_rgba,
                        candidate_source.convert("RGBA") as candidate_rgba,
                    ):
                        with comparison_rgba.crop(
                            (0, 0, source_size[0], source_size[1])
                        ) as left_half, comparison_rgba.crop(
                            (source_size[0], 0, source_size[0] * 2, source_size[1])
                        ) as right_half:
                            left_matches = (
                                all(
                                    band.getbbox() is None
                                    for band in ImageChops.difference(
                                        left_half, original_rgba
                                    ).split()
                                )
                            )
                            right_matches = (
                                all(
                                    band.getbbox() is None
                                    for band in ImageChops.difference(
                                        right_half, candidate_rgba
                                    ).split()
                                )
                            )
            except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
                raise ValueError("comparison review artifact cannot be canonically decoded") from exc
            if not left_matches or not right_matches:
                raise ValueError(
                    "comparison review artifact must be exact source-left candidate-right pixels"
                )
        else:
            raise ValueError("review artifact kind is invalid")
        result.append(
            {
                "path": str(path),
                "sha256": digest,
                "kind": kind,
                "candidate_sha256": row["candidate_sha256"],
                "source_sha256": row["source_sha256"],
                "preflight_id": row["preflight_id"],
                "created_at": artifact_time,
            }
        )
    required = {"full_resolution_original", "full_resolution_candidate", "full_resolution_comparison"}
    if not required.issubset(seen_kinds):
        raise ValueError(f"review artifact missing required full-resolution kinds: {sorted(required - seen_kinds)!r}")
    return result


def _review_matrix(value: object, *, text_bearing: bool, accepted: bool) -> dict[str, bool]:
    source = _mapping(value, "check_matrix")
    expected = frozenset(REVIEW_MATRIX_CHECKS + (TEXT_REVIEW_CHECKS if text_bearing else ()))
    _exact_keys(source, expected, "check_matrix")
    result: dict[str, bool] = {}
    for name in sorted(expected):
        status = source[name]
        if not isinstance(status, bool):
            raise ValueError("check_matrix values must be booleans")
        if accepted and status is not True:
            raise ValueError("accepted review cannot contain pending or failed checks")
        result[name] = status
    return result


def _glyph_review(
    value: object,
    *,
    report: Mapping[str, Any],
    generator_id: str,
    declaration: Mapping[str, Any],
    created_at: str,
    reviewed_at: str,
) -> dict[str, Any]:
    source = _mapping(value, "glyph review")
    _exact_keys(
        source,
        frozenset(
            {
                "artifact",
                "reviewer_id",
                "result",
                "rendered_blocks",
                "regression_vocabulary",
            }
        ),
        "glyph review",
    )
    reviewer_id = _stable_id(source["reviewer_id"], "glyph review reviewer_id")
    if reviewer_id == generator_id:
        raise ValueError("glyph review reviewer must be independent from generator")
    artifact = _mapping(source["artifact"], "glyph review artifact")
    _exact_keys(
        artifact,
        frozenset(
            {
                "path",
                "sha256",
                "kind",
                "source_sha256",
                "candidate_sha256",
                "preflight_id",
                "created_at",
            }
        ),
        "glyph review artifact",
    )
    artifact_path = _path(artifact["path"], "glyph review artifact.path")
    artifact_hash = _sha256_value(artifact["sha256"], "glyph review artifact.sha256")
    if _sha256(artifact_path) != artifact_hash or artifact["kind"] != "full_resolution_glyph":
        raise ValueError("glyph review artifact must be a hash-bound full-resolution image")
    if (
        artifact["source_sha256"] != report["hashes"]["original"]
        or artifact["candidate_sha256"] != report["hashes"]["candidate"]
        or artifact["preflight_id"] != report["preflight_id"]
    ):
        raise ValueError("glyph review artifact binding mismatch")
    artifact_time = _timestamp(artifact["created_at"])
    if not created_at < artifact_time <= reviewed_at:
        raise ValueError("glyph review artifact time is invalid")
    artifact_metrics = _verified_metrics(
        artifact_path, int(DEFAULT_THRESHOLDS["edge_pixel_threshold"])
    )
    if artifact_metrics is None or (
        artifact_metrics["size"][0] < report["expected_size"][0]
        or artifact_metrics["size"][1] < report["expected_size"][1]
    ):
        raise ValueError("glyph review artifact is not a decodable full-resolution image")
    normalized_artifact = {
        "path": str(artifact_path),
        "sha256": artifact_hash,
        "kind": "full_resolution_glyph",
        "source_sha256": artifact["source_sha256"],
        "candidate_sha256": artifact["candidate_sha256"],
        "preflight_id": artifact["preflight_id"],
        "created_at": artifact_time,
    }
    if source["result"] != "passed":
        raise ValueError("glyph review result must be passed")
    rendered = source["rendered_blocks"]
    if not isinstance(rendered, list):
        raise ValueError("glyph review rendered_blocks must be a list")
    expected_ids = [block["block_id"] for block in declaration["blocks"]]
    actual_ids: list[str] = []
    normalized_blocks: list[dict[str, Any]] = []
    for index, item in enumerate(rendered):
        row = _mapping(item, f"glyph review rendered_blocks[{index}]")
        _exact_keys(row, frozenset({"block_id", "inspected"}), "glyph review rendered block")
        block_id = _text(row["block_id"], "glyph review block_id")
        if row["inspected"] is not True:
            raise ValueError("every glyph review block must be visually inspected")
        if block_id in actual_ids:
            raise ValueError("glyph review block IDs must be unique")
        actual_ids.append(block_id)
        normalized_blocks.append({"block_id": block_id, "inspected": True})
    if actual_ids != expected_ids:
        raise ValueError("glyph review must exactly cover every rendered Chinese block")
    vocabulary = source["regression_vocabulary"]
    if not isinstance(vocabulary, list):
        raise ValueError("glyph review regression_vocabulary must be a list")
    normalized_vocabulary: list[dict[str, Any]] = []
    seen_chars: set[str] = set()
    for index, item in enumerate(vocabulary):
        row = _mapping(item, f"glyph review regression_vocabulary[{index}]")
        _exact_keys(row, frozenset({"character", "shape_inspected"}), "glyph review vocabulary")
        character = _text(row["character"], "glyph review vocabulary character")
        if len(character) != 1 or row["shape_inspected"] is not True or character in seen_chars:
            raise ValueError("glyph review vocabulary must contain unique shape-inspected characters")
        seen_chars.add(character)
        normalized_vocabulary.append({"character": character, "shape_inspected": True})
    if not {"\u5f3a", "\u9047"}.issubset(seen_chars):
        raise ValueError("glyph review regression vocabulary must include shape checks for 强 and 遇")
    return {
        "artifact": normalized_artifact,
        "reviewer_id": reviewer_id,
        "result": "passed",
        "rendered_blocks": normalized_blocks,
        "regression_vocabulary": normalized_vocabulary,
    }


def record_independent_review(
    preflight_report: object,
    generator: object,
    reviewer: object,
    decision: object,
    reviewed_at: datetime | str,
    notes: object = None,
    *,
    candidate_created_at: datetime | str | None = None,
    review_artifacts: object = None,
    blind: object = None,
    inspected_panels: object = None,
    inspected_entities: object = None,
    check_matrix: object = None,
    glyph_review: object = None,
) -> dict[str, Any]:
    """Create a full-content-bound independent candidate review."""
    validate_preflight_report(preflight_report)
    strict_v4 = "page_class" in preflight_report or any(
        item is not None
        for item in (
            candidate_created_at,
            review_artifacts,
            blind,
            inspected_panels,
            inspected_entities,
            check_matrix,
            glyph_review,
        )
    )
    author = _stable_id(generator, "generator") if strict_v4 else _text(generator, "generator")
    judge = _stable_id(reviewer, "reviewer") if strict_v4 else _text(reviewer, "reviewer")
    if author.casefold() == judge.casefold():
        raise ValueError("independent review requires generator and reviewer to differ")
    outcome = _text(decision, "decision")
    allowed_decisions = REVIEW_DECISIONS if strict_v4 else {"accepted", "rejected"}
    if outcome not in allowed_decisions:
        raise ValueError("decision must be accepted, rejected, or needs_review")
    if outcome == "accepted" and preflight_report["status"] != "pass":
        raise ValueError("only a pass preflight can be accepted")
    reviewed_timestamp = _timestamp(reviewed_at)
    body = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "preflight_id": preflight_report["preflight_id"],
        "preflight_status": preflight_report["status"],
        "candidate_hash": preflight_report["hashes"]["candidate"],
        "generator": author,
        "reviewer": judge,
        "decision": outcome,
        "reviewed_at": reviewed_timestamp,
        "notes": _optional_text(notes, "notes"),
    }
    if strict_v4:
        if candidate_created_at is None:
            raise ValueError("candidate_created_at is required")
        created_timestamp = _timestamp(candidate_created_at)
        if created_timestamp >= reviewed_timestamp:
            raise ValueError("candidate must be created before review")
        if blind is not True:
            raise ValueError("independent review must be blind")
        if not isinstance(review_artifacts, list) or not review_artifacts:
            raise ValueError("review artifact evidence is required")
        page_class = preflight_report["page_class"]
        text_bearing = page_class == "text_only" or (
            page_class == "full_page_redraw"
            and preflight_report["candidate_stage"] == "final"
            and preflight_report["text_declaration"] is not None
        )
        if text_bearing and glyph_review is None:
            raise ValueError("glyph review is required for text-bearing candidate")
        normalized_artifacts = _review_artifacts(
            review_artifacts,
            report=preflight_report,
            created_at=created_timestamp,
            reviewed_at=reviewed_timestamp,
        )
        normalized_panels = _string_list(
            inspected_panels, "inspected_panels", nonempty=True
        )
        normalized_entities = _string_list(
            inspected_entities, "inspected_entities", nonempty=False
        )
        normalized_matrix = _review_matrix(
            check_matrix, text_bearing=text_bearing, accepted=outcome == "accepted"
        )
        normalized_glyph = None
        if text_bearing:
            normalized_glyph = _glyph_review(
                glyph_review,
                report=preflight_report,
                generator_id=author,
                declaration=preflight_report["text_declaration"],
                created_at=created_timestamp,
                reviewed_at=reviewed_timestamp,
            )
        elif glyph_review is not None:
            raise ValueError("textless candidate must not carry glyph review")
        body.update(
            {
                "page_class": page_class,
                "candidate_created_at": created_timestamp,
                "blind": True,
                "review_artifacts": normalized_artifacts,
                "inspected_panels": normalized_panels,
                "inspected_entities": normalized_entities,
                "check_matrix": normalized_matrix,
                "glyph_review": normalized_glyph,
            }
        )
    return {**body, "review_id": _review_id(body)}


def validate_review(review: object, preflight_report: object = None) -> bool:
    """Deep-check a review, its ID, and optionally its bound preflight."""
    source = _mapping(review, "review")
    strict_v4 = "page_class" in source
    _exact_keys(source, _V4_REVIEW_KEYS if strict_v4 else _REVIEW_KEYS, "review")
    if source["schema_version"] != REVIEW_SCHEMA_VERSION:
        raise ValueError("review schema_version is unsupported")
    preflight_id = _text(source["preflight_id"], "preflight_id")
    if re.fullmatch(r"preflight-[0-9a-f]{64}", preflight_id) is None:
        raise ValueError("review preflight_id is invalid")
    if source["preflight_status"] not in REPORT_STATUSES:
        raise ValueError("review preflight_status is invalid")
    candidate_hash = _text(source["candidate_hash"], "candidate_hash")
    if _SHA256_RE.fullmatch(candidate_hash) is None:
        raise ValueError("review candidate_hash is invalid")
    generator = _stable_id(source["generator"], "generator") if strict_v4 else _text(source["generator"], "generator")
    reviewer = _stable_id(source["reviewer"], "reviewer") if strict_v4 else _text(source["reviewer"], "reviewer")
    if generator.casefold() == reviewer.casefold():
        raise ValueError("review is not independent")
    decision = _text(source["decision"], "decision")
    if decision not in (REVIEW_DECISIONS if strict_v4 else {"accepted", "rejected"}):
        raise ValueError("review decision is invalid")
    if decision == "accepted" and source["preflight_status"] != "pass":
        raise ValueError("non-pass preflight cannot be accepted")
    reviewed_at = _timestamp(source["reviewed_at"])
    if reviewed_at != source["reviewed_at"]:
        raise ValueError("reviewed_at must be canonical UTC")
    _optional_text(source["notes"], "notes")
    if strict_v4:
        if preflight_report is None:
            raise ValueError("strict review validation requires current preflight report")
        if source["blind"] is not True:
            raise ValueError("strict review must be blind")
        created_at = _timestamp(source["candidate_created_at"])
        if created_at != source["candidate_created_at"] or created_at >= reviewed_at:
            raise ValueError("candidate creation time must be canonical and precede review")
        if source["page_class"] not in PAGE_CLASSES:
            raise ValueError("review page_class is invalid")
    if preflight_report is not None:
        validate_preflight_report(preflight_report)
        if preflight_id != preflight_report["preflight_id"]:
            raise ValueError("review is bound to a different preflight")
        if source["preflight_status"] != preflight_report["status"]:
            raise ValueError("review preflight status mismatch")
        if candidate_hash != preflight_report["hashes"]["candidate"]:
            raise ValueError("review candidate hash mismatch")
        if strict_v4:
            if source["page_class"] != preflight_report["page_class"]:
                raise ValueError("review page_class mismatch")
            text_bearing = source["page_class"] == "text_only" or (
                source["page_class"] == "full_page_redraw"
                and preflight_report["candidate_stage"] == "final"
                and preflight_report["text_declaration"] is not None
            )
            normalized_artifacts = _review_artifacts(
                source["review_artifacts"],
                report=preflight_report,
                created_at=created_at,
                reviewed_at=reviewed_at,
            )
            if normalized_artifacts != source["review_artifacts"]:
                raise ValueError("review artifacts are not canonical")
            if _string_list(source["inspected_panels"], "inspected_panels", nonempty=True) != source["inspected_panels"]:
                raise ValueError("inspected_panels are not canonical")
            if _string_list(source["inspected_entities"], "inspected_entities", nonempty=False) != source["inspected_entities"]:
                raise ValueError("inspected_entities are not canonical")
            matrix = _review_matrix(
                source["check_matrix"],
                text_bearing=text_bearing,
                accepted=decision == "accepted",
            )
            if matrix != source["check_matrix"]:
                raise ValueError("check_matrix is not canonical")
            if text_bearing:
                normalized_glyph = _glyph_review(
                    source["glyph_review"],
                    report=preflight_report,
                    generator_id=generator,
                    declaration=preflight_report["text_declaration"],
                    created_at=created_at,
                    reviewed_at=reviewed_at,
                )
                if normalized_glyph != source["glyph_review"]:
                    raise ValueError("glyph review is not canonical")
            elif source["glyph_review"] is not None:
                raise ValueError("textless review cannot carry glyph review")
    body = {key: source[key] for key in source if key != "review_id"}
    if source["review_id"] != _review_id(body):
        raise ValueError("review_id does not match full review content")
    return True


def candidate_is_finally_eligible(preflight_report: object, review: object) -> bool:
    validate_preflight_report(preflight_report)
    validate_review(review, preflight_report)
    return bool(
        preflight_report["status"] == "pass" and review["decision"] == "accepted"
    )


def validate_candidate_batch(
    inputs: Iterable[object],
    mappings: Iterable[Mapping[str, Any]],
    preflight_reports: object,
    *,
    candidate_root: Path,
    actual_outputs: Iterable[str] | None = None,
    reviews: object = None,
) -> bool:
    """Validate strict source/output bijection and ordered candidate reports."""
    resolved_candidate_root = Path(candidate_root).expanduser().resolve()
    if not resolved_candidate_root.is_dir():
        raise ValueError(f"candidate root is not a directory: {resolved_candidate_root}")
    input_rows = list(inputs)
    mapping_rows = list(mappings)
    validate_bijection(input_rows, mapping_rows, actual_outputs=actual_outputs)
    if not isinstance(preflight_reports, list):
        raise ValueError("preflight_reports must be a list")
    if len(preflight_reports) != len(input_rows):
        raise ValueError("preflight report count must match input count")
    seen_ids: set[str] = set()
    for index, (mapping, report) in enumerate(zip(mapping_rows, preflight_reports)):
        validate_preflight_report(report)
        if not candidate_is_machine_eligible(report):
            raise ValueError(f"preflight report at index {index} is not machine eligible")
        if report["preflight_id"] in seen_ids:
            raise ValueError("duplicate preflight_id in candidate batch")
        seen_ids.add(report["preflight_id"])
        output_name = normalize_relative_image_path(mapping["output_name"])
        candidate_path = Path(report["paths"]["candidate"]).expanduser().resolve()
        try:
            candidate_relative = candidate_path.relative_to(resolved_candidate_root)
        except ValueError as exc:
            raise ValueError(
                f"candidate report path is outside candidate root: {candidate_path}"
            ) from exc
        candidate_name = normalize_relative_image_path(candidate_relative.as_posix())
        if candidate_name != output_name:
            raise ValueError(
                f"candidate report order mismatch at index {index}: "
                f"expected {output_name!r}, got {candidate_name!r}"
            )
    if reviews is not None:
        if not isinstance(reviews, list) or len(reviews) != len(preflight_reports):
            raise ValueError("review count must match preflight report count")
        for index, (report, review) in enumerate(zip(preflight_reports, reviews)):
            if not candidate_is_finally_eligible(report, review):
                raise ValueError(f"candidate at index {index} is not finally eligible")
    return True
