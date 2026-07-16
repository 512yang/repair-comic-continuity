"""Compare a blind detection result with a reviewed golden benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline_version import ORCHESTRATION_PIPELINE_ID


VALID_STATUSES = frozenset(
    {"passed_current_review", "confirmed_defect", "evidence_blocked"}
)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"benchmark JSON is unreadable: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("benchmark JSON must be an object")
    return value


def _rows(document: dict[str, Any], label: str) -> dict[str, dict[str, Any]]:
    if document.get("schema_version") != "1.0":
        raise ValueError(f"{label} schema_version mismatch")
    if document.get("pipeline_id") != ORCHESTRATION_PIPELINE_ID:
        raise ValueError(f"{label} pipeline_id mismatch")
    pages = document.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError(f"{label} pages must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for row in pages:
        if not isinstance(row, dict) or not isinstance(row.get("page"), str) or not row["page"]:
            raise ValueError(f"{label} page record is invalid")
        page = row["page"]
        if page in result:
            raise ValueError(f"{label} contains duplicate page: {page}")
        if row.get("status") not in VALID_STATUSES:
            raise ValueError(f"{label} status is invalid: {page}")
        for field in ("visual_issue_codes", "text_issue_codes"):
            codes = row.get(field)
            if (
                not isinstance(codes, list)
                or any(not isinstance(code, str) or not code.strip() for code in codes)
                or len(set(codes)) != len(codes)
            ):
                raise ValueError(f"{label} {field} is invalid: {page}")
        result[page] = row
    return result


def _issue_set(rows: dict[str, dict[str, Any]], only_confirmed: bool) -> set[tuple[str, str, str]]:
    issues: set[tuple[str, str, str]] = set()
    for page, row in rows.items():
        if only_confirmed and row["status"] != "confirmed_defect":
            continue
        for domain, field in (("visual", "visual_issue_codes"), ("text", "text_issue_codes")):
            issues.update((page, domain, code) for code in row[field])
    return issues


def validate_detection_benchmark(golden_path: Path, detected_path: Path) -> dict[str, Any]:
    golden = _rows(_load(golden_path), "golden")
    detected = _rows(_load(detected_path), "detected")
    if set(golden) != set(detected):
        raise ValueError("benchmark page coverage mismatch")

    required = _issue_set(golden, only_confirmed=True)
    found = _issue_set(detected, only_confirmed=False)
    missed = sorted(required - found)
    extras = sorted(found - _issue_set(golden, only_confirmed=False))
    status_mismatches = sorted(
        page for page in golden if golden[page]["status"] != detected[page]["status"]
    )
    clean_false_positives = sorted(
        page
        for page, row in golden.items()
        if row["status"] == "passed_current_review"
        and detected[page]["status"] == "confirmed_defect"
    )
    if missed:
        raise ValueError(f"missed confirmed defect: {missed[0]}")
    if extras or clean_false_positives:
        item = extras[0] if extras else clean_false_positives[0]
        raise ValueError(f"false positive defect: {item}")
    if status_mismatches:
        raise ValueError(f"benchmark status mismatch: {status_mismatches[0]}")

    visual_required = {item for item in required if item[1] == "visual"}
    text_required = {item for item in required if item[1] == "text"}
    return {
        "status": "benchmark_passed",
        "pipeline_id": ORCHESTRATION_PIPELINE_ID,
        "page_count": len(golden),
        "missed_confirmed_defects": [],
        "false_positive_defects": [],
        "visual_recall": 1.0 if not visual_required else len(visual_required & found) / len(visual_required),
        "text_recall": 1.0 if not text_required else len(text_required & found) / len(text_required),
        "correct_page_false_positive_count": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--detected", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_detection_benchmark(args.golden, args.detected)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "benchmark_failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
