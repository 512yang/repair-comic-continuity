"""Convert measured comic text styling into the hash-bound V5 style contract."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Sequence

from pipeline_contracts import canonical_hash


class StyleEvidenceBlocked(ValueError):
    """Raised when the original style cannot be reproduced with reviewed evidence."""


def _box(value: Sequence[int], name: str) -> list[int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise ValueError(f"{name} must contain four integers")
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 and 0 <= y1 < y2):
        raise ValueError(f"{name} must be ordered and nonnegative")
    return list(value)


def _rgba(value: Sequence[int], name: str) -> list[int]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= 255 for item in value)
    ):
        raise ValueError(f"{name} must be RGBA integers")
    return list(value)


def style_lock_from_measurements(
    style,
    font_match,
    *,
    bbox: Sequence[int],
    line_boxes: Sequence[Sequence[int]],
    alignment: str = "left",
    rotation_deg: float = 0.0,
    reviewed_stroke_rgba: Sequence[int] | None = None,
) -> dict:
    """Build a strict style lock; uncertainty blocks rather than selecting a guess."""
    style_confidence = float(getattr(style, "confidence", 0.0))
    font_confidence = float(getattr(font_match, "confidence", 0.0))
    if (
        not math.isfinite(style_confidence)
        or not math.isfinite(font_confidence)
        or style_confidence < 0.95
        or font_confidence < 0.95
        or bool(getattr(font_match, "fallback_used", True))
    ):
        raise StyleEvidenceBlocked(
            "style/font confidence below 0.95 or fallback used; evidence_blocked"
        )
    block_bbox = _box(bbox, "bbox")
    normalized_lines = [_box(value, f"line_boxes[{index}]") for index, value in enumerate(line_boxes)]
    if not normalized_lines:
        raise StyleEvidenceBlocked("original line geometry is missing; evidence_blocked")
    if any(
        line[0] < block_bbox[0]
        or line[1] < block_bbox[1]
        or line[2] > block_bbox[2]
        or line[3] > block_bbox[3]
        for line in normalized_lines
    ):
        raise StyleEvidenceBlocked("original line geometry escapes its text block")
    font_path = Path(getattr(font_match, "path", ""))
    if not font_path.is_file():
        raise StyleEvidenceBlocked("matched font asset is missing")
    orientation = getattr(style, "orientation", "")
    if orientation not in {"horizontal", "vertical"}:
        raise StyleEvidenceBlocked("source text orientation is unknown")
    if alignment not in {"left", "center", "right", "justify"}:
        raise ValueError("alignment is unsupported")
    font_size = float(getattr(style, "font_size", 0.0))
    if not math.isfinite(font_size) or font_size <= 0:
        raise StyleEvidenceBlocked("source font size is unknown")
    stroke_width = float(getattr(style, "stroke_width", 0.0))
    if not math.isfinite(stroke_width) or stroke_width < 0:
        raise StyleEvidenceBlocked("source stroke width is unknown")
    if stroke_width > 0 and reviewed_stroke_rgba is None:
        raise StyleEvidenceBlocked("source stroke color requires full-resolution review")
    stroke_rgba = (
        _rgba(reviewed_stroke_rgba, "reviewed_stroke_rgba")
        if reviewed_stroke_rgba is not None
        else [0, 0, 0, 0]
    )
    fill = getattr(style, "fill", None)
    if not isinstance(fill, (list, tuple)) or len(fill) != 3:
        raise StyleEvidenceBlocked("source fill color is unknown")
    body = {
        "font": {
            "family": str(getattr(font_match, "family", "")).strip(),
            "asset_sha256": hashlib.sha256(font_path.read_bytes()).hexdigest(),
            "match_method": "reviewed_visual_match",
            "confidence": min(style_confidence, font_confidence),
        },
        "font_size_px": font_size,
        "fill_rgba": _rgba([int(fill[0]), int(fill[1]), int(fill[2]), 255], "fill"),
        "stroke_rgba": stroke_rgba,
        "stroke_width_px": stroke_width,
        "letter_spacing_px": float(getattr(style, "char_gap", 0.0)),
        "line_spacing_px": float(getattr(style, "line_gap", 0.0)),
        "horizontal_scale": float(getattr(style, "width_ratio", 1.0)),
        "writing_mode": "horizontal-tb" if orientation == "horizontal" else "vertical-rl",
        "alignment": alignment,
        "rotation_deg": float(rotation_deg),
        "anchor": [float(block_bbox[0]), float(block_bbox[1])],
        "line_boxes": normalized_lines,
    }
    if not body["font"]["family"]:
        raise StyleEvidenceBlocked("matched font family is missing")
    return {**body, "style_sha256": canonical_hash(body)}
