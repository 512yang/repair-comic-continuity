"""Validate the hash-bound page list for human-selected visual repair scope."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_KEYS = frozenset(
    {
        "version",
        "status",
        "mode",
        "selected_pages",
        "visual_policy",
        "text_policy",
        "output_policy",
    }
)
SELECTION_KEYS = frozenset({"path", "sha256"})
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("selected page path must be a safe relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("selected page path must be a safe relative path")
    return value


def validate_human_visual_selection(
    document: dict[str, Any], input_dir: Path, input_names: list[str]
) -> dict[str, Any]:
    """Return normalized selected paths after validating the exact mode contract."""
    if not isinstance(document, dict) or set(document) != MANIFEST_KEYS:
        raise ValueError("human visual selection must contain exact keys")
    if (
        document.get("version") != 1
        or document.get("status") != "confirmed"
        or document.get("mode") != "human_visual_auto_text"
        or document.get("visual_policy") != "selected_pages_only"
        or document.get("text_policy")
        != "all_input_pages_page_reset_preserve_style"
        or document.get("output_policy") != "exact_input_bijection"
    ):
        raise ValueError("human visual selection contract mismatch")

    rows = document.get("selected_pages")
    if not isinstance(rows, list):
        raise ValueError("human visual selected_pages must be a list")
    input_positions = {name: index for index, name in enumerate(input_names)}
    selected: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != SELECTION_KEYS:
            raise ValueError(f"selected_pages[{index}] must contain exact keys")
        relative = _safe_relative_path(row.get("path"))
        if relative not in input_positions:
            raise ValueError(f"selected page is an unknown input page: {relative}")
        if relative in seen:
            raise ValueError(f"selected page is duplicate: {relative}")
        digest = row.get("sha256")
        if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise ValueError(f"selected page sha256 is invalid: {relative}")
        source = Path(input_dir) / Path(*PurePosixPath(relative).parts)
        if not source.is_file() or _sha256(source) != digest:
            raise ValueError(f"selected page sha256 mismatch: {relative}")
        seen.add(relative)
        selected.append(relative)

    positions = [input_positions[name] for name in selected]
    if positions != sorted(positions):
        raise ValueError("selected pages must follow natural input order")
    return {
        "mode": "human_visual_auto_text",
        "selected_pages": selected,
        "selected_page_count": len(selected),
        "visual_policy": "selected_pages_only",
        "text_policy": "all_input_pages_page_reset_preserve_style",
        "output_policy": "exact_input_bijection",
    }
