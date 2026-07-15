from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


QA_THRESHOLDS = {
    "ocr_confidence": 0.65,
    "font_confidence": 0.50,
    "source_confidence": 0.62,
    "sfx_short_text_max_chars": 2,
    "sfx_review_confidence": 0.70,
    "residual_text_risk": 0.62,
    "background_damage_risk": 0.34,
    "minimum_mask_pixels": 4,
    "mask_boundary_width": 5,
    "contact_sheet_max_dimension": 8192,
    "contact_cell_width": 760,
    "contact_cell_height": 470,
}

WARNING_CODES = {
    "text_overflow", "font_match_low_confidence", "ocr_low_confidence",
    "source_match_low_confidence", "source_match_too_long_kept_ocr",
    "possible_text_residual", "background_damage_risk",
    "sfx_classification_review", "page_processing_failed",
    "internal_review_warning",
}


def _value(obj: Any, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _jsonable(value: Any):
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _array(image: Any, gray: bool = False) -> np.ndarray:
    if isinstance(image, (str, Path)):
        with Image.open(image) as loaded:
            array = np.asarray(loaded.convert("L" if gray else "RGB"))
    elif isinstance(image, Image.Image):
        array = np.asarray(image.convert("L" if gray else "RGB"))
    else:
        array = np.asarray(image)
        if gray and array.ndim == 3:
            array = cv2.cvtColor(array.astype(np.uint8), cv2.COLOR_RGB2GRAY)
    if gray and array.ndim != 2:
        raise ValueError("image must be grayscale or RGB")
    return array.astype(np.uint8, copy=False)


def _validated_images(original: Any, clean: Any, mask: Any):
    original_gray = _array(original, True)
    clean_gray = _array(clean, True)
    mask_gray = _array(mask, True)
    if original_gray.shape != clean_gray.shape or original_gray.shape != mask_gray.shape:
        raise ValueError("image and mask size mismatch")
    return original_gray, clean_gray, mask_gray > 0


def check_block(block_json_or_block: Any, layout: Any = None) -> list[str]:
    raw_warnings = list(_value(block_json_or_block, "warnings", []) or [])
    warnings = [code for code in raw_warnings if code in WARNING_CODES]
    if any(code not in WARNING_CODES for code in raw_warnings):
        warnings.append("internal_review_warning")
    confidence = float(_value(block_json_or_block, "confidence", 1.0) or 0.0)
    if confidence <= QA_THRESHOLDS["ocr_confidence"]:
        warnings.append("ocr_low_confidence")

    font = _value(block_json_or_block, "font_match")
    if font is not None and float(_value(font, "confidence", 1.0) or 0.0) <= QA_THRESHOLDS["font_confidence"]:
        warnings.append("font_match_low_confidence")

    source = _value(block_json_or_block, "source_match")
    if source:
        score = float(_value(source, "score", _value(source, "confidence", 1.0)) or 0.0)
        if score <= QA_THRESHOLDS["source_confidence"]:
            warnings.append("source_match_low_confidence")
        if _value(source, "kept_ocr", False) and _value(source, "too_long", False):
            warnings.append("source_match_too_long_kept_ocr")

    if layout is not None and bool(_value(layout, "overflow", False)):
        warnings.append("text_overflow")

    kind = str(_value(block_json_or_block, "kind", ""))
    text = str(_value(block_json_or_block, "original_text", "") or "").strip()
    if kind != "sfx_keep" and 0 < len(text) <= QA_THRESHOLDS["sfx_short_text_max_chars"] and confidence <= QA_THRESHOLDS["sfx_review_confidence"]:
        warnings.append("sfx_classification_review")
    return list(dict.fromkeys(warnings))


def residual_text_score(original: Any, clean: Any, mask: Any) -> float:
    original_gray, clean_gray, selected = _validated_images(original, clean, mask)
    if int(selected.sum()) < QA_THRESHOLDS["minimum_mask_pixels"]:
        return 0.0
    original_float = original_gray.astype(np.float32)
    clean_float = clean_gray.astype(np.float32)
    original_background = cv2.GaussianBlur(original_float, (0, 0), 5.0)
    clean_background = cv2.GaussianBlur(clean_float, (0, 0), 5.0)
    original_contrast = original_float - original_background
    clean_contrast = clean_float - clean_background
    original_edges = cv2.Canny(original_gray, 40, 120) > 0
    edge_zone = cv2.dilate(
        original_edges.astype(np.uint8), np.ones((3, 3), np.uint8)
    ) > 0
    glyphs = selected & (np.abs(original_contrast) >= 18.0) & edge_zone
    glyphs = cv2.dilate(
        glyphs.astype(np.uint8), np.ones((3, 3), np.uint8)
    ) > 0
    if int(glyphs.sum()) < QA_THRESHOLDS["minimum_mask_pixels"]:
        return 0.0
    original_values = original_contrast[glyphs]
    clean_values = clean_contrast[glyphs]
    same_polarity = original_values * clean_values > 0
    contrast_keep = np.clip(
        np.abs(clean_values) / np.maximum(np.abs(original_values), 8.0), 0.0, 1.0
    )
    polarity_keep = float(np.mean(same_polarity * contrast_keep))
    clean_edges = cv2.Canny(clean_gray, 40, 120) > 0
    edge_pixels = glyphs & original_edges
    edge_keep = float(np.mean(clean_edges[edge_pixels])) if edge_pixels.any() else 0.0
    return float(np.clip(0.80 * polarity_keep + 0.20 * edge_keep, 0.0, 1.0))


def background_change_score(original: Any, clean: Any, mask: Any) -> float:
    original_gray, clean_gray, selected = _validated_images(original, clean, mask)
    if int(selected.sum()) < QA_THRESHOLDS["minimum_mask_pixels"]:
        return 0.0
    width = QA_THRESHOLDS["mask_boundary_width"]
    kernel = np.ones((width * 2 + 1, width * 2 + 1), np.uint8)
    mask_u8 = selected.astype(np.uint8)
    inner = cv2.erode(mask_u8, kernel) > 0
    inner_band = selected & ~inner
    estimated_clean = cv2.inpaint(
        clean_gray, mask_u8 * 255, 3, cv2.INPAINT_TELEA
    )
    seam_difference = np.abs(
        clean_gray.astype(np.float32) - estimated_clean.astype(np.float32)
    )
    seam_risk = (
        float(np.percentile(seam_difference[inner_band], 75)) / 60.0
        if inner_band.any() else 0.0
    )

    original_edges = cv2.Canny(original_gray, 40, 120) > 0
    clean_edges = cv2.Canny(clean_gray, 40, 120) > 0
    original_gradient = np.hypot(
        cv2.Sobel(original_gray, cv2.CV_32F, 1, 0),
        cv2.Sobel(original_gray, cv2.CV_32F, 0, 1),
    )
    clean_gradient = np.hypot(
        cv2.Sobel(clean_gray, cv2.CV_32F, 1, 0),
        cv2.Sobel(clean_gray, cv2.CV_32F, 0, 1),
    )
    ys, xs = np.where(selected)
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    continuity_losses = []
    strength_losses = []

    for y in range(y1, y2):
        row = slice(max(0, y - 1), min(original_gray.shape[0], y + 2))
        left = original_edges[row, max(0, x1 - width):x1]
        right = original_edges[row, x2:min(original_gray.shape[1], x2 + width)]
        if left.any() and right.any():
            continuity_losses.append(not clean_edges[row, x1:x2].any())
            reference = max(
                float(original_gradient[row, max(0, x1 - width):x1].max()),
                float(original_gradient[row, x2:min(original_gray.shape[1], x2 + width)].max()),
            )
            observed = float(clean_gradient[row, x1:x2].max())
            strength_losses.append(max(0.0, 1.0 - observed / max(reference, 1.0)))

    for x in range(x1, x2):
        column = slice(max(0, x - 1), min(original_gray.shape[1], x + 2))
        top = original_edges[max(0, y1 - width):y1, column]
        bottom = original_edges[y2:min(original_gray.shape[0], y2 + width), column]
        if top.any() and bottom.any():
            continuity_losses.append(not clean_edges[y1:y2, column].any())
            reference = max(
                float(original_gradient[max(0, y1 - width):y1, column].max()),
                float(original_gradient[y2:min(original_gray.shape[0], y2 + width), column].max()),
            )
            observed = float(clean_gradient[y1:y2, column].max())
            strength_losses.append(max(0.0, 1.0 - observed / max(reference, 1.0)))

    continuity_risk = float(np.mean(continuity_losses)) if continuity_losses else 0.0
    blur_risk = float(np.mean(strength_losses)) if strength_losses else 0.0
    return float(np.clip(max(
        0.80 * min(seam_risk, 1.0),
        0.70 * continuity_risk,
        0.70 * blur_risk,
    ), 0.0, 1.0))


def check_page(original: Any, clean: Any, final: Any, mask: Any,
               blocks: Iterable[Any], layouts: Iterable[Any] | None = None) -> dict:
    original_array = _array(original)
    clean_array = _array(clean)
    final_array = _array(final)
    mask_array = _array(mask, True)
    if original_array.shape[:2] != final_array.shape[:2]:
        raise ValueError("original and final image size mismatch")
    residual = residual_text_score(original_array, clean_array, mask_array)
    damage = background_change_score(original_array, clean_array, mask_array)
    block_list = list(blocks)
    layout_list = list(layouts or [])
    block_warnings = [check_block(value, layout_list[index] if index < len(layout_list) else None)
                      for index, value in enumerate(block_list)]
    page_warnings = []
    if residual >= QA_THRESHOLDS["residual_text_risk"]:
        page_warnings.append("possible_text_residual")
    if damage >= QA_THRESHOLDS["background_damage_risk"]:
        page_warnings.append("background_damage_risk")
        for index, value in enumerate(block_list):
            if _value(value, "kind", "") != "sfx_keep":
                block_warnings[index] = list(dict.fromkeys(block_warnings[index] + ["background_damage_risk"]))
    page_warnings.extend(code for codes in block_warnings for code in codes)
    return {
        "warnings": list(dict.fromkeys(page_warnings)),
        "block_warnings": block_warnings,
        "metrics": {"residual_text_score": round(residual, 4),
                    "background_change_score": round(damage, 4)},
        "thresholds": dict(QA_THRESHOLDS),
        "assessment": "Automated risk indicators only; manual review is required for flagged pages.",
    }


def _font(size: int):
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/arial.ttf")):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _entry_value(entry: Any, name: str, default=None):
    return entry.get(name, default) if isinstance(entry, dict) else getattr(entry, name, default)


def make_contact_sheet(entries: Iterable[Any], dest: str | Path,
                       max_pages_per_sheet: int = 12) -> list[Path]:
    if max_pages_per_sheet < 1 or max_pages_per_sheet > 12:
        raise ValueError("max_pages_per_sheet must be between 1 and 12")
    items = list(entries)
    if not items:
        return []
    destination = Path(dest)
    if destination.suffix:
        folder, prefix = destination.parent, destination.stem
    else:
        folder, prefix = destination, "comparison"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    cell_w, cell_h = QA_THRESHOLDS["contact_cell_width"], QA_THRESHOLDS["contact_cell_height"]
    label_font = _font(20)
    for sheet_index in range(0, len(items), max_pages_per_sheet):
        chunk = items[sheet_index:sheet_index + max_pages_per_sheet]
        cols = 2 if len(chunk) > 1 else 1
        rows = math.ceil(len(chunk) / cols)
        sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), "white")
        draw = ImageDraw.Draw(sheet)
        for index, entry in enumerate(chunk):
            x, y = (index % cols) * cell_w, (index // cols) * cell_h
            original = Image.open(_entry_value(entry, "original")).convert("RGB")
            final = Image.open(_entry_value(entry, "final")).convert("RGB")
            original.thumbnail((350, 400)); final.thumbnail((350, 400))
            page = str(_entry_value(entry, "page", ""))
            warnings = ", ".join(_entry_value(entry, "warnings", []) or [])
            label = page + (f" | {warnings}" if warnings else "")
            draw.text((x + 8, y + 6), label[:80], font=label_font, fill="black")
            sheet.paste(original, (x + 8 + (350 - original.width) // 2, y + 42))
            sheet.paste(final, (x + 402 + (350 - final.width) // 2, y + 42))
        path = folder / f"{prefix}_{len(paths) + 1:03d}.png"
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("page_count", str(len(chunk)))
        metadata.add_text(
            "pages",
            json.dumps(
                [str(_entry_value(entry, "page", "")) for entry in chunk],
                ensure_ascii=False,
            ),
        )
        metadata.add_text(
            "warning_codes",
            json.dumps(
                sorted({
                    code
                    for entry in chunk
                    for code in (_entry_value(entry, "warnings", []) or [])
                }),
                ensure_ascii=False,
            ),
        )
        sheet.save(path, pnginfo=metadata)
        paths.append(path)
    return paths


def write_font_match_report(page_name: str, blocks: Iterable[Any], dest_json: str | Path) -> Path:
    payload = {"page": page_name, "blocks": []}
    for value in blocks:
        base = {
            "original": _value(value, "original_text", ""),
            "rewrite": _value(value, "rewrite_text", ""),
            "box": list(_value(value, "box", ())),
            "kind": _value(value, "kind", ""),
            "style": _jsonable(_value(value, "style")),
        }
        if base["kind"] == "sfx_keep":
            base["preserved"] = True
        else:
            font = _value(value, "font_match")
            base["selected"] = None if font is None else {
                "family": _value(font, "family"), "path": str(_value(font, "path", "")),
                "weight": _value(font, "weight"), "score": _value(font, "score"),
                "confidence": _value(font, "confidence"),
                "fallback": bool(_value(font, "fallback_used", False)),
                "warnings": list(_value(font, "warnings", []) or []),
            }
            base["top3"] = _jsonable(list(_value(font, "top_candidates", []) or [])[:3]) if font else []
        payload["blocks"].append(base)
    destination = Path(dest_json)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def aggregate_warning_counts(results: Iterable[dict]) -> dict[str, int]:
    counts = Counter()
    for item in results:
        page_codes = set(item.get("warnings", []) or [])
        block_codes = {
            code
            for block in item.get("blocks", []) or []
            for code in (block.get("warnings", []) or [])
        }
        counts.update(page_codes | block_codes)
    return dict(sorted(counts.items()))


def write_exception_report(results: Iterable[dict], dest_json: str | Path):
    items = [_jsonable(item) for item in results]
    warning_counts = aggregate_warning_counts(items)
    failed_pages = []
    block_count = 0
    for item in items:
        if "page_processing_failed" in item.get("warnings", []):
            failed_pages.append(item.get("page"))
        for value in item.get("blocks", []):
            block_count += 1
    payload = {"summary": {"page_count": len(items), "block_count": block_count,
                           "failed_pages": failed_pages,
                           "warning_counts": warning_counts},
               "results": items}
    json_path = Path(dest_json)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = json_path.with_suffix(".md")
    lines = ["# 异常联系表", "", f"- 异常页数：{len(items)}", f"- 异常块数：{block_count}",
             f"- 失败页数：{len(failed_pages)}", "", "## Warning 统计", ""]
    lines.extend(f"- `{code}`：{count}" for code, count in warning_counts.items())
    lines += ["", "自动检测仅表示风险，需人工复核。", ""]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
