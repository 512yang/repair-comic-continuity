#!/usr/bin/env python3
"""Validate complete, hash-bound source-page ordinary-text audit evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from PIL import Image, ImageChops


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}\Z")
TARGET_GLYPHS = frozenset({"强", "遇"})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _exact(mapping: object, keys: set[str], field: str) -> Mapping:
    if not isinstance(mapping, Mapping) or set(mapping) != keys:
        raise ValueError(f"{field} must contain exactly {sorted(keys)}")
    return mapping


def _safe_file(root: Path, artifact: object, field: str) -> Path:
    row = _exact(artifact, {"path", "sha256"}, field)
    raw = _text(row.get("path"), f"{field}.path").replace("\\", "/")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise ValueError(f"{field}.path must be a safe relative path")
    digest = row.get("sha256")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
        raise ValueError(f"{field}.sha256 must be lowercase sha256")
    root = root.resolve()
    path = (root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{field}.path escapes project root") from exc
    if not path.is_file():
        raise ValueError(f"{field}.path is missing")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        raise ValueError(f"{field} hash does not match current file")
    return path


def _read_novel(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("novel text encoding is unreadable")


def _bbox(value: object, field: str, size: tuple[int, int]) -> tuple[int, int, int, int]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or len(value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ValueError(f"{field} must be four integer coordinates")
    x1, y1, x2, y2 = value
    if not (0 <= x1 < x2 <= size[0] and 0 <= y1 < y2 <= size[1]):
        raise ValueError(f"{field} escapes current page")
    return x1, y1, x2, y2


def _block_refs(
    value: object, field: str, size: tuple[int, int]
) -> list[tuple[str, tuple[int, int, int, int], str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    rows: list[tuple[str, tuple[int, int, int, int], str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        row = _exact(
            item, {"block_id", "bbox", "transcription"}, f"{field}[{index}]"
        )
        block_id = _text(row.get("block_id"), f"{field}[{index}].block_id")
        if block_id in seen:
            raise ValueError(f"{field} contains duplicate block_id")
        seen.add(block_id)
        rows.append(
            (
                block_id,
                _bbox(row.get("bbox"), f"{field}[{index}].bbox", size),
                _text(row.get("transcription"), f"{field}[{index}].transcription"),
            )
        )
    return rows


def _repeat_signatures(text: str) -> set[tuple[str, int, int]]:
    candidates: set[tuple[str, int, int]] = set()
    for start in range(len(text)):
        matches: list[tuple[str, int, int]] = []
        for width in range(1, 7):
            end = start + width * 2
            if end > len(text):
                break
            chunk = text[start : start + width]
            if chunk == text[start + width : end] and all("\u3400" <= char <= "\u9fff" for char in chunk):
                matches.append((chunk, start, end))
        if matches:
            candidates.add(max(matches, key=lambda item: len(item[0])))
    return candidates


def _repeat_reviews(value: object, transcription: str, field: str) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    actual: set[tuple[str, int, int]] = set()
    blocked = False
    for index, item in enumerate(value):
        row = _exact(item, {"text", "start", "end", "decision", "reason"}, f"{field}[{index}]")
        chunk = _text(row.get("text"), f"{field}[{index}].text")
        start, end = row.get("start"), row.get("end")
        if any(isinstance(number, bool) or not isinstance(number, int) for number in (start, end)):
            raise ValueError(f"{field} offsets must be integers")
        if not (0 <= start < end <= len(transcription)) or transcription[start:end] != chunk * 2:
            raise ValueError(f"{field} offsets do not bind the repeated source text")
        decision = row.get("decision")
        if decision not in {"intentional", "defect", "evidence_blocked"}:
            raise ValueError(f"{field} decision is invalid")
        _text(row.get("reason"), f"{field}[{index}].reason")
        signature = (chunk, start, end)
        if signature in actual:
            raise ValueError(f"{field} contains duplicate review")
        actual.add(signature)
        blocked |= decision == "evidence_blocked"
    if actual != _repeat_signatures(transcription):
        raise ValueError(f"{field} must exactly review every adjacent Chinese repeat")
    return blocked


def _glyph_checks(value: object, transcription: str, field: str) -> bool:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    expected = {
        (character, offset)
        for offset, character in enumerate(transcription)
        if character in TARGET_GLYPHS
    }
    actual: set[tuple[str, int]] = set()
    blocked = False
    for index, item in enumerate(value):
        row = _exact(
            item,
            {"character", "offset", "decision", "reason", "ocr_only"},
            f"{field}[{index}]",
        )
        character = row.get("character")
        offset = row.get("offset")
        if character not in TARGET_GLYPHS or isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError(f"{field} character/offset is invalid")
        if row.get("ocr_only") is not False:
            raise ValueError(f"{field} ocr_only must be false")
        decision = row.get("decision")
        if decision not in {"passed", "defect", "evidence_blocked"}:
            raise ValueError(f"{field} decision is invalid")
        _text(row.get("reason"), f"{field}[{index}].reason")
        signature = (character, offset)
        if signature in actual:
            raise ValueError(f"{field} contains duplicate occurrence")
        actual.add(signature)
        blocked |= decision == "evidence_blocked"
    if actual != expected:
        raise ValueError(f"{field} must exactly cover every 强 and 遇 occurrence")
    return blocked


def validate_source_text_audit(value: object, root: Path) -> dict:
    document = _exact(
        value,
        {
            "version",
            "status",
            "page",
            "novel",
            "machine_detector_id",
            "visual_reviewer_id",
            "source_has_ordinary_text",
            "textless_review",
            "machine_blocks",
            "visual_blocks",
            "blocks",
        },
        "source text audit",
    )
    if document.get("version") != 1:
        raise ValueError("source text audit version must be 1")
    status = document.get("status")
    if status not in {"confirmed", "evidence_blocked"}:
        raise ValueError("source text audit status is invalid")
    machine_id = document.get("machine_detector_id")
    reviewer_id = document.get("visual_reviewer_id")
    if not isinstance(machine_id, str) or ID.fullmatch(machine_id) is None:
        raise ValueError("machine_detector_id is invalid")
    if not isinstance(reviewer_id, str) or ID.fullmatch(reviewer_id) is None:
        raise ValueError("visual_reviewer_id is invalid")
    if machine_id == reviewer_id:
        raise ValueError("visual reviewer must be independent from machine detector")
    source_has_text = document.get("source_has_ordinary_text")
    if not isinstance(source_has_text, bool):
        raise ValueError("source_has_ordinary_text must be boolean")
    textless_review = document.get("textless_review")
    if source_has_text:
        if textless_review is not None:
            raise ValueError("textless_review must be null for a text-bearing page")
    else:
        review = _exact(
            textless_review,
            {"reviewer_id", "full_resolution", "reason"},
            "textless_review",
        )
        textless_reviewer = review.get("reviewer_id")
        if not isinstance(textless_reviewer, str) or ID.fullmatch(textless_reviewer) is None:
            raise ValueError("textless_review reviewer_id is invalid")
        if textless_reviewer in {machine_id, reviewer_id}:
            raise ValueError("textless_review must use a third independent reviewer")
        if review.get("full_resolution") is not True:
            raise ValueError("textless_review full_resolution must be true")
        _text(review.get("reason"), "textless_review.reason")

    page = _safe_file(root, document.get("page"), "page")
    novel_path = _safe_file(root, document.get("novel"), "novel")
    novel_text = _read_novel(novel_path)
    with Image.open(page) as source:
        source.load()
        page_image = source.convert("RGBA")
    size = page_image.size
    machine = _block_refs(document.get("machine_blocks"), "machine_blocks", size)
    visual = _block_refs(document.get("visual_blocks"), "visual_blocks", size)
    if visual != machine:
        raise ValueError("visual_blocks must exactly match machine_blocks in reading order")

    raw_blocks = document.get("blocks")
    if isinstance(raw_blocks, (str, bytes)) or not isinstance(raw_blocks, Sequence):
        raise ValueError("blocks must be a list")
    normalized_blocks = []
    coverage: list[tuple[str, tuple[int, int, int, int], str]] = []
    blocked = False
    for index, item in enumerate(raw_blocks):
        row = _exact(
            item,
            {
                "block_id",
                "bbox",
                "crop",
                "transcription",
                "decision",
                "reason",
                "novel_alignment",
                "repeat_reviews",
                "glyph_checks",
            },
            f"blocks[{index}]",
        )
        block_id = _text(row.get("block_id"), f"blocks[{index}].block_id")
        box = _bbox(row.get("bbox"), f"blocks[{index}].bbox", size)
        crop_path = _safe_file(root, row.get("crop"), f"blocks[{index}].crop")
        with Image.open(crop_path) as crop:
            crop.load()
            crop_image = crop.convert("RGBA")
        expected_crop = page_image.crop(box)
        difference = ImageChops.difference(
            crop_image.convert("RGB"), expected_crop.convert("RGB")
        )
        if crop_image.size != expected_crop.size or difference.getbbox() is not None:
            raise ValueError(f"blocks[{index}] crop pixels do not match current page bbox")
        transcription = _text(row.get("transcription"), f"blocks[{index}].transcription")
        coverage.append((block_id, box, transcription))
        decision = row.get("decision")
        if decision not in {"passed", "defect", "evidence_blocked"}:
            raise ValueError(f"blocks[{index}].decision is invalid")
        _text(row.get("reason"), f"blocks[{index}].reason")
        blocked |= decision == "evidence_blocked"
        alignment = _exact(
            row.get("novel_alignment"),
            {"status", "start", "end", "excerpt", "semantic_decision", "reason"},
            f"blocks[{index}].novel_alignment",
        )
        alignment_status = alignment.get("status")
        if alignment_status not in {"confirmed", "evidence_blocked"}:
            raise ValueError(f"blocks[{index}].novel_alignment status is invalid")
        start, end = alignment.get("start"), alignment.get("end")
        if any(isinstance(number, bool) or not isinstance(number, int) for number in (start, end)):
            raise ValueError(f"blocks[{index}].novel_alignment offsets must be integers")
        excerpt = _text(alignment.get("excerpt"), f"blocks[{index}].novel_alignment.excerpt")
        if not (0 <= start < end <= len(novel_text)) or novel_text[start:end] != excerpt:
            raise ValueError(f"blocks[{index}].novel_alignment does not match exact novel offsets")
        semantic_decision = alignment.get("semantic_decision")
        if semantic_decision not in {"faithful", "defect", "evidence_blocked"}:
            raise ValueError(f"blocks[{index}].novel_alignment semantic_decision is invalid")
        _text(alignment.get("reason"), f"blocks[{index}].novel_alignment.reason")
        blocked |= alignment_status == "evidence_blocked"
        blocked |= semantic_decision == "evidence_blocked"
        blocked |= _repeat_reviews(row.get("repeat_reviews"), transcription, f"blocks[{index}].repeat_reviews")
        blocked |= _glyph_checks(row.get("glyph_checks"), transcription, f"blocks[{index}].glyph_checks")
        normalized_blocks.append(copy.deepcopy(dict(row)))
    if coverage != machine:
        raise ValueError("blocks must exactly cover machine_blocks and visual_blocks")
    if source_has_text != bool(normalized_blocks):
        raise ValueError(
            "source_has_ordinary_text must exactly match the audited block inventory"
        )
    if blocked and status != "evidence_blocked":
        raise ValueError("source text audit status must be evidence_blocked")
    if not blocked and status != "confirmed":
        raise ValueError("source text audit status must be confirmed")
    result = copy.deepcopy(dict(document))
    result["blocks"] = normalized_blocks
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.audit.read_text(encoding="utf-8-sig"))
    result = validate_source_text_audit(payload, args.root)
    print(json.dumps({"status": result["status"], "block_count": len(result["blocks"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
