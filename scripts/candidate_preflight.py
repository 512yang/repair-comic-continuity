"""Deterministic machine preflight and independent review for comic candidates."""

from __future__ import annotations

import hashlib
import math
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat, UnidentifiedImageError

from pipeline_contracts import (
    canonical_hash,
    normalize_relative_image_path,
    validate_bijection,
)


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
    {"textless", "preserve_art_text", "preserve_text", "preserve_source_text"}
)
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
    expected_size: object = (896, 1200),
    text_policy: str = "textless",
    ocr_metadata: object = None,
    thresholds: object = None,
) -> dict[str, Any]:
    """Compute a normalized report body without validation recursion."""
    candidate = _path(candidate_path, "candidate_path")
    original = _path(original_path, "original_path")
    size = _expected_size(expected_size)
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
    if policy != "textless":
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
    evaluation_inputs = {
        "expected_size": size,
        "thresholds": limits,
        "text_policy": policy,
        "ocr_metadata": ocr,
    }
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
    return body


def run_candidate_preflight(
    candidate_path: object,
    original_path: object,
    expected_size: object = (896, 1200),
    text_policy: str = "textless",
    ocr_metadata: object = None,
    thresholds: object = None,
) -> dict[str, Any]:
    """Build a deterministic, fully bound machine-preflight report."""
    body = _compute_preflight_body(
        candidate_path,
        original_path,
        expected_size=expected_size,
        text_policy=text_policy,
        ocr_metadata=ocr_metadata,
        thresholds=thresholds,
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
    _exact_keys(source, _REPORT_KEYS, "preflight report")
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
    if tuple(checks) != REQUIRED_CHECKS:
        raise ValueError("required checks are missing or out of order")
    for name in REQUIRED_CHECKS:
        _validate_check(name, checks[name])
    if source["status"] != _overall_status(checks):
        raise ValueError("preflight status does not match check statuses")

    evaluation_inputs = _mapping(source["evaluation_inputs"], "evaluation_inputs")
    _exact_keys(
        evaluation_inputs,
        frozenset({"expected_size", "thresholds", "text_policy", "ocr_metadata"}),
        "evaluation_inputs",
    )
    normalized_evaluation = {
        "expected_size": _expected_size(evaluation_inputs["expected_size"]),
        "thresholds": _thresholds(evaluation_inputs["thresholds"]),
        "text_policy": _text(evaluation_inputs["text_policy"], "evaluation_inputs.text_policy"),
        "ocr_metadata": _normalize_ocr(evaluation_inputs["ocr_metadata"]),
    }
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


def record_independent_review(
    preflight_report: object,
    generator: object,
    reviewer: object,
    decision: object,
    reviewed_at: datetime | str,
    notes: object = None,
) -> dict[str, Any]:
    """Create a full-content-bound independent candidate review."""
    validate_preflight_report(preflight_report)
    author = _text(generator, "generator")
    judge = _text(reviewer, "reviewer")
    if author.casefold() == judge.casefold():
        raise ValueError("independent review requires generator and reviewer to differ")
    outcome = _text(decision, "decision")
    if outcome not in {"accepted", "rejected"}:
        raise ValueError("decision must be accepted or rejected")
    if outcome == "accepted" and preflight_report["status"] != "pass":
        raise ValueError("only a pass preflight can be accepted")
    body = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "preflight_id": preflight_report["preflight_id"],
        "preflight_status": preflight_report["status"],
        "candidate_hash": preflight_report["hashes"]["candidate"],
        "generator": author,
        "reviewer": judge,
        "decision": outcome,
        "reviewed_at": _timestamp(reviewed_at),
        "notes": _optional_text(notes, "notes"),
    }
    return {**body, "review_id": _review_id(body)}


def validate_review(review: object, preflight_report: object = None) -> bool:
    """Deep-check a review, its ID, and optionally its bound preflight."""
    source = _mapping(review, "review")
    _exact_keys(source, _REVIEW_KEYS, "review")
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
    generator = _text(source["generator"], "generator")
    reviewer = _text(source["reviewer"], "reviewer")
    if generator.casefold() == reviewer.casefold():
        raise ValueError("review is not independent")
    decision = _text(source["decision"], "decision")
    if decision not in {"accepted", "rejected"}:
        raise ValueError("review decision is invalid")
    if decision == "accepted" and source["preflight_status"] != "pass":
        raise ValueError("non-pass preflight cannot be accepted")
    reviewed_at = _timestamp(source["reviewed_at"])
    if reviewed_at != source["reviewed_at"]:
        raise ValueError("reviewed_at must be canonical UTC")
    _optional_text(source["notes"], "notes")
    body = {key: source[key] for key in source if key != "review_id"}
    if source["review_id"] != _review_id(body):
        raise ValueError("review_id does not match full review content")
    if preflight_report is not None:
        validate_preflight_report(preflight_report)
        if preflight_id != preflight_report["preflight_id"]:
            raise ValueError("review is bound to a different preflight")
        if source["preflight_status"] != preflight_report["status"]:
            raise ValueError("review preflight status mismatch")
        if candidate_hash != preflight_report["hashes"]["candidate"]:
            raise ValueError("review candidate hash mismatch")
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
