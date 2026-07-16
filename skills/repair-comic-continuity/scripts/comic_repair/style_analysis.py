from dataclasses import dataclass
from typing import Optional, Sequence

import cv2
import numpy as np
from PIL import Image


_CATEGORIES = ("sans", "serif", "rounded", "handwriting", "display", "condensed")


@dataclass
class TextStyle:
    fill: tuple[int, int, int]
    orientation: str
    font_size: int
    weight_score: float
    width_ratio: float
    char_gap: int
    line_gap: int
    stroke_width: int
    category_scores: dict[str, float]
    confidence: float
    warnings: list[str]


@dataclass
class _Candidate:
    fill: tuple[int, int, int]
    mask: np.ndarray
    labels: np.ndarray
    stats: np.ndarray
    component_ids: np.ndarray
    primary_ids: np.ndarray
    contrast: float
    foreground_ratio: float
    border_occupancy: float
    structure_score: float
    confidence: float
    complex_background: bool


def _smooth_categories() -> dict[str, float]:
    return {name: 1.0 / len(_CATEGORIES) for name in _CATEGORIES}


def _default(orientation: str, warnings: list[str], fill=(0, 0, 0)) -> TextStyle:
    for warning in ("insufficient_glyph_components", "style_low_confidence"):
        if warning not in warnings:
            warnings.append(warning)
    return TextStyle(
        fill=fill,
        orientation=orientation,
        font_size=8,
        weight_score=0.0,
        width_ratio=0.0,
        char_gap=0,
        line_gap=0,
        stroke_width=0,
        category_scores=_smooth_categories(),
        confidence=0.05,
        warnings=warnings,
    )


def _runs(active: np.ndarray) -> list[tuple[int, int]]:
    indices = np.flatnonzero(active)
    if indices.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(indices) > 1)
    starts = np.r_[indices[0], indices[breaks + 1]]
    ends = np.r_[indices[breaks], indices[-1]] + 1
    return [(int(start), int(end)) for start, end in zip(starts, ends)]


def _entropy(gray: np.ndarray) -> float:
    histogram = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    probabilities = histogram[histogram > 0] / gray.size
    return float(-(probabilities * np.log2(probabilities)).sum())


def _finite_box(value, name: str) -> tuple[int, int, int, int]:
    try:
        numbers = np.asarray(tuple(value), dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain four finite coordinates") from error
    if numbers.shape != (4,) or not np.all(np.isfinite(numbers)):
        raise ValueError(f"{name} must contain four finite coordinates")
    return tuple(int(round(float(item))) for item in numbers)


def _prepare_line_boxes(
    line_boxes: Optional[Sequence[Sequence[float]]],
    crop_box: tuple[int, int, int, int],
) -> list[tuple[int, int, int, int]]:
    if not line_boxes:
        return []
    crop_x1, crop_y1, crop_x2, crop_y2 = crop_box
    local = []
    for value in line_boxes:
        x1, y1, x2, y2 = _finite_box(value, "line_boxes")
        x1, x2 = max(crop_x1, x1), min(crop_x2, x2)
        y1, y2 = max(crop_y1, y1), min(crop_y2, y2)
        if x2 > x1 and y2 > y1:
            local.append((x1 - crop_x1, y1 - crop_y1, x2 - crop_x1, y2 - crop_y1))
    return local


def _estimated_line_height(
    shape: tuple[int, int],
    orientation: str,
    local_lines: list[tuple[int, int, int, int]],
) -> float:
    if local_lines:
        sizes = [
            (y2 - y1) if orientation == "horizontal" else (x2 - x1)
            for x1, y1, x2, y2 in local_lines
        ]
        return float(np.clip(np.median(sizes) * 0.78, 8, 256))
    cross_axis = shape[0] if orientation == "horizontal" else shape[1]
    return float(np.clip(cross_axis * 0.65, 8, 72))


def _line_union(shape, local_lines):
    if not local_lines:
        return np.ones(shape, dtype=bool)
    union = np.zeros(shape, dtype=bool)
    for x1, y1, x2, y2 in local_lines:
        union[y1:y2, x1:x2] = True
    return union


def _remove_frame_lines(mask: np.ndarray, line_height: float) -> np.ndarray:
    result = mask.copy()
    row_ratio = np.count_nonzero(result, axis=1) / max(1, result.shape[1])
    column_ratio = np.count_nonzero(result, axis=0) / max(1, result.shape[0])
    thick = max(1, min(4, int(round(line_height * 0.05))))
    rows = np.flatnonzero(row_ratio > 0.72)
    columns = np.flatnonzero(column_ratio > 0.72)
    for row in rows:
        result[max(0, row - thick) : min(result.shape[0], row + thick + 1), :] = 0
    for column in columns:
        result[:, max(0, column - thick) : min(result.shape[1], column + thick + 1)] = 0
    return result


def _candidate_mask(gray: np.ndarray, dark_text: bool, line_height: float) -> tuple[np.ndarray, float]:
    smooth = cv2.GaussianBlur(gray, (3, 3), 0)
    kernel_size = int(np.clip(round(line_height * 1.35), 15, 151))
    if kernel_size % 2 == 0:
        kernel_size += 1
    background = cv2.GaussianBlur(smooth, (kernel_size, kernel_size), 0)
    delta = (
        background.astype(np.int16) - smooth.astype(np.int16)
        if dark_text
        else smooth.astype(np.int16) - background.astype(np.int16)
    )
    response = np.clip(delta, 0, 255).astype(np.uint8)
    otsu, local = cv2.threshold(response, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    response_floor = max(4, int(round(otsu * 0.55)))

    block_size = int(np.clip(round(line_height * 1.7), 15, 151))
    if block_size % 2 == 0:
        block_size += 1
    adaptive_mode = cv2.THRESH_BINARY_INV if dark_text else cv2.THRESH_BINARY
    adaptive = cv2.adaptiveThreshold(
        smooth, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, adaptive_mode, block_size, 5
    )
    mask = np.where((local > 0) | ((adaptive > 0) & (response >= response_floor)), 255, 0).astype(np.uint8)
    positive = response[mask > 0]
    contrast = float(np.median(positive)) if positive.size else 0.0
    return mask, contrast


def _filter_components(
    mask: np.ndarray,
    line_height: float,
    local_lines: list[tuple[int, int, int, int]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 1:
        return np.zeros_like(mask), labels, stats, np.array([], dtype=int), np.array([], dtype=int)

    component_ids = np.arange(1, count)
    areas = stats[1:, cv2.CC_STAT_AREA]
    widths = stats[1:, cv2.CC_STAT_WIDTH]
    heights = stats[1:, cv2.CC_STAT_HEIGHT]
    minimum_area = max(1, int(round(line_height * line_height * 0.0022)))
    minimum_extent = max(2, int(round(line_height * 0.08)))
    not_background_plane = (widths < mask.shape[1] * 0.88) & (heights < mask.shape[0] * 0.88)
    primary_selector = (
        (areas >= minimum_area)
        & (np.maximum(widths, heights) >= minimum_extent)
        & not_background_plane
    )
    primary_ids = component_ids[primary_selector]
    if primary_ids.size == 0:
        return np.zeros_like(mask), labels, stats, np.array([], dtype=int), primary_ids

    primary_mask = np.isin(labels, primary_ids).astype(np.uint8)
    radius = max(2, min(31, int(round(line_height * 0.58))))
    near_primary = cv2.dilate(
        primary_mask,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1, radius * 2 + 1)),
    )
    keep = set(int(value) for value in primary_ids)
    line_union = _line_union(mask.shape, local_lines)
    for index, component_id in enumerate(component_ids):
        if int(component_id) in keep or areas[index] < 1:
            continue
        center_x, center_y = np.rint(centroids[component_id]).astype(int)
        center_x = int(np.clip(center_x, 0, mask.shape[1] - 1))
        center_y = int(np.clip(center_y, 0, mask.shape[0] - 1))
        if near_primary[center_y, center_x] and line_union[center_y, center_x]:
            keep.add(int(component_id))

    kept_ids = np.asarray(sorted(keep), dtype=int)
    filtered = (np.isin(labels, kept_ids).astype(np.uint8) * 255)
    return filtered, labels, stats, kept_ids, primary_ids


def _projection_groups(
    mask: np.ndarray,
    orientation: str,
    local_lines: list[tuple[int, int, int, int]],
    line_height: float,
) -> list[tuple[int, int]]:
    regions = local_lines
    if not regions:
        points = cv2.findNonZero(mask)
        if points is None:
            return []
        x, y, width, height = cv2.boundingRect(points)
        regions = [(x, y, x + width, y + height)]
    groups = []
    close_size = max(1, min(9, int(round(line_height * 0.07))))
    for x1, y1, x2, y2 in regions:
        region = mask[y1:y2, x1:x2]
        if region.size == 0:
            continue
        kernel = (
            cv2.getStructuringElement(cv2.MORPH_RECT, (close_size, 1))
            if orientation == "horizontal"
            else cv2.getStructuringElement(cv2.MORPH_RECT, (1, close_size))
        )
        closed = cv2.morphologyEx(region, cv2.MORPH_CLOSE, kernel)
        projection = (
            np.count_nonzero(closed, axis=0)
            if orientation == "horizontal"
            else np.count_nonzero(closed, axis=1)
        )
        groups.extend(_runs(projection > 0))
    return [(start, end) for start, end in groups if end - start >= 1]


def _build_candidate(
    gray: np.ndarray,
    dark_text: bool,
    orientation: str,
    line_height: float,
    local_lines: list[tuple[int, int, int, int]],
    entropy: float,
    edge_density: float,
) -> _Candidate:
    raw_mask, contrast = _candidate_mask(gray, dark_text, line_height)
    raw_mask = _remove_frame_lines(raw_mask, line_height)
    mask, labels, stats, component_ids, primary_ids = _filter_components(
        raw_mask, line_height, local_lines
    )
    line_union = _line_union(mask.shape, local_lines)
    foreground = mask > 0
    analysis_area = max(1, int(np.count_nonzero(line_union)))
    foreground_ratio = float(np.count_nonzero(foreground & line_union)) / analysis_area
    foreground_total = max(1, int(np.count_nonzero(foreground)))
    line_fraction = float(np.count_nonzero(foreground & line_union)) / foreground_total

    border_width = max(1, min(5, int(round(line_height * 0.04))))
    border = np.zeros(mask.shape, dtype=bool)
    border[:border_width] = True
    border[-border_width:] = True
    border[:, :border_width] = True
    border[:, -border_width:] = True
    border_occupancy = float(np.count_nonzero(foreground & border)) / max(1, np.count_nonzero(border))

    groups = _projection_groups(mask, orientation, local_lines, line_height)
    projection_score = min(1.0, len(groups) / 4.0)
    component_score = min(1.0, np.log1p(primary_ids.size) / np.log(12.0))
    if primary_ids.size >= 2:
        component_heights = stats[primary_ids, cv2.CC_STAT_HEIGHT].astype(float)
        component_widths = stats[primary_ids, cv2.CC_STAT_WIDTH].astype(float)
        scales = np.maximum(component_heights, component_widths)
        consistency = float(np.exp(-np.median(np.abs(scales - np.median(scales))) / max(1.0, np.median(scales))))
    else:
        consistency = 0.0
    foreground_score = float(np.clip(1.0 - abs(foreground_ratio - 0.13) / 0.20, 0.0, 1.0))
    contrast_score = float(np.clip(contrast / 80.0, 0.0, 1.0))
    structure_score = (
        0.24 * component_score
        + 0.24 * projection_score
        + 0.16 * consistency
        + 0.18 * foreground_score
        + 0.10 * line_fraction
        + 0.08 * contrast_score
        - 0.20 * min(1.0, border_occupancy * 4.0)
    )
    structure_score = float(np.clip(structure_score, 0.0, 1.0))

    complex_background = (entropy > 5.7 and edge_density > 0.12) or edge_density > 0.27
    confidence = 0.05 + 0.82 * structure_score
    if complex_background:
        confidence *= 0.78 if structure_score >= 0.55 else 0.42
    if foreground_ratio > 0.48 or primary_ids.size < 2:
        confidence = min(confidence, 0.30)
    confidence = float(np.clip(confidence, 0.0, 0.95))
    return _Candidate(
        fill=(0, 0, 0) if dark_text else (255, 255, 255),
        mask=mask,
        labels=labels,
        stats=stats,
        component_ids=component_ids,
        primary_ids=primary_ids,
        contrast=contrast,
        foreground_ratio=foreground_ratio,
        border_occupancy=border_occupancy,
        structure_score=structure_score,
        confidence=confidence,
        complex_background=complex_background,
    )


def _font_and_line_metrics(
    mask: np.ndarray,
    orientation: str,
    local_lines: list[tuple[int, int, int, int]],
) -> tuple[int, int]:
    ink_sizes = []
    if local_lines:
        for x1, y1, x2, y2 in local_lines:
            points = cv2.findNonZero(mask[y1:y2, x1:x2])
            if points is not None:
                _, _, width, height = cv2.boundingRect(points)
                ink_sizes.append(height if orientation == "horizontal" else width)
        box_sizes = [
            (y2 - y1) if orientation == "horizontal" else (x2 - x1)
            for x1, y1, x2, y2 in local_lines
        ]
        ink_size = float(np.median(ink_sizes)) if ink_sizes else float(np.median(box_sizes))
        font_size = 0.65 * float(np.median(box_sizes)) + 0.35 * ink_size
        axis_start = 1 if orientation == "horizontal" else 0
        axis_end = 3 if orientation == "horizontal" else 2
        ordered = sorted(local_lines, key=lambda item: item[axis_start])
        gaps = [
            max(0, ordered[index + 1][axis_start] - ordered[index][axis_end])
            for index in range(len(ordered) - 1)
        ]
        line_gap = int(round(float(np.median(gaps)))) if gaps else 0
    else:
        points = cv2.findNonZero(mask)
        if points is None:
            return 8, 0
        _, _, width, height = cv2.boundingRect(points)
        font_size = height if orientation == "horizontal" else width
        line_gap = 0
    return int(np.clip(round(font_size), 8, 256)), int(np.clip(line_gap, 0, 256))


def _layout_metrics(
    mask: np.ndarray,
    orientation: str,
    local_lines: list[tuple[int, int, int, int]],
    font_size: int,
) -> tuple[float, int]:
    groups = _projection_groups(mask, orientation, local_lines, float(font_size))
    sizes = [end - start for start, end in groups]
    gaps = [max(0, groups[index + 1][0] - groups[index][1]) for index in range(len(groups) - 1)]
    width_ratio = float(np.clip((np.median(sizes) if sizes else 0.0) / max(1, font_size), 0.0, 8.0))
    char_gap = int(np.clip(round(float(np.median(gaps))) if gaps else 0, 0, 256))
    return width_ratio, char_gap


def _glyph_regions(
    mask: np.ndarray,
    orientation: str,
    local_lines: list[tuple[int, int, int, int]],
    font_size: int,
) -> list[tuple[int, int, int, int]]:
    regions = local_lines
    if not regions:
        points = cv2.findNonZero(mask)
        if points is None:
            return []
        x, y, width, height = cv2.boundingRect(points)
        regions = [(x, y, x + width, y + height)]

    glyphs = []
    close_size = max(3, min(17, int(round(font_size * 0.16))))
    if close_size % 2 == 0:
        close_size += 1
    for line_x1, line_y1, line_x2, line_y2 in regions:
        line = mask[line_y1:line_y2, line_x1:line_x2]
        if line.size == 0:
            continue
        kernel = (
            cv2.getStructuringElement(cv2.MORPH_RECT, (close_size, 1))
            if orientation == "horizontal"
            else cv2.getStructuringElement(cv2.MORPH_RECT, (1, close_size))
        )
        closed = cv2.morphologyEx(line, cv2.MORPH_CLOSE, kernel)
        projection = (
            np.count_nonzero(closed, axis=0)
            if orientation == "horizontal"
            else np.count_nonzero(closed, axis=1)
        )
        for start, end in _runs(projection > 0):
            span = end - start
            pieces = max(1, int(round(span / max(1, font_size)))) if span > font_size * 1.35 else 1
            boundaries = np.rint(np.linspace(start, end, pieces + 1)).astype(int)
            for index in range(pieces):
                piece_start, piece_end = int(boundaries[index]), int(boundaries[index + 1])
                piece = (
                    line[:, piece_start:piece_end]
                    if orientation == "horizontal"
                    else line[piece_start:piece_end, :]
                )
                points = cv2.findNonZero(piece)
                if points is None:
                    continue
                x, y, width, height = cv2.boundingRect(points)
                if orientation == "horizontal":
                    glyphs.append(
                        (line_x1 + piece_start + x, line_y1 + y, line_x1 + piece_start + x + width, line_y1 + y + height)
                    )
                else:
                    glyphs.append(
                        (line_x1 + x, line_y1 + piece_start + y, line_x1 + x + width, line_y1 + piece_start + y + height)
                    )
    return glyphs


def _weight_metrics(
    candidate: _Candidate,
    font_size: int,
    orientation: str,
    local_lines: list[tuple[int, int, int, int]],
) -> tuple[float, int]:
    glyphs = _glyph_regions(candidate.mask, orientation, local_lines, font_size)
    if not glyphs:
        return 0.0, 0
    distance = cv2.distanceTransform(candidate.mask, cv2.DIST_L2, 5)
    group_distances = []
    group_occupancies = []
    for x1, y1, x2, y2 in glyphs:
        glyph_mask = candidate.mask[y1:y2, x1:x2] > 0
        ink_count = int(np.count_nonzero(glyph_mask))
        if ink_count == 0:
            continue
        glyph_distance = distance[y1:y2, x1:x2][glyph_mask]
        group_distances.append(float(np.percentile(glyph_distance, 75)))
        group_occupancies.append(float(ink_count / max(1, glyph_mask.size)))
    if not group_distances:
        return 0.0, 0
    distances = np.asarray(group_distances)
    occupancies = np.asarray(group_occupancies)
    if distances.size >= 5:
        low, high = np.percentile(distances, (15, 85))
        selected = (distances >= low) & (distances <= high)
        distances = distances[selected]
        occupancies = occupancies[selected]
    stroke_estimate = 2.0 * float(np.median(distances))
    component_ink = float(np.median(occupancies))
    relative_stroke = stroke_estimate / max(1, font_size)
    weight_score = float(np.clip(5.2 * relative_stroke + 0.28 * component_ink - 0.03, 0.0, 1.0))
    stroke_width = int(np.clip(round(stroke_estimate), 0, min(64, max(1, font_size // 2))))
    return weight_score, stroke_width


def _category_prior(width_ratio: float, weight_score: float) -> dict[str, float]:
    values = {
        "sans": 0.34,
        "serif": 0.17,
        "rounded": 0.13,
        "handwriting": 0.11,
        "display": 0.13 + 0.10 * weight_score,
        "condensed": 0.12 + max(0.0, 0.9 - width_ratio) * 0.18,
    }
    total = sum(values.values())
    return {name: float(value / total) for name, value in values.items()}


def analyze_text_style(image, box, orientation, line_boxes=None) -> TextStyle:
    if orientation not in ("horizontal", "vertical"):
        raise ValueError("orientation must be 'horizontal' or 'vertical'")
    if isinstance(image, Image.Image):
        rgb = np.asarray(image.convert("RGB"))
    elif isinstance(image, np.ndarray):
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("numpy image must be RGB with shape (height, width, 3)")
        rgb = np.asarray(image, dtype=np.uint8)
    else:
        raise TypeError("image must be a PIL RGB image or numpy RGB array")

    raw_x1, raw_y1, raw_x2, raw_y2 = _finite_box(box, "box")
    warnings: list[str] = []
    image_height, image_width = rgb.shape[:2]
    x1, y1 = max(0, raw_x1), max(0, raw_y1)
    x2, y2 = min(image_width, raw_x2), min(image_height, raw_y2)
    if (x1, y1, x2, y2) != (raw_x1, raw_y1, raw_x2, raw_y2):
        warnings.append("crop_clamped")
    if x2 <= x1 or y2 <= y1:
        return _default(orientation, warnings + ["empty_crop"])

    crop = np.ascontiguousarray(rgb[y1:y2, x1:x2])
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    default_fill = (0, 0, 0) if float(gray.mean()) >= 128 else (255, 255, 255)
    local_lines = _prepare_line_boxes(line_boxes, (x1, y1, x2, y2))
    if gray.size < 16 or float(gray.std()) < 3.0:
        return _default(orientation, warnings, default_fill)

    line_height = _estimated_line_height(gray.shape, orientation, local_lines)
    entropy = _entropy(gray)
    edge_density = float(np.count_nonzero(cv2.Canny(gray, 60, 160))) / gray.size
    candidates = [
        _build_candidate(gray, dark, orientation, line_height, local_lines, entropy, edge_density)
        for dark in (True, False)
    ]
    candidate = max(candidates, key=lambda value: (value.structure_score, value.contrast))
    if candidate.component_ids.size == 0:
        return _default(orientation, warnings, candidate.fill)

    if candidate.complex_background:
        warnings.append("complex_background")
    if candidate.primary_ids.size < 2:
        warnings.append("insufficient_glyph_components")

    font_size, line_gap = _font_and_line_metrics(candidate.mask, orientation, local_lines)
    width_ratio, char_gap = _layout_metrics(candidate.mask, orientation, local_lines, font_size)
    weight_score, stroke_width = _weight_metrics(
        candidate, font_size, orientation, local_lines
    )
    confidence = candidate.confidence
    if confidence < 0.4:
        warnings.append("style_low_confidence")
    categories = _smooth_categories() if confidence < 0.4 else _category_prior(width_ratio, weight_score)
    return TextStyle(
        fill=candidate.fill,
        orientation=orientation,
        font_size=font_size,
        weight_score=weight_score,
        width_ratio=width_ratio,
        char_gap=char_gap,
        line_gap=line_gap,
        stroke_width=stroke_width,
        category_scores=categories,
        confidence=confidence,
        warnings=warnings,
    )
