"""Shared deterministic filesystem helpers for comic continuity projects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError


INPUT_PAGE_EXTENSIONS = frozenset({".jpg", ".jpeg"})
REFERENCE_IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
# Backward-compatible name for callers that mean general reference images.
IMAGE_EXTENSIONS = REFERENCE_IMAGE_EXTENSIONS
_PAGE_PATTERN = re.compile(r"^(?P<page>\d+)\s*(?:\(\s*(?P<variant>\d+)\s*\))?$")
_DIGIT_PATTERN = re.compile(r"(\d+)")


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    novel: Path
    references: Path
    input_dir: Path
    output_dir: Path


def _normalized_stem(path: Path) -> str:
    return path.stem.translate(str.maketrans({"（": "(", "）": ")"})).strip()


def page_identity(path: Path) -> tuple[Any, ...]:
    """Return an extension-independent identity with normalized parentheses."""
    stem = _normalized_stem(path)
    match = _PAGE_PATTERN.fullmatch(stem)
    if match:
        variant = match.group("variant")
        return ("numeric", int(match.group("page")), int(variant or 0))
    collapsed = re.sub(r"\s+", "", stem).casefold()
    return ("named", collapsed)


def natural_page_key(path: Path) -> tuple[Any, ...]:
    """Sort numeric comic pages and their parenthesized inserts naturally."""
    stem = _normalized_stem(path)
    match = _PAGE_PATTERN.fullmatch(stem)
    if match:
        variant = match.group("variant")
        return (0, int(match.group("page")), int(variant or 0), path.name.casefold())
    tokens = tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in _DIGIT_PATTERN.split(stem)
        if token
    )
    return (1, tokens, path.suffix.casefold(), path.name.casefold())


def verify_readable_image(path: Path) -> None:
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image.load()
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise ValueError(f"unreadable image: {path}") from exc


def sorted_input_pages(input_dir: Path) -> list[Path]:
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise ValueError(f"input directory missing: {input_dir}")
    unexpected_images = sorted(
        path.name
        for path in input_dir.iterdir()
        if path.is_file()
        and path.suffix.casefold() in REFERENCE_IMAGE_EXTENSIONS
        and path.suffix.casefold() not in INPUT_PAGE_EXTENSIONS
    )
    if unexpected_images:
        raise ValueError(
            "unexpected input image extension; comic input pages must use only "
            f".jpg or .jpeg: {', '.join(unexpected_images)}"
        )
    pages = [
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in INPUT_PAGE_EXTENSIONS
    ]
    identities: dict[tuple[Any, ...], Path] = {}
    for page in pages:
        verify_readable_image(page)
        identity = page_identity(page)
        if identity in identities:
            raise ValueError(
                f"duplicate page identity: {identities[identity].name!r} and {page.name!r}"
            )
        identities[identity] = page
    return sorted(pages, key=natural_page_key)


def inventory_reference_images(reference_dir: Path) -> list[dict[str, str]]:
    """Verify common reference images and return deterministic SHA-256 records."""
    reference_dir = Path(reference_dir)
    if not reference_dir.is_dir():
        raise ValueError(f"reference directory missing: {reference_dir}")
    references = sorted(
        (
            path
            for path in reference_dir.iterdir()
            if path.is_file() and path.suffix.casefold() in REFERENCE_IMAGE_EXTENSIONS
        ),
        key=lambda path: path.name.casefold(),
    )
    records: list[dict[str, str]] = []
    for reference in references:
        try:
            verify_readable_image(reference)
        except ValueError as exc:
            raise ValueError(f"unreadable reference image: {reference}") from exc
        records.append({"name": reference.name, "sha256": sha256_file(reference)})
    return records


def discover_project(root: Path) -> ProjectPaths:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"project root missing: {root}")
    novels = sorted(path for path in root.glob("*.txt") if path.is_file())
    if not novels:
        raise ValueError(f"novel txt missing in project root: {root}")
    if len(novels) > 1:
        names = ", ".join(path.name for path in novels)
        raise ValueError(f"multiple novel txt files in project root: {names}")
    required = {
        "人物参考图": root / "人物参考图",
        "输入": root / "输入",
        "输出": root / "输出",
    }
    missing = [name for name, path in required.items() if not path.is_dir()]
    if missing:
        raise ValueError(f"required project directory missing: {', '.join(missing)}")
    return ProjectPaths(
        root=root,
        novel=novels[0],
        references=required["人物参考图"],
        input_dir=required["输入"],
        output_dir=required["输出"],
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_safe_subpath(root: Path, path: Path) -> Path:
    """Resolve *path* and reject targets outside *root*."""
    resolved_root = Path(root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    candidate = candidate.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"unsafe path outside project root: {candidate}") from exc
    return candidate


def is_safe_subpath(root: Path, path: Path) -> bool:
    try:
        ensure_safe_subpath(root, path)
    except ValueError:
        return False
    return True


def atomic_write_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
