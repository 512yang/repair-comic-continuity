"""Inventory a comic project using Python 3.11 + Pillow without changing media."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

from project_common import (
    discover_project,
    inventory_reference_images,
    sha256_file,
    sorted_input_pages,
)
from source_text import decode_source_text


def inventory_project(root: Path, expected_count: int | None = None) -> dict:
    project = discover_project(root)
    pages = sorted_input_pages(project.input_dir)
    references = inventory_reference_images(project.references)
    novel = decode_source_text(project.novel)
    if expected_count is not None and len(pages) != expected_count:
        raise ValueError(f"input page count mismatch: expected {expected_count}, found {len(pages)}")
    return {
        "status": "inventoried",
        "root": str(project.root),
        "novel": {
            "path": str(project.novel),
            "raw_sha256": novel.raw_sha256,
            "decoded_sha256": novel.decoded_sha256,
            "encoding": novel.encoding,
            # Compatibility alias during the staged manifest migration.
            "sha256": novel.raw_sha256,
        },
        "references": str(project.references),
        "reference_files": references,
        "input_dir": str(project.input_dir),
        "output_dir": str(project.output_dir),
        "expected_count": expected_count,
        "input_count": len(pages),
        "pages": [
            _inventory_page(project.input_dir, page, index)
            for index, page in enumerate(pages, start=1)
        ],
    }


def _inventory_page(input_dir: Path, page: Path, index: int) -> dict:
    relative_path = page.relative_to(input_dir).as_posix()
    with Image.open(page) as image:
        width, height = image.size
    return {
        "index": index,
        "input_name": relative_path,
        "relative_path": relative_path,
        "extension": page.suffix,
        "width": width,
        "height": height,
        "input_sha256": sha256_file(page),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Project root")
    parser.add_argument("--expected-count", type=int, help="Required input page count")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        result = inventory_project(args.root, args.expected_count)
    except (OSError, ValueError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}")
        return 1
    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    else:
        print(f"inventoried {result['input_count']} input pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
