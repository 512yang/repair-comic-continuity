from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .font_catalog import FontCandidate
from .style_analysis import TextStyle


_GLYPH_CANVAS = (96, 96)
_LINE_CANVAS = (96, 384)
_VERTICAL_CANVAS = (384, 96)
_MAX_RENDER_DIMENSION = 4096
_CacheInfo = namedtuple("CacheInfo", "hits misses maxsize currsize")


@dataclass
class FontMatch:
    family: str
    path: Path
    weight: int
    score: float
    confidence: float
    fallback_used: bool
    top_candidates: list[dict]
    warnings: list[str]


@dataclass(frozen=True)
class _Descriptor:
    mask: np.ndarray
    edge: np.ndarray
    row_projection: np.ndarray
    column_projection: np.ndarray
    area: int


@dataclass(frozen=True)
class _Piece:
    text: str
    mode: str
    descriptor: _Descriptor


@dataclass(frozen=True)
class _SourcePlan:
    pieces: tuple[_Piece, ...]
    reliable_glyphs: bool


def _freeze(array: np.ndarray) -> np.ndarray:
    array.setflags(write=False)
    return array


def _is_han(char: str) -> bool:
    value = ord(char)
    return (
        value == 0x3007
        or 0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF
        or 0x20000 <= value <= 0x323AF
    )


def _han_text(text: str) -> str:
    return "".join(char for char in (text or "") if _is_han(char))


def _as_gray(crop) -> np.ndarray:
    if isinstance(crop, Image.Image):
        return np.asarray(crop.convert("L"), dtype=np.uint8)
    if not isinstance(crop, np.ndarray):
        raise TypeError("crop must be a PIL image or numpy array")
    array = np.asarray(crop)
    if array.ndim == 2:
        return np.asarray(array, dtype=np.uint8)
    if array.ndim == 3 and array.shape[2] == 3:
        return cv2.cvtColor(np.asarray(array, dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    raise ValueError("crop numpy array must be gray or RGB")


def _ink_mask(gray: np.ndarray) -> np.ndarray:
    if gray.size == 0:
        return np.zeros(gray.shape, dtype=np.uint8)
    border = np.concatenate((gray[0], gray[-1], gray[:, 0], gray[:, -1]))
    background = float(np.median(border))
    delta = np.abs(gray.astype(np.float32) - background).astype(np.uint8)
    if int(delta.max()) < 3:
        return np.zeros(gray.shape, dtype=np.uint8)
    threshold, _ = cv2.threshold(delta, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    floor = max(5, int(round(threshold * 0.55)))
    return (delta >= floor).astype(np.uint8)


def _trim(mask: np.ndarray) -> np.ndarray:
    points = cv2.findNonZero((mask > 0).astype(np.uint8))
    if points is None:
        return np.zeros((1, 1), dtype=np.uint8)
    x, y, width, height = cv2.boundingRect(points)
    return (mask[y : y + height, x : x + width] > 0).astype(np.uint8)


def _normalize(mask: np.ndarray, canvas=_GLYPH_CANVAS) -> np.ndarray:
    if isinstance(canvas, int):
        canvas = (canvas, canvas)
    canvas_height, canvas_width = canvas
    source = _trim(mask)
    output = np.zeros((canvas_height, canvas_width), dtype=np.uint8)
    if not source.any():
        return output
    height, width = source.shape
    scale = min((canvas_height - 12) / height, (canvas_width - 12) / width)
    resized = cv2.resize(
        source,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_NEAREST,
    )
    resized = (resized >= 0.35).astype(np.uint8)
    y = (canvas_height - resized.shape[0]) // 2
    x = (canvas_width - resized.shape[1]) // 2
    output[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
    return output


def _set_weight(font: ImageFont.FreeTypeFont, weight: int) -> bool:
    setter = getattr(font, "set_variation_by_axes", None)
    axes_getter = getattr(font, "get_variation_axes", None)
    if setter is None or axes_getter is None:
        return False
    try:
        axes = axes_getter()
        if not axes:
            return False
        values = []
        for axis in axes:
            name = axis.get("name", b"")
            if isinstance(name, bytes):
                name = name.decode("ascii", "ignore")
            requested = weight if "weight" in str(name).lower() else axis.get("default", axis["minimum"])
            values.append(max(axis["minimum"], min(axis["maximum"], requested)))
        setter(values)
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _limit_mask(mask: np.ndarray) -> np.ndarray:
    longest = max(mask.shape)
    if longest <= _MAX_RENDER_DIMENSION:
        return mask
    scale = _MAX_RENDER_DIMENSION / longest
    return cv2.resize(
        mask,
        (max(1, round(mask.shape[1] * scale)), max(1, round(mask.shape[0] * scale))),
        interpolation=cv2.INTER_AREA,
    ) >= 0.35


@lru_cache(maxsize=1024)
def _render_cached(font_path: str, size: int, weight: int, text: str, stroke_width: int) -> np.ndarray:
    font = ImageFont.truetype(font_path, size)
    _set_weight(font, weight)
    probe = Image.new("L", (1, 1), 255)
    box = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font, stroke_width=stroke_width)
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    scale = min(1.0, (_MAX_RENDER_DIMENSION - 16) / max(width, height))
    if scale < 1.0:
        font = ImageFont.truetype(font_path, max(8, round(size * scale)))
        _set_weight(font, weight)
        box = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        width, height = max(1, box[2] - box[0]), max(1, box[3] - box[1])
    image = Image.new("L", (min(width + 16, _MAX_RENDER_DIMENSION), min(height + 16, _MAX_RENDER_DIMENSION)), 255)
    ImageDraw.Draw(image).text(
        (8 - box[0], 8 - box[1]), text, font=font, fill=0,
        stroke_width=stroke_width, stroke_fill=0,
    )
    return _freeze(np.asarray(_limit_mask(_ink_mask(np.asarray(image))), dtype=np.uint8))


@lru_cache(maxsize=512)
def _render_vertical_cached(
    font_path: str, size: int, weight: int, text: str, stroke_width: int,
) -> np.ndarray:
    font = ImageFont.truetype(font_path, size)
    _set_weight(font, weight)
    advance = max(1, round(size * 1.08))
    image = Image.new("L", (size * 2, min(_MAX_RENDER_DIMENSION, advance * len(text) + 16)), 255)
    draw = ImageDraw.Draw(image)
    for index, char in enumerate(text):
        draw.text((8, 8 + index * advance), char, font=font, fill=0,
                  stroke_width=stroke_width, stroke_fill=0)
    return _freeze(np.asarray(_ink_mask(np.asarray(image)), dtype=np.uint8))


def _render_text_mask(
    font_path: Path, size: int, weight: int, text: str, stroke_width: int,
    variable_weight: bool = False,
) -> np.ndarray:
    return _render_cached(str(Path(font_path)), int(size), int(weight), text, int(stroke_width))


def _canvas_for_mode(mode: str) -> tuple[int, int]:
    if mode == "horizontal":
        return _LINE_CANVAS
    if mode == "vertical":
        return _VERTICAL_CANVAS
    return _GLYPH_CANVAS


def _make_descriptor(mask: np.ndarray, mode: str) -> _Descriptor:
    normalized = _freeze(_normalize(mask, _canvas_for_mode(mode)))
    edge = _freeze((cv2.Canny(normalized * 255, 50, 150) > 0).astype(np.uint8))
    row = _freeze(normalized.sum(axis=1).astype(np.float32))
    column = _freeze(normalized.sum(axis=0).astype(np.float32))
    return _Descriptor(normalized, edge, row, column, int(normalized.sum()))


@lru_cache(maxsize=2048)
def _candidate_descriptor_cached(
    font_path: str, size: int, weight: int, text: str, stroke_width: int, mode: str,
) -> _Descriptor:
    if mode == "vertical":
        mask = _render_vertical_cached(font_path, size, weight, text, stroke_width)
    else:
        mask = _render_text_mask(Path(font_path), size, weight, text, stroke_width)
    return _make_descriptor(mask, mode)


def clear_render_cache() -> None:
    _render_cached.cache_clear()
    _render_vertical_cached.cache_clear()
    _candidate_descriptor_cached.cache_clear()
    _probe_font.cache_clear()


def render_cache_info():
    infos = (
        _render_cached.cache_info(),
        _render_vertical_cached.cache_info(),
        _candidate_descriptor_cached.cache_info(),
    )
    return _CacheInfo(
        sum(info.hits for info in infos),
        sum(info.misses for info in infos),
        sum(info.maxsize for info in infos),
        sum(info.currsize for info in infos),
    )


def _shift(mask: np.ndarray, dx: int, dy: int) -> np.ndarray:
    transform = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(mask, transform, (mask.shape[1], mask.shape[0]), flags=cv2.INTER_NEAREST)


def _cosine(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    return float(np.dot(left, right) / denominator) if denominator else 0.0


def _descriptor_score(left: _Descriptor, right: _Descriptor) -> float:
    left_mask = left.mask > 0
    left_edge = left.edge > 0
    best = 0.0
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            shifted = _shift(right.mask, dx, dy) > 0
            shifted_edge = _shift(right.edge, dx, dy) > 0
            intersection = int(np.count_nonzero(left_mask & shifted))
            union = int(np.count_nonzero(left_mask | shifted))
            iou = intersection / max(1, union)
            row = _cosine(left.row_projection, shifted.sum(axis=1).astype(np.float32))
            column = _cosine(left.column_projection, shifted.sum(axis=0).astype(np.float32))
            ink = np.exp(-abs(left.area - int(shifted.sum())) / max(1, left.area))
            edge_union = np.count_nonzero(left_edge | shifted_edge)
            edge_iou = np.count_nonzero(left_edge & shifted_edge) / max(1, edge_union)
            best = max(best, float(0.44 * iou + 0.18 * row + 0.18 * column + 0.12 * ink + 0.08 * edge_iou))
    return float(np.clip(best, 0.0, 1.0))


def _equal_regions(mask: np.ndarray, count: int, axis: int) -> list[np.ndarray]:
    source = _trim(mask)
    if count < 1 or not source.any():
        return []
    length = source.shape[axis]
    boundaries = np.rint(np.linspace(0, length, count + 1)).astype(int)
    regions = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        piece = source[:, start:end] if axis == 1 else source[start:end, :]
        regions.append(_trim(piece))
    return regions


def _projection_glyphs(mask: np.ndarray, count: int, axis: int = 1) -> tuple[list[np.ndarray], bool]:
    source = _trim(mask)
    if count < 1 or not source.any():
        return [], False
    projection = source.sum(axis=0 if axis == 1 else 1)
    active = np.flatnonzero(projection > 0)
    if active.size == 0:
        return [], False
    breaks = np.flatnonzero(np.diff(active) > 1)
    starts = list(np.r_[active[0], active[breaks + 1]])
    ends = list(np.r_[active[breaks], active[-1]] + 1)
    merge_gap = max(1, round(source.shape[1 - axis] * 0.04))
    groups: list[list[int]] = []
    for start, end in zip(starts, ends):
        if groups and start - groups[-1][1] <= merge_gap:
            groups[-1][1] = int(end)
        else:
            groups.append([int(start), int(end)])
    if len(groups) != count:
        return _equal_regions(mask, count, axis), False
    widths = np.asarray([end - start for start, end in groups], dtype=float)
    reliable = bool(widths.min() >= max(2, np.median(widths) * 0.35))
    pieces = [
        _trim(source[:, start:end] if axis == 1 else source[start:end, :])
        for start, end in groups
    ]
    return pieces, reliable


def _chunks(text: str, maximum: int = 6) -> list[str]:
    return [text[index : index + maximum] for index in range(0, len(text), maximum) if text[index : index + maximum]]


def _text_regions(mask: np.ndarray, fragments: Sequence[str], axis: int) -> list[np.ndarray]:
    source = _trim(mask)
    total = sum(len(fragment) for fragment in fragments)
    if total < 1 or not source.any():
        return []
    cumulative = np.cumsum([0] + [len(fragment) for fragment in fragments])
    boundaries = np.rint(cumulative * source.shape[axis] / total).astype(int)
    regions = []
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        piece = source[:, start:end] if axis == 1 else source[start:end, :]
        regions.append(_trim(piece))
    return regions


def _source_plan(mask: np.ndarray, text: str, style: TextStyle) -> _SourcePlan:
    han = _han_text(text)
    if not han or not mask.any():
        return _SourcePlan((), False)
    if style.orientation == "horizontal" and "\n" not in text and len(han) <= 8:
        glyphs, reliable = _projection_glyphs(mask, len(han), axis=1)
        if reliable:
            pieces = tuple(
                _Piece(char, "glyph", _make_descriptor(glyph, "glyph"))
                for glyph, char in zip(glyphs, han) if glyph.sum() >= 8
            )
            if len(pieces) >= max(2, int(np.ceil(len(han) * 0.6))):
                return _SourcePlan(pieces, True)
        if text == han:
            glyphs = _equal_regions(mask, len(han), axis=1)
            pieces = tuple(
                _Piece(char, "glyph", _make_descriptor(glyph, "glyph"))
                for glyph, char in zip(glyphs, han) if glyph.sum() >= 8
            )
            if len(pieces) >= max(2, int(np.ceil(len(han) * 0.6))):
                return _SourcePlan(pieces, False)

    orientation = "vertical" if style.orientation == "vertical" else "horizontal"
    lines = [line for line in text.splitlines() if line] or [text]
    line_regions = _equal_regions(mask, len(lines), axis=0) if orientation == "horizontal" else [_trim(mask)]
    pieces = []
    for line, line_mask in zip(lines, line_regions):
        fragments = _chunks(line)
        regions = _text_regions(line_mask, fragments, axis=0 if orientation == "vertical" else 1)
        for fragment, region in zip(fragments, regions):
            if region.any():
                pieces.append(_Piece(fragment, orientation, _make_descriptor(region, orientation)))
    return _SourcePlan(tuple(pieces), False)


def _robust_mean(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = np.sort(np.asarray(values, dtype=float))
    if ordered.size >= 5:
        trim = max(1, int(ordered.size * 0.15))
        ordered = ordered[trim:-trim]
    if ordered.size >= 3:
        return float(0.55 * np.median(ordered) + 0.45 * np.mean(ordered))
    return float(np.mean(ordered))


def _glyph_similarity(plan: _SourcePlan, item: FontCandidate) -> tuple[float, bool]:
    scores = []
    for piece in plan.pieces:
        candidate = _candidate_descriptor_cached(
            str(item.path), 96, item.weight, piece.text, 0, piece.mode
        )
        scores.append(_descriptor_score(piece.descriptor, candidate))
    return _robust_mean(scores), plan.reliable_glyphs


def _target_weight(style: TextStyle) -> float:
    return 100.0 + 800.0 * float(np.clip(style.weight_score, 0.0, 1.0))


def _expected_width(category: str) -> float:
    return {"condensed": 0.68, "rounded": 1.08, "display": 0.96}.get(category, 1.0)


def _weight_fit(item: FontCandidate, style: TextStyle) -> float:
    return float(np.exp(-abs(item.weight - _target_weight(style)) / 230.0))


def _width_fit(item: FontCandidate, style: TextStyle) -> float:
    return float(np.exp(-abs(_expected_width(item.category) - style.width_ratio) / 0.22))


def _prior(item: FontCandidate, style: TextStyle) -> float:
    category = float(style.category_scores.get(item.category, 0.0))
    return float(np.clip(0.50 * category + 0.32 * _weight_fit(item, style) + 0.18 * _width_fit(item, style), 0.0, 1.0))


def _safe_fallback(items: Sequence[FontCandidate], style: TextStyle) -> FontCandidate:
    return max(items, key=lambda item: (_prior(item, style), _width_fit(item, style), _weight_fit(item, style)))


def _prefilter_weights(items: Sequence[FontCandidate], style: TextStyle, limit_per_font: int = 3) -> list[FontCandidate]:
    groups: dict[tuple[str, Path], list[FontCandidate]] = {}
    order: list[tuple[str, Path]] = []
    for item in items:
        key = (item.family, Path(item.path))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    selected = []
    for key in order:
        selected.extend(sorted(groups[key], key=lambda item: _prior(item, style), reverse=True)[:limit_per_font])
    return selected


@lru_cache(maxsize=256)
def _probe_font(path: str, weight: int, variable_weight: bool) -> tuple[bool, bool]:
    font = ImageFont.truetype(path, 16)
    axis_ok = not variable_weight or _set_weight(font, weight)
    return True, axis_ok


def _valid_candidates(candidates: Sequence[FontCandidate], role: str | None) -> tuple[list[FontCandidate], list[str]]:
    valid = []
    warnings = []
    for item in candidates:
        if role is not None and role not in item.roles:
            continue
        if not Path(item.path).is_file():
            warnings.append("font_file_missing")
            continue
        try:
            _, axis_ok = _probe_font(str(item.path), item.weight, item.variable_weight)
        except Exception:
            warnings.append("font_file_invalid")
            continue
        if not axis_ok:
            warnings.append("variable_weight_unsupported")
        valid.append(item)
    if not valid:
        raise ValueError("no valid font candidates")
    return valid, list(dict.fromkeys(warnings))


def _family_ranking(rows: list[dict]) -> list[dict]:
    families: dict[str, dict] = {}
    for row in rows:
        current = families.get(row["item"].family)
        if current is None or (row["visual"], row["score"]) > (current["visual"], current["score"]):
            families[row["item"].family] = row
    return sorted(families.values(), key=lambda row: (row["visual"], row["score"]), reverse=True)


def match_font(
    crop, text: str, style: TextStyle, candidates: Sequence[FontCandidate],
    role: str | None = None,
) -> FontMatch:
    gray = _as_gray(crop)
    crop_mask = _ink_mask(gray)
    valid, warnings = _valid_candidates(candidates, role)
    valid = _prefilter_weights(valid, style)
    han = _han_text(text)
    plan = _source_plan(crop_mask, text, style)
    rows = []
    for item in valid:
        try:
            visual, reliable = _glyph_similarity(plan, item) if plan.pieces else (0.0, False)
            prior = _prior(item, style)
            score = float(np.clip(0.96 * visual + 0.04 * prior, 0.0, 1.0))
            rows.append({"item": item, "score": score, "visual": visual, "prior": prior, "reliable": reliable})
        except Exception:
            warnings.append("font_candidate_failed")
    if not rows:
        raise ValueError("no valid font candidates after scoring")

    rows.sort(key=lambda row: (row["score"], row["visual"]), reverse=True)
    family_rows = _family_ranking(rows)
    family_margin = family_rows[0]["visual"] - family_rows[1]["visual"] if len(family_rows) > 1 else family_rows[0]["visual"]
    visual_available = bool(plan.pieces and family_rows[0]["visual"] > 0.05)
    full_pool_fallback = len(han) < 2 or not visual_available
    low = (
        full_pool_fallback
        or style.confidence < 0.4
        or family_margin < 0.04
        or "variable_weight_unsupported" in warnings
    )

    if full_pool_fallback:
        selected = _safe_fallback([row["item"] for row in rows], style)
    else:
        visual_family = family_rows[0]["item"].family
        same_family = [row for row in rows if row["item"].family == visual_family]
        selected_row = max(
            same_family,
            key=lambda row: 0.90 * row["visual"] + 0.07 * _weight_fit(row["item"], style) + 0.03 * _width_fit(row["item"], style),
        )
        selected = selected_row["item"]
    if low:
        warnings.append("font_match_low_confidence")
    if han and not plan.reliable_glyphs:
        warnings.append("font_match_line_fallback")

    selected_row = next(row for row in rows if row["item"] is selected)
    confidence = float(np.clip(0.68 * selected_row["score"] + 0.32 * min(1.0, family_margin / 0.18), 0.0, 1.0))
    if low:
        confidence = min(confidence, 0.39)
    top = [
        {"family": row["item"].family, "path": row["item"].path,
         "weight": row["item"].weight, "score": row["score"]}
        for row in rows[:3]
    ]
    return FontMatch(
        family=selected.family, path=selected.path, weight=selected.weight,
        score=selected_row["score"], confidence=confidence,
        fallback_used=low, top_candidates=top,
        warnings=list(dict.fromkeys(warnings)),
    )
