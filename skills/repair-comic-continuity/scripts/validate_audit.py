"""Read-only validation for the V4 audit-only phase; never creates candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from entity_timeline import validate_timeline
from pipeline_contracts import canonical_hash
from validate_appearance_matrix import validate_matrix
from validate_human_issue_annotations import validate_human_issue_annotations
from validate_human_visual_selection import validate_human_visual_selection
from validate_source_text_audit import validate_source_text_audit
from project_common import (
    INPUT_PAGE_EXTENSIONS,
    discover_project,
    ensure_safe_subpath,
    sorted_input_pages,
)


FINAL_AUDIT_DECISIONS = frozenset(
    {"unchanged", "text_only", "full_page_redraw", "evidence_blocked"}
)
REPAIR_TASK_TYPES = frozenset(
    {"redraw", "full_page_redraw", "text_repair", "visual", "repair"}
)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"audit evidence is unreadable: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"audit evidence must be an object: {path.name}")
    return value


def _validate_registry(document: dict[str, Any], label: str) -> None:
    recorded = document.get("registry_hash")
    payload = {key: value for key, value in document.items() if key != "registry_hash"}
    if not isinstance(recorded, str) or recorded != canonical_hash(payload):
        raise ValueError(f"{label} registry_hash mismatch")


def _candidate_images(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in INPUT_PAGE_EXTENSIONS
        and any("candidate" in part.casefold() for part in path.relative_to(root).parts[:-1])
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_audit_project(root: Path, evidence_dir: Path) -> dict[str, Any]:
    """Validate audit completeness and prove that generation has not begun."""
    project = discover_project(Path(root))
    evidence = ensure_safe_subpath(project.root, Path(evidence_dir))
    input_names = [
        path.relative_to(project.input_dir).as_posix()
        for path in sorted_input_pages(project.input_dir)
    ]
    candidates = _candidate_images(project.root)
    if candidates:
        raise ValueError(
            f"audit-only run contains candidate image: {candidates[0].relative_to(project.root).as_posix()}"
        )
    outputs = [
        path for path in project.output_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in INPUT_PAGE_EXTENSIONS
    ]
    if outputs:
        raise ValueError(
            f"audit-only run contains final output image: {outputs[0].relative_to(project.root).as_posix()}"
        )

    alignment = _load_json(evidence / "novel_alignment.json")
    clusters = _load_json(evidence / "scene_clusters.json")
    packs = _load_json(evidence / "style_reference_packs.json")
    timeline = _load_json(evidence / "entity_state_timeline.json")
    audits = _load_json(evidence / "page_audit.json")
    queue = _load_json(evidence / "task_queue.json")
    selection_path = evidence / "human_visual_selection.json"
    if selection_path.is_file():
        try:
            selection = validate_human_visual_selection(
                _load_json(selection_path), project.input_dir, input_names
            )
        except ValueError as exc:
            raise ValueError(f"audit-only human visual selection is invalid: {exc}") from exc
        audit_mode = "human_visual_auto_text"
        selected_visual_pages = selection["selected_pages"]
    else:
        audit_mode = "automatic_full_visual_audit"
        selected_visual_pages = input_names
    selected_visual_set = set(selected_visual_pages)

    annotations_path = evidence / "human_issue_annotations.json"
    if annotations_path.is_file():
        if audit_mode != "human_visual_auto_text":
            raise ValueError(
                "audit-only human issue annotations requires human visual selection"
            )
        try:
            issue_annotations = validate_human_issue_annotations(
                _load_json(annotations_path),
                project.input_dir,
                input_names,
                selected_visual_pages,
            )
        except ValueError as exc:
            raise ValueError(
                f"audit-only human issue annotations are invalid: {exc}"
            ) from exc
        annotated_pages = set(issue_annotations["annotated_pages"])
    else:
        issue_annotations = None
        annotated_pages = set()

    matrix_path = evidence / "character_appearance_matrix.json"
    if selected_visual_pages:
        if not matrix_path.is_file():
            raise ValueError("audit-only appearance matrix is missing")
        try:
            appearance_matrix = validate_matrix(_load_json(matrix_path))
        except ValueError as exc:
            raise ValueError(f"audit-only appearance matrix is invalid: {exc}") from exc
        if (
            appearance_matrix.get("status") not in {"confirmed", "defects_confirmed"}
            or appearance_matrix.get("cluster_pages") != selected_visual_pages
        ):
            raise ValueError("audit-only appearance matrix coverage is incomplete")
        appearance_matrix_status = appearance_matrix["status"]
    else:
        appearance_matrix_status = "not_applicable"
    for label, document in (
        ("scene_clusters", clusters),
        ("style_reference_packs", packs),
        ("entity_state_timeline", timeline),
        ("page_audit", audits),
        ("task_queue", queue),
    ):
        _validate_registry(document, label)

    alignment_rows = alignment.get("pages")
    if (
        alignment.get("status") not in {"confirmed", "passed"}
        or not isinstance(alignment_rows, list)
        or [row.get("output_name") for row in alignment_rows if isinstance(row, dict)] != input_names
        or any(row.get("status") != "confirmed" for row in alignment_rows if isinstance(row, dict))
    ):
        raise ValueError("audit-only novel alignment is incomplete")
    if clusters.get("status") != "passed" or not clusters.get("clusters"):
        raise ValueError("audit-only semantic clusters are incomplete")
    if packs.get("status") != "passed" or not packs.get("reference_packs") or not packs.get("stable_pages"):
        raise ValueError("audit-only reference packs are incomplete")
    try:
        validate_timeline(
            {key: value for key, value in timeline.items() if key not in {"status", "registry_hash"}}
        )
    except ValueError as exc:
        raise ValueError(f"audit-only entity timeline is incomplete: {exc}") from exc
    if timeline.get("status") != "passed":
        raise ValueError("audit-only entity timeline is incomplete")

    audit_rows = audits.get("pages")
    if audits.get("status") not in {"audit_passed", "passed"} or not isinstance(audit_rows, list):
        raise ValueError("audit-only page audits are incomplete")
    by_page = {
        row.get("page"): row for row in audit_rows
        if isinstance(row, dict) and isinstance(row.get("page"), str)
    }
    if set(by_page) != set(input_names):
        raise ValueError("audit-only page audit coverage is incomplete")
    second_reviews = 0
    selection_relative = (
        selection_path.relative_to(project.root).as_posix()
        if audit_mode == "human_visual_auto_text"
        else None
    )
    selection_hash = (
        _sha256(selection_path) if audit_mode == "human_visual_auto_text" else None
    )
    annotations_relative = (
        annotations_path.relative_to(project.root).as_posix()
        if issue_annotations is not None
        else None
    )
    annotations_hash = (
        _sha256(annotations_path) if issue_annotations is not None else None
    )
    for page in input_names:
        row = by_page[page]
        records = row.get("audits") if isinstance(row.get("audits"), list) else []
        primary = [
            record for record in records
            if isinstance(record, dict)
            and record.get("perspective") == "continuity"
            and record.get("artifact", {}).get("kind") == "full_resolution_page"
        ]
        if page in selected_visual_set:
            if len(primary) != 1:
                raise ValueError(
                    f"audit-only page requires one full-resolution primary audit: {page}"
                )
        else:
            if primary:
                raise ValueError(
                    f"audit-only unselected page contains full-resolution visual audit: {page}"
                )
            scope_records = [
                record for record in records
                if isinstance(record, dict)
                and record.get("perspective") == "human_visual_scope"
                and record.get("artifact", {}).get("kind")
                == "user_selected_page_list"
            ]
            if len(scope_records) != 1:
                raise ValueError(
                    f"audit-only unselected page requires one human visual scope audit: {page}"
                )
            scope_artifact = scope_records[0]["artifact"]
            if (
                scope_artifact.get("path") != selection_relative
                or scope_artifact.get("sha256") != selection_hash
            ):
                raise ValueError(
                    f"audit-only human visual scope artifact mismatch: {page}"
                )
        annotation_records = [
            record for record in records
            if isinstance(record, dict)
            and record.get("perspective") == "human_issue_annotation"
            and record.get("artifact", {}).get("kind")
            == "user_confirmed_issue_annotations"
        ]
        if page in annotated_pages:
            if len(annotation_records) != 1:
                raise ValueError(
                    f"audit-only page requires one human issue annotation audit: {page}"
                )
            annotation_artifact = annotation_records[0]["artifact"]
            if (
                annotation_artifact.get("path") != annotations_relative
                or annotation_artifact.get("sha256") != annotations_hash
            ):
                raise ValueError(
                    f"audit-only human issue annotation artifact mismatch: {page}"
                )
        elif annotation_records:
            raise ValueError(
                f"audit-only unannotated page contains human issue annotation audit: {page}"
            )
        decision = row.get("decision")
        if page not in selected_visual_set and decision == "full_page_redraw":
            raise ValueError(
                f"audit-only unselected page cannot enter full_page_redraw: {page}"
            )
        if decision == "second_review_required":
            if len(records) < 2:
                raise ValueError(f"audit-only second review is incomplete: {page}")
            second_reviews += 1
        elif decision not in FINAL_AUDIT_DECISIONS:
            raise ValueError(f"audit-only final page classification is missing: {page}")

        source_audit_path = evidence / "source_text_audit" / f"{page}.json"
        if not source_audit_path.is_file():
            raise ValueError(f"audit-only source text audit is missing: {page}")
        try:
            source_audit = validate_source_text_audit(
                _load_json(source_audit_path), project.root
            )
        except ValueError as exc:
            raise ValueError(
                f"audit-only source text audit is invalid for {page}: {exc}"
            ) from exc
        if source_audit.get("status") != "confirmed":
            raise ValueError(f"audit-only source text audit is incomplete: {page}")

    repair_completions = [
        task for task in queue.get("tasks", [])
        if isinstance(task, dict)
        and task.get("state") == "completed"
        and task.get("task_type") in REPAIR_TASK_TYPES
    ]
    if repair_completions:
        raise ValueError("audit-only run contains completed repair task")
    return {
        "status": "audit_passed",
        "page_count": len(input_names),
        "candidate_count": 0,
        "promoted_output_count": 0,
        "second_review_count": second_reviews,
        "audit_mode": audit_mode,
        "selected_visual_page_count": len(selected_visual_pages),
        "human_issue_annotation_count": (
            issue_annotations["annotation_count"] if issue_annotations else 0
        ),
        "human_issue_annotated_page_count": len(annotated_pages),
        "appearance_matrix_status": appearance_matrix_status,
        "source_text_audit_count": len(input_names),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = validate_audit_project(args.root, args.evidence_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "audit_failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
