#!/usr/bin/env python3
"""Fail-closed routing for removing ordinary comic text before typesetting."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Iterable


BACKGROUND_KINDS = frozenset({"flat", "texture", "critical_art"})
LAMA_RESULTS = frozenset({"not_applicable", "pass", "fail", "unsafe", "unavailable"})


def choose_cleanup_route(
    background_kind: str,
    lama_canary: str,
    image2_available: bool,
) -> str:
    if background_kind not in BACKGROUND_KINDS or lama_canary not in LAMA_RESULTS:
        return "evidence_blocked"
    if background_kind == "flat":
        return "deterministic_fill"
    if lama_canary == "pass":
        return "lama"
    if lama_canary in {"fail", "unsafe", "unavailable"} and image2_available:
        return "gpt_image_2"
    return "evidence_blocked"


def build_image2_cleanup_request(
    *,
    page: str,
    source_sha256: str,
    mask_sha256: str,
    width: int,
    height: int,
    block_ids: Iterable[str],
) -> dict:
    _require_sha256(source_sha256, "source_sha256")
    _require_sha256(mask_sha256, "mask_sha256")
    if not page or width <= 0 or height <= 0:
        raise ValueError("page and positive dimensions are required")
    blocks = list(block_ids)
    if not blocks or any(not isinstance(item, str) or not item for item in blocks):
        raise ValueError("at least one nonempty block id is required")
    identity = json.dumps(
        [page, source_sha256, mask_sha256, width, height, blocks],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "schema_version": "comic-text-cleanup-image2-v1",
        "request_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "model": "gpt-image-2",
        "operation": "remove_text_only",
        "page": page,
        "source_sha256": source_sha256,
        "mask_sha256": mask_sha256,
        "dimensions": {"width": width, "height": height},
        "block_ids": blocks,
        "allow_typesetting": False,
        "preserve_outside_mask": True,
        "prompt": (
            "仅清除蒙版内的原有文字并重建被文字遮挡的背景。不得添加任何文字、"
            "符号、对话框或拟声字；不得改变蒙版外的人物、道具、场景、线条、颜色、"
            "构图和画风；保持原尺寸。"
        ),
    }


def _require_sha256(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be lowercase sha256")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--background-kind", required=True, choices=sorted(BACKGROUND_KINDS))
    parser.add_argument("--lama-canary", required=True, choices=sorted(LAMA_RESULTS))
    parser.add_argument("--image2-available", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "route": choose_cleanup_route(
                    args.background_kind,
                    args.lama_canary,
                    args.image2_available,
                )
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
