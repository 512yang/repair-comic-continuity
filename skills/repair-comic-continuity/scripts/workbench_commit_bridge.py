"""Validate workbench manifests in one process and return normalized evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from comic_repair.input_order import page_sort_key
from validate_human_issue_annotations import validate_human_issue_annotations
from validate_human_revision_feedback import validate_human_revision_feedback
from validate_human_visual_selection import validate_human_visual_selection


IMAGE_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}


def _load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("workbench commit request is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("workbench commit request must be a mapping")
    return value


def _input_names(input_dir: Path) -> list[str]:
    if not input_dir.is_dir():
        raise ValueError(f"isolated run input directory is missing: {input_dir}")
    pages = sorted(
        (
            path
            for path in input_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
        ),
        key=lambda path: page_sort_key(path.relative_to(input_dir)),
    )
    if not pages:
        raise ValueError("isolated run input directory has no comic pages")
    return [path.relative_to(input_dir).as_posix() for path in pages]


def validate_request(request: dict) -> dict:
    required = {
        "project_root",
        "run_root",
        "selection_document",
        "annotation_document",
        "revision_document",
    }
    if set(request) != required:
        raise ValueError("workbench commit request must contain exact keys")
    project_root = Path(request["project_root"]).resolve()
    run_root = Path(request["run_root"]).resolve()
    if not run_root.is_relative_to(project_root):
        raise ValueError("isolated run root must stay inside the project")
    input_dir = run_root / "输入"
    input_names = _input_names(input_dir)

    result = {}
    selection_document = request["selection_document"]
    if selection_document is not None:
        selection = validate_human_visual_selection(
            selection_document, input_dir, input_names
        )
        result["selection"] = selection_document
        selected_pages = selection["selected_pages"]
    else:
        selected_pages = []
    annotation_document = request["annotation_document"]
    if annotation_document is not None:
        if selection_document is None:
            raise ValueError("annotations require a validated human visual selection")
        validate_human_issue_annotations(
            annotation_document, input_dir, input_names, selected_pages
        )
        result["annotations"] = annotation_document
    revision_document = request["revision_document"]
    if revision_document is not None:
        normalized_revision = validate_human_revision_feedback(
            revision_document, input_dir, input_names, run_root
        )
        result["revision"] = normalized_revision
    if not result:
        raise ValueError("workbench commit contains no evidence document")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_request(_load(args.request))
        print(json.dumps({"status": "validated", "documents": result}, ensure_ascii=False))
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
