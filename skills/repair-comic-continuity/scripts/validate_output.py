"""Validate comic output and evidence using Python 3.11 + Pillow."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

from build_output_manifest import (
    BATCH_QA_CHECKS,
    CONTINUITY_CATEGORIES,
    CONTINUITY_LOCK_FIELDS,
    CONTINUITY_SOURCE_TYPES,
    EVIDENCE_FILES,
    FINAL_REPORT_KEYS,
    PAGE_QA_CHECKS,
    STYLE_REVIEW_CHECKS,
    VISUAL_EDIT_MODES,
    VISUAL_ISSUE_EXTENTS,
    VISUAL_REVIEW_CHECKS,
    expected_batch_count,
    expected_batch_sizes,
)

from project_common import (
    INPUT_PAGE_EXTENSIONS,
    discover_project,
    ensure_safe_subpath,
    sha256_file,
    sorted_input_pages,
    verify_readable_image,
)
from audit_evidence import validate_review_log
from entity_timeline import validate_timeline
from evidence_integrity import validate_v4_integrity
from failure_learning import validate_failure_store
from pipeline_contracts import canonical_hash, normalize_page_id, normalize_relative_image_path
from pipeline_version import EVIDENCE_PIPELINE_ID, EVIDENCE_SCHEMA_VERSION
from task_queue import load_queue
from scene_clusters import build_reference_pack, validate_reference_pack


SCENE_CLUSTER_PIPELINE_MODE = "scene_cluster_v1"


def _v4_report_values(path: Path, errors: list[str]) -> dict[str, str]:
    if not path.is_file():
        errors.append("evidence file missing: FINAL_QA_REPORT.md")
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"invalid FINAL_QA_REPORT.md: {exc}")
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def _validate_v4_project(
    project: Any,
    evidence: Path,
    input_pages: list[Path],
    expected_count: int,
    run: dict[str, Any],
) -> dict[str, Any]:
    """Assemble final integrity facts from V4 registries; never rebuild evidence."""
    errors: list[str] = []
    counts = {
        "expected": expected_count,
        "input": len(input_pages),
        "output": 0,
        "manifest": len(run.get("pages", [])) if isinstance(run.get("pages"), list) else 0,
    }
    input_names = [page.relative_to(project.input_dir).as_posix() for page in input_pages]
    output_files = sorted(
        path for path in project.output_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in INPUT_PAGE_EXTENSIONS
    )
    discovered_output_names = [
        path.relative_to(project.output_dir).as_posix() for path in output_files
    ]
    discovered_output_set = set(discovered_output_names)
    output_names = [name for name in input_names if name in discovered_output_set]
    output_names.extend(
        sorted(name for name in discovered_output_names if name not in set(input_names))
    )
    counts["output"] = len(output_files)
    if len(output_files) != expected_count:
        errors.append(
            f"output count mismatch: expected {expected_count}, found {len(output_files)}"
        )
    non_image_files = [path for path in project.output_dir.rglob("*") if path.is_file() and path not in output_files]
    if non_image_files:
        errors.append("OUTPUT_NAME_SET_MISMATCH")
    for output in output_files:
        try:
            verify_readable_image(output)
        except ValueError as exc:
            errors.append(str(exc))

    documents: dict[str, dict[str, Any] | None] = {"comic_run_manifest": run}
    filenames = {
        "continuity_bible": "continuity_bible.json",
        "novel_alignment": "novel_alignment.json",
        "repair_log": "repair_log.json",
        "scene_clusters": "scene_clusters.json",
        "style_reference_packs": "style_reference_packs.json",
        "task_queue": "task_queue.json",
        "failure_learning": "failure_learning.json",
        "scene_cluster_qa": "scene_cluster_qa.json",
        "entity_state_timeline": "entity_state_timeline.json",
        "page_audit": "page_audit.json",
        "text_geometry": "text_geometry.json",
        "regression_summary": "regression_summary.json",
    }
    for label, filename in filenames.items():
        documents[label] = _load_json(evidence / filename, errors)

    for label in (
        "scene_clusters", "style_reference_packs", "task_queue", "failure_learning",
        "scene_cluster_qa", "entity_state_timeline", "page_audit", "text_geometry",
        "regression_summary",
    ):
        _validate_registry_hash(documents[label], label, errors)

    alignment = documents["novel_alignment"] or {}
    repair = documents["repair_log"] or {}
    bible = documents["continuity_bible"] or {}
    clusters_doc = documents["scene_clusters"] or {}
    packs_doc = documents["style_reference_packs"] or {}
    queue = documents["task_queue"] or {}
    cluster_qa = documents["scene_cluster_qa"] or {}
    timeline = documents["entity_state_timeline"] or {}
    audits = documents["page_audit"] or {}
    regression = documents["regression_summary"] or {}

    try:
        queue = load_queue(evidence / "task_queue.json")
    except ValueError as exc:
        errors.append(str(exc))
    try:
        validate_failure_store(documents["failure_learning"] or {})
    except ValueError as exc:
        errors.append(str(exc))

    if run.get("schema_version") != EVIDENCE_SCHEMA_VERSION or run.get("pipeline_mode") != EVIDENCE_PIPELINE_ID:
        errors.append("FINAL_REPORT_STATE_MISMATCH")
    if repair.get("schema_version") != EVIDENCE_SCHEMA_VERSION or repair.get("pipeline_mode") != EVIDENCE_PIPELINE_ID:
        errors.append("FINAL_REPORT_STATE_MISMATCH")
    if run.get("evidence_files") != list(EVIDENCE_FILES) or repair.get("evidence_files") != list(EVIDENCE_FILES):
        errors.append("FINAL_REPORT_STATE_MISMATCH")

    run_pages = run.get("pages") if isinstance(run.get("pages"), list) else []
    repair_pages = repair.get("pages") if isinstance(repair.get("pages"), list) else []
    alignments = alignment.get("pages") if isinstance(alignment.get("pages"), list) else []
    audit_pages = audits.get("pages") if isinstance(audits.get("pages"), list) else []
    task_map = {
        task.get("task_id"): task
        for task in queue.get("tasks", [])
        if isinstance(task, dict) and isinstance(task.get("task_id"), str)
    }
    audit_map = {
        row.get("page"): row
        for row in audit_pages
        if isinstance(row, dict) and isinstance(row.get("page"), str)
    }
    output_hashes = {
        path.relative_to(project.output_dir).as_posix(): sha256_file(path)
        for path in output_files
    }
    page_rows: list[dict[str, Any]] = []
    full_reviews: list[dict[str, Any]] = []
    for index, name in enumerate(input_names):
        run_row = run_pages[index] if index < len(run_pages) and isinstance(run_pages[index], dict) else {}
        repair_row = repair_pages[index] if index < len(repair_pages) and isinstance(repair_pages[index], dict) else {}
        audit_row = audit_map.get(name, {})
        task = task_map.get(repair_row.get("task_id"), {})
        audit_records = audit_row.get("audits") if isinstance(audit_row.get("audits"), list) else []
        review_times = [_parse_zoned_datetime(row.get("reviewed_at")) for row in audit_records if isinstance(row, dict)]
        review_times = [value for value in review_times if value is not None]
        reviewed_at = max(review_times).isoformat() if review_times else audit_row.get("reviewed_at")
        artifact_ok = True
        artifact_hash = None
        for record in audit_records:
            artifact = record.get("artifact", {}) if isinstance(record, dict) else {}
            artifact_path = artifact.get("path")
            digest = artifact.get("sha256")
            try:
                resolved = project.root / normalize_relative_image_path(artifact_path)
                current_hash = sha256_file(resolved)
            except (OSError, ValueError, TypeError):
                artifact_ok = False
            else:
                artifact_ok = artifact_ok and current_hash == digest
                artifact_hash = digest
        if len(audit_records) != 2:
            artifact_ok = False
        full_reviews.append(
            {
                "page": name,
                "full_size": len(audit_records) == 2 and all(
                    isinstance(row, dict) and row.get("artifact", {}).get("kind") == "full_resolution_page"
                    for row in audit_records
                ),
                "artifact_exists": artifact_ok,
                "artifact_hash": artifact_hash,
            }
        )
        candidate_created = task.get("completed_at") or task.get("timestamps", {}).get("completed_at")
        page_rows.append(
            {
                "page": name,
                "cluster_id": repair_row.get("cluster_id"),
                "page_class": repair_row.get("page_class"),
                "generator": repair_row.get("generated_by"),
                "reviewer": repair_row.get("reviewed_by"),
                "candidate_created_at": candidate_created,
                "reviewed_at": reviewed_at,
                "audit_status": "resolved" if audit_row.get("decision") == repair_row.get("page_class") else audit_row.get("status"),
                "preflight_status": audit_row.get("preflight_status"),
                "task_status": task.get("state"),
            }
        )
        if (
            run_row.get("input_name") != name
            or run_row.get("output_name") != name
            or repair_row.get("output_name") != name
            or run_row.get("input_sha256") != sha256_file(input_pages[index])
            or run_row.get("output_sha256") != output_hashes.get(name)
        ):
            errors.append("OUTPUT_NAME_SET_MISMATCH")

    required_cast = sorted(
        {
            character
            for row in alignments
            if isinstance(row, dict) and isinstance(row.get("involved_characters"), list)
            for character in row["involved_characters"]
            if isinstance(character, str) and character
        }
    )
    reference_packs = []
    for pack in packs_doc.get("reference_packs", []):
        if not isinstance(pack, dict):
            continue
        cast = sorted(
            {
                row.get("subject")
                for row in pack.get("references", [])
                if isinstance(row, dict)
                and row.get("role") in {"identity_only", "comic_style_anchor"}
                and isinstance(row.get("subject"), str)
            }
        )
        reference_packs.append({"pack_id": pack.get("reference_pack_id"), "cast": cast})

    try:
        events = validate_review_log(evidence / "review_events.jsonl")
    except ValueError as exc:
        errors.append(str(exc))
        events = []
    try:
        validate_timeline(
            {
                key: value
                for key, value in timeline.items()
                if key not in {"status", "registry_hash"}
            }
        )
        timeline_supported = True
    except ValueError as exc:
        errors.append(str(exc))
        timeline_supported = False

    report = _v4_report_values(evidence / "FINAL_QA_REPORT.md", errors)
    statuses = {
        label: (document or {}).get("status")
        for label, document in documents.items()
        if label in {
            "scene_clusters", "style_reference_packs", "task_queue", "failure_learning",
            "scene_cluster_qa", "entity_state_timeline", "page_audit", "text_geometry",
            "regression_summary",
        }
    }
    summary = {
        "schema_version": run.get("schema_version"),
        "pipeline_mode": run.get("pipeline_mode"),
        "input_names": input_names,
        "output_names": output_names,
        "alignments": alignments,
        "full_size_reviews": full_reviews,
        "required_cast": required_cast,
        "reference_packs": reference_packs,
        "stable_pages": packs_doc.get("stable_pages"),
        "timeline_supported": timeline_supported,
        "events": events,
        "pages": page_rows,
        "clusters": cluster_qa.get("clusters"),
        "registry_statuses": statuses,
        "final_status": report.get("status"),
        "final_reviewed_at": report.get("reviewed_at"),
        "unresolved_issues": regression.get("unresolved_issues"),
    }
    errors.extend(code for code in validate_v4_integrity(summary) if code not in errors)
    if any((document or {}).get("status") != "passed" for document in (run, repair, alignment, bible)):
        if "FINAL_REPORT_STATE_MISMATCH" not in errors:
            errors.append("FINAL_REPORT_STATE_MISMATCH")
    return {"ok": not errors, "errors": errors, "counts": counts}


def _load_json(path: Path, errors: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        errors.append(f"evidence file missing: {path.name}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid evidence JSON {path.name}: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"invalid evidence JSON object: {path.name}")
        return None
    return value


def _indexed_pages(document: dict[str, Any] | None, name: str, errors: list[str]) -> list[dict]:
    if document is None:
        return []
    pages = document.get("pages")
    if not isinstance(pages, list):
        errors.append(f"{name} pages must be a list")
        return []
    return [page for page in pages if isinstance(page, dict)]


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_hashed_project_file(
    root: Path,
    row: dict[str, Any],
    path_field: str,
    hash_field: str,
    label: str,
    page_index: int,
    errors: list[str],
) -> None:
    raw_path = row.get(path_field)
    recorded_hash = row.get(hash_field)
    if not _nonempty_string(raw_path):
        errors.append(f"{label} path missing at page {page_index}")
        return
    try:
        path = ensure_safe_subpath(root, Path(raw_path))
    except (OSError, ValueError) as exc:
        errors.append(f"{label} path unsafe at page {page_index}: {exc}")
        return
    if not path.is_file():
        errors.append(f"{label} file missing at page {page_index}: {path}")
        return
    if not _nonempty_string(recorded_hash) or recorded_hash != sha256_file(path):
        errors.append(f"{label} hash mismatch at page {page_index}")


def _validate_external_candidate(row: dict[str, Any], page_index: int, errors: list[str]) -> None:
    used = row.get("used_external_text_resource")
    candidate = row.get("external_candidate")
    if used is False:
        if candidate is not None:
            errors.append(f"external_candidate must be null when unused at page {page_index}")
        return
    if used is not True or not isinstance(candidate, dict):
        errors.append(f"external_candidate contract missing at page {page_index}")
        return
    required = {"run_dir", "source_path", "source_sha256", "imported_at", "imported_by", "status"}
    if set(candidate) != required:
        errors.append(f"external_candidate fields invalid at page {page_index}")
        return
    try:
        run_dir = Path(candidate["run_dir"]).resolve()
        allowed_parent = Path(r"D:\漫画文字修复\输出").resolve()
    except (TypeError, OSError):
        errors.append(f"external_candidate run_dir invalid at page {page_index}")
        return
    if (
        run_dir.parent != allowed_parent
        or re.fullmatch(r"repair-comic-continuity_\d{8}_\d{6}", run_dir.name) is None
    ):
        errors.append(f"external_candidate run_dir invalid at page {page_index}")
        return
    try:
        source = ensure_safe_subpath(run_dir / "final", Path(candidate["source_path"]))
    except (TypeError, OSError, ValueError) as exc:
        errors.append(f"external_candidate source_path invalid at page {page_index}: {exc}")
        return
    if not source.is_file():
        errors.append(f"external_candidate source missing at page {page_index}")
        return
    try:
        verify_readable_image(source)
    except ValueError as exc:
        errors.append(f"external_candidate source unreadable at page {page_index}: {exc}")
        return
    source_hash = sha256_file(source)
    if (
        candidate.get("source_sha256") != source_hash
        or row.get("candidate_sha256") != source_hash
    ):
        errors.append(f"external_candidate hash chain mismatch at page {page_index}")
    if candidate.get("status") != "supervised_import":
        errors.append(f"external_candidate status invalid at page {page_index}")
    if not _nonempty_string(candidate.get("imported_by")):
        errors.append(f"external_candidate imported_by missing at page {page_index}")
    if _parse_zoned_datetime(candidate.get("imported_at")) is None:
        errors.append(f"external_candidate imported_at invalid at page {page_index}")


def _validate_checks(
    review: dict[str, Any],
    required: tuple[str, ...],
    label: str,
    page_index: int,
    errors: list[str],
) -> None:
    checks = review.get("checks")
    if not isinstance(checks, dict) or any(checks.get(name) is not True for name in required):
        errors.append(f"{label} checks incomplete at page {page_index}")


def _string_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(_nonempty_string(item) for item in value)
    )


def _parse_zoned_datetime(value: Any) -> datetime | None:
    if not _nonempty_string(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_registry_hash(
    document: dict[str, Any] | None,
    label: str,
    errors: list[str],
) -> bool:
    if document is None:
        return False
    recorded = document.get("registry_hash")
    payload = {key: value for key, value in document.items() if key != "registry_hash"}
    if not _nonempty_string(recorded) or recorded != canonical_hash(payload):
        errors.append(f"{label} registry_hash mismatch")
        return False
    return True


def _valid_repair_policy(row: dict[str, Any]) -> bool:
    action = row.get("action")
    scope = row.get("repair_scope")
    had_text = row.get("had_ordinary_text")
    recognition = row.get("text_recognition")
    reset_mode = row.get("text_reset_mode")
    reason = row.get("text_reset_reason")
    if not isinstance(had_text, bool):
        return False
    if action == "unchanged_copy":
        return (
            scope == "unchanged" and recognition == "not_applicable"
            and reset_mode == "none" and reason == "unchanged_page"
        )
    if action != "edited":
        return False
    if scope == "visual":
        if had_text:
            return (
                recognition in {"reliable", "unreliable"}
                and reset_mode == "page"
                and reason == "visual_repair_requires_page_reset"
            )
        return recognition == "not_applicable" and reset_mode == "none" and reason == "no_ordinary_text"
    if scope == "text_only":
        if not had_text:
            return False
        return (recognition == "reliable" and reset_mode == "block" and reason == "reliable_block_match") or (
            recognition == "unreliable" and reset_mode == "page" and reason == "unreliable_text_page_reset"
        )
    return False


def _validate_style_contract(
    root: Path,
    row: dict[str, Any],
    page_index: int,
    output_names: list[str],
    input_pages: list[Path],
    errors: list[str],
) -> None:
    scope = row.get("repair_scope")
    mode = row.get("visual_edit_mode")
    extent = row.get("visual_issue_extent")
    review = row.get("style_review")
    neutral_review = (
        isinstance(review, dict)
        and review.get("status") == "not_applicable"
        and review.get("full_size") is False
        and review.get("reviewer") is None
        and review.get("reviewed_at") is None
        and review.get("comparison_path") is None
        and review.get("comparison_sha256") is None
        and review.get("full_page_exception_approved") is False
        and isinstance(review.get("checks"), dict)
        and all(review["checks"].get(name) is False for name in STYLE_REVIEW_CHECKS)
    )
    neutral = (
        mode == "not_applicable"
        and extent == "not_applicable"
        and row.get("style_reference_pages") == []
        and row.get("identity_reference_files") == []
        and row.get("character_reference_role") == "not_applicable"
        and row.get("visual_mask_path") is None
        and row.get("visual_mask_sha256") is None
        and row.get("changed_region_ratio") == 0.0
        and row.get("full_page_regeneration_reason") is None
        and neutral_review
    )
    if scope in {"unchanged", "text_only"}:
        if not neutral:
            errors.append(f"neutral style contract invalid at page {page_index}")
        return
    if scope != "visual":
        return

    if mode not in VISUAL_EDIT_MODES or mode == "not_applicable":
        errors.append(f"visual_edit_mode invalid at page {page_index}")
    if extent not in VISUAL_ISSUE_EXTENTS or extent == "not_applicable":
        errors.append(f"visual_issue_extent invalid at page {page_index}")

    references = row.get("style_reference_pages")
    if (
        not _string_list(references, nonempty=True)
        or len(set(references)) != len(references)
        or any(reference not in output_names for reference in references)
    ):
        errors.append(f"style_reference_pages invalid at page {page_index}")
    elif len(output_names) > 1:
        current = page_index - 1
        positions = {name: offset for offset, name in enumerate(output_names)}
        if not any(1 <= abs(positions[reference] - current) <= 2 for reference in references):
            errors.append(f"adjacent style reference missing at page {page_index}")

    identity_files = row.get("identity_reference_files")
    if not _string_list(identity_files) or len(set(identity_files or [])) != len(identity_files or []):
        errors.append(f"identity_reference_files invalid at page {page_index}")
    elif identity_files:
        if row.get("character_reference_role") != "identity_only":
            errors.append(f"character_reference_role must be identity_only at page {page_index}")
        for name in identity_files:
            try:
                reference = ensure_safe_subpath(root / "人物参考图", Path(name))
            except (OSError, ValueError) as exc:
                errors.append(f"identity reference unsafe at page {page_index}: {exc}")
                continue
            if not reference.is_file():
                errors.append(f"identity reference missing at page {page_index}: {name}")
    elif row.get("character_reference_role") != "not_applicable":
        errors.append(f"character_reference_role invalid at page {page_index}")

    _validate_hashed_project_file(
        root,
        row,
        "visual_mask_path",
        "visual_mask_sha256",
        "visual mask",
        page_index,
        errors,
    )
    ratio = row.get("changed_region_ratio")
    if (
        not isinstance(ratio, (int, float))
        or isinstance(ratio, bool)
        or not 0 < ratio <= 1
    ):
        errors.append(f"changed_region_ratio invalid at page {page_index}")

    mask_is_full = False
    try:
        mask_path = ensure_safe_subpath(root, Path(row["visual_mask_path"]))
        input_path = input_pages[page_index - 1]
        with Image.open(mask_path) as mask_image, Image.open(input_path) as source_image:
            mask = mask_image.convert("L")
            if mask.size != source_image.size:
                errors.append(f"visual mask dimensions mismatch at page {page_index}")
            pixels = (
                mask.get_flattened_data()
                if hasattr(mask, "get_flattened_data")
                else mask.getdata()
            )
            nonzero = sum(1 for pixel in pixels if pixel > 0)
            measured_ratio = nonzero / (mask.width * mask.height)
            mask_is_full = nonzero == mask.width * mask.height
            if isinstance(ratio, (int, float)) and not isinstance(ratio, bool):
                if abs(measured_ratio - float(ratio)) > 0.01:
                    errors.append(f"changed_region_ratio mismatch at page {page_index}")
    except (KeyError, TypeError, OSError, ValueError):
        pass

    if not isinstance(review, dict):
        errors.append(f"style_review missing at page {page_index}")
        review = {}
    if (
        review.get("status") != "passed"
        or review.get("full_size") is not True
        or not _nonempty_string(review.get("reviewer"))
        or _parse_zoned_datetime(review.get("reviewed_at")) is None
    ):
        errors.append(f"style_review not passed full-size with reviewer/time at page {page_index}")
    _validate_checks(review, STYLE_REVIEW_CHECKS, "style_review", page_index, errors)
    _validate_hashed_project_file(
        root,
        review,
        "comparison_path",
        "comparison_sha256",
        "style comparison",
        page_index,
        errors,
    )

    if mode in {"localized_inpaint", "localized_regeneration"}:
        if (
            extent != "localized"
            or row.get("full_page_regeneration_reason") is not None
            or mask_is_full
        ):
            errors.append(f"localized visual repair contract invalid at page {page_index}")
        if review.get("full_page_exception_approved") is not False:
            errors.append(f"localized repair cannot use full-page approval at page {page_index}")
    elif mode == "full_page_regeneration":
        if extent != "page_wide":
            errors.append(f"full-page regeneration requires page-wide issue at page {page_index}")
        if not _nonempty_string(row.get("full_page_regeneration_reason")):
            errors.append(f"full-page regeneration reason missing at page {page_index}")
        if ratio != 1.0 or not mask_is_full:
            errors.append(f"full-page regeneration requires full mask at page {page_index}")
        if review.get("full_page_exception_approved") is not True:
            errors.append(f"full-page regeneration approval missing at page {page_index}")


def _validate_continuity_bible(
    bible: dict[str, Any] | None, output_names: list[str], errors: list[str]
) -> dict[str, dict[str, Any]]:
    if bible is None:
        return {}
    if bible.get("status") != "confirmed":
        errors.append("continuity bible status is not confirmed")
    if not _nonempty_string(bible.get("reviewer")):
        errors.append("continuity bible reviewer missing")
    if _parse_zoned_datetime(bible.get("reviewed_at")) is None:
        errors.append("continuity bible reviewed_at must be timezone-aware ISO8601")
    source_priority = bible.get("source_priority")
    if source_priority != list(CONTINUITY_SOURCE_TYPES):
        errors.append("continuity bible source_priority invalid")
    coverage = bible.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("continuity bible coverage missing")
        coverage = {}
    locks = bible.get("locks")
    if not isinstance(locks, list):
        errors.append("continuity bible locks must be a list")
        locks = []
    lock_map: dict[str, dict[str, Any]] = {}
    categories_with_locks: set[str] = set()
    valid_outputs = set(output_names)
    for position, lock in enumerate(locks, start=1):
        if not isinstance(lock, dict):
            errors.append(f"continuity lock {position} must be an object")
            continue
        if set(lock) != CONTINUITY_LOCK_FIELDS:
            errors.append(f"continuity lock {position} fields invalid")
        lock_id = lock.get("lock_id")
        if not _nonempty_string(lock_id) or lock_id in lock_map:
            errors.append(f"continuity lock {position} lock_id missing or duplicate")
        else:
            lock_map[lock_id] = lock
        category = lock.get("category")
        if category not in CONTINUITY_CATEGORIES:
            errors.append(f"continuity lock {position} category invalid")
        else:
            categories_with_locks.add(category)
        for field in ("subject", "attribute", "value", "source_ref", "created_by"):
            if not _nonempty_string(lock.get(field)):
                errors.append(f"continuity lock {position} {field} missing")
        if lock.get("source_type") not in CONTINUITY_SOURCE_TYPES:
            errors.append(f"continuity lock {position} source_type invalid")
        if lock.get("confirmed") is not True:
            errors.append(f"continuity lock {position} confirmed must be true")
        if not _nonempty_string(lock.get("reviewer")):
            errors.append(f"continuity lock {position} reviewer missing")
        if _parse_zoned_datetime(lock.get("reviewed_at")) is None:
            errors.append(f"continuity lock {position} reviewed_at invalid")
        applies = lock.get("applies_to_pages")
        if not _string_list(applies, nonempty=True) or any(page not in valid_outputs for page in applies):
            errors.append(f"continuity lock {position} applies_to_pages invalid")
        confidence = lock.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            errors.append(f"continuity lock {position} confidence invalid")
        if _parse_zoned_datetime(lock.get("created_at")) is None:
            errors.append(f"continuity lock {position} created_at invalid")
    for category in CONTINUITY_CATEGORIES:
        entry = coverage.get(category)
        if not isinstance(entry, dict) or entry.get("status") not in {"confirmed", "not_applicable"}:
            errors.append(f"continuity bible coverage {category} invalid")
            continue
        coverage_lock_ids = entry.get("lock_ids")
        if entry["status"] == "confirmed":
            if (
                not _string_list(coverage_lock_ids, nonempty=True)
                or len(set(coverage_lock_ids)) != len(coverage_lock_ids)
            ):
                errors.append(f"continuity bible coverage {category} lock_ids invalid")
            else:
                for lock_id in coverage_lock_ids:
                    lock = lock_map.get(lock_id)
                    if lock is None:
                        errors.append(f"continuity bible coverage {category} references unknown lock {lock_id!r}")
                    elif lock.get("category") != category:
                        errors.append(f"continuity bible coverage {category} references cross-category lock {lock_id!r}")
            if category not in categories_with_locks:
                errors.append(f"continuity bible coverage {category} confirmed without lock")
        else:
            if coverage_lock_ids != []:
                errors.append(f"continuity bible coverage {category} not_applicable lock_ids must be empty")
            if not _nonempty_string(entry.get("rationale")):
                errors.append(f"continuity bible coverage {category} not_applicable needs rationale")
    return lock_map


def _validate_scene_cluster_contract(
    root: Path,
    evidence: Path,
    run: dict[str, Any] | None,
    repair: dict[str, Any] | None,
    run_pages: list[dict[str, Any]],
    repair_pages: list[dict[str, Any]],
    input_pages: list[Path],
    expected_names: list[str],
    errors: list[str],
) -> None:
    """Validate scene-cluster-only traceability without breaking legacy evidence."""
    new_page_fields = {
        "cluster_id",
        "reference_pack_id",
        "task_id",
        "failure_rule_ids",
        "generated_by",
        "reviewed_by",
    }
    run_mode = run.get("pipeline_mode") if run is not None else None
    repair_mode = repair.get("pipeline_mode") if repair is not None else None
    run_schema = run.get("schema_version") if run is not None else None
    repair_schema = repair.get("schema_version") if repair is not None else None
    if run_schema != repair_schema:
        errors.append("schema_version mismatch between manifest and repair log")
        return
    if run_schema == "2.0":
        has_new_contract = (
            run_mode is not None
            or repair_mode is not None
            or (run is not None and "evidence_files" in run)
            or (repair is not None and "evidence_files" in repair)
            or any(new_page_fields & set(page) for page in run_pages + repair_pages)
        )
        if has_new_contract:
            errors.append("legacy schema contains scene-cluster fields")
        return
    if run_schema != "3.0":
        errors.append(f"unsupported manifest schema_version: {run_schema!r}")
        return
    if run_mode is None or repair_mode is None:
        errors.append("schema_version 3.0 requires pipeline_mode scene_cluster_v1")
        return
    if run_mode != repair_mode:
        errors.append("pipeline_mode mismatch between manifest and repair log")
        return
    if run_mode != SCENE_CLUSTER_PIPELINE_MODE:
        errors.append(f"unsupported pipeline_mode: {run_mode!r}")
        return

    for label, document in (("manifest", run), ("repair log", repair)):
        declared = document.get("evidence_files") if document is not None else None
        if declared != list(EVIDENCE_FILES):
            errors.append(f"{label} evidence_files must exactly declare the v3 evidence set")

    expected_inputs = [page.name for page in input_pages]
    mapped_inputs = [page.get("input_name") for page in run_pages]
    mapped_outputs = [page.get("output_name") for page in run_pages]
    repair_outputs = [page.get("output_name") for page in repair_pages]
    if (
        len(mapped_inputs) != len(set(mapped_inputs))
        or mapped_inputs != expected_inputs
        or len(mapped_outputs) != len(set(mapped_outputs))
        or mapped_outputs != expected_names
        or len(repair_outputs) != len(set(repair_outputs))
        or repair_outputs != expected_names
    ):
        errors.append("scene-cluster input/output mapping must be a strict bijection")

    cluster_document = _load_json(evidence / "scene_clusters.json", errors)
    _validate_registry_hash(cluster_document, "scene_clusters", errors)
    raw_clusters = cluster_document.get("clusters") if cluster_document is not None else None
    cluster_map: dict[str, dict[str, Any]] = {}
    page_to_cluster: dict[str, str] = {}
    ordered_members: list[str] = []
    cursor = 0
    if not isinstance(raw_clusters, list):
        if cluster_document is not None:
            errors.append("scene_clusters clusters must be a list")
    else:
        for position, cluster in enumerate(raw_clusters, start=1):
            if not isinstance(cluster, dict) or not _nonempty_string(cluster.get("cluster_id")):
                errors.append(f"scene cluster identity invalid at position {position}")
                continue
            cluster_id = cluster["cluster_id"]
            if cluster_id in cluster_map:
                errors.append(f"duplicate scene cluster_id: {cluster_id!r}")
                continue
            cluster_map[cluster_id] = cluster
            members = cluster.get("member_pages")
            if (
                not _string_list(members, nonempty=True)
                or len(set(members or [])) != len(members or [])
            ):
                errors.append(f"cluster membership invalid for {cluster_id!r}")
                continue
            size = len(members)
            undersized = cluster.get("undersized")
            if (
                not isinstance(undersized, bool)
                or size > 20
                or (undersized and size >= 8)
                or (not undersized and size < 8)
            ):
                errors.append(f"cluster size/undersized contract invalid for {cluster_id!r}")
            if undersized is True:
                valid_boundary_exception = (
                    cluster.get("boundary_exception") is True
                    and len(expected_names) < 8
                    and members == expected_names
                    and cluster.get("undersized_reason") == "project_total_below_min"
                )
                if not valid_boundary_exception:
                    errors.append(
                        f"non-boundary undersized cluster is blocked from final QA: {cluster_id!r}"
                    )
            expected_members = expected_names[cursor : cursor + size]
            if members != expected_members:
                errors.append(f"cluster membership/order mismatch for {cluster_id!r}")
            before = expected_names[max(0, cursor - 2) : cursor]
            after = expected_names[cursor + size : cursor + size + 2]
            if cluster.get("context_before") != before or cluster.get("context_after") != after:
                errors.append(f"cluster context must be exact +/-2 for {cluster_id!r}")
            if cluster.get("canary_page") not in members:
                errors.append(f"cluster canary must be a member for {cluster_id!r}")
            if (
                cluster.get("reference_pack_state") != "bound"
                or not _nonempty_string(cluster.get("reference_pack_id"))
            ):
                errors.append(f"cluster reference pack must be bound for {cluster_id!r}")
            for member in members:
                if member in page_to_cluster:
                    errors.append(f"cluster membership duplicates page {member!r}")
                page_to_cluster[member] = cluster_id
            ordered_members.extend(members)
            cursor += size
    if ordered_members != expected_names:
        errors.append("cluster membership must exactly cover all output pages in order")

    queue_path = evidence / "task_queue.json"
    queue_document = _load_json(queue_path, errors)
    _validate_registry_hash(queue_document, "task_queue", errors)
    raw_tasks = queue_document.get("tasks") if queue_document is not None else None
    task_map: dict[str, dict[str, Any]] = {}
    if not isinstance(raw_tasks, list):
        if queue_document is not None:
            errors.append("task queue tasks must be a list")
    else:
        for position, task in enumerate(raw_tasks, start=1):
            if not isinstance(task, dict) or not _nonempty_string(task.get("task_id")):
                errors.append(f"task queue task_id invalid at position {position}")
                continue
            task_id = task["task_id"]
            if task_id in task_map:
                errors.append(f"duplicate task_id in task queue: {task_id!r}")
                continue
            task_map[task_id] = task
            if task.get("state") != "completed":
                errors.append(
                    f"unfinished task {task_id!r} has state {task.get('state')!r}"
                )
        try:
            load_queue(queue_path)
        except (OSError, ValueError) as exc:
            errors.append(f"task queue invalid: {exc}")

        expected_task_by_page: dict[str, str] = {}
        strict_task_mapping = len(raw_tasks) == len(expected_names)
        for index, output_name in enumerate(expected_names):
            expected_page_id = normalize_page_id(output_name)
            run_task_id = (
                run_pages[index].get("task_id")
                if index < len(run_pages)
                else None
            )
            repair_task_id = (
                repair_pages[index].get("task_id")
                if index < len(repair_pages)
                else None
            )
            if (
                not _nonempty_string(run_task_id)
                or run_task_id != repair_task_id
            ):
                strict_task_mapping = False
            else:
                expected_task_by_page[expected_page_id] = run_task_id

        queue_task_by_page: dict[str, dict[str, Any]] = {}
        for task in raw_tasks:
            if not isinstance(task, dict):
                strict_task_mapping = False
                continue
            page_id = task.get("page_id")
            if (
                not _nonempty_string(page_id)
                or page_id not in expected_task_by_page
                or page_id in queue_task_by_page
            ):
                strict_task_mapping = False
                continue
            queue_task_by_page[page_id] = task
            if (
                task.get("state") != "completed"
                or task.get("task_id") != expected_task_by_page[page_id]
            ):
                strict_task_mapping = False
        if set(queue_task_by_page) != set(expected_task_by_page):
            strict_task_mapping = False
        if not strict_task_mapping:
            errors.append(
                "task queue must map exactly one completed task per manifest page"
            )

    learning_path = evidence / "failure_learning.json"
    learning = _load_json(learning_path, errors)
    _validate_registry_hash(learning, "failure_learning", errors)
    rule_ids: set[str] = set()
    if learning is not None:
        try:
            validate_failure_store(learning)
        except ValueError as exc:
            errors.append(f"failure learning invalid: {exc}")
        rules = learning.get("rules")
        if isinstance(rules, list):
            rule_ids = {
                rule["rule_id"]
                for rule in rules
                if isinstance(rule, dict) and _nonempty_string(rule.get("rule_id"))
            }
        else:
            errors.append("failure learning rules must be a list")

    packs_document = _load_json(evidence / "style_reference_packs.json", errors)
    _validate_registry_hash(packs_document, "style_reference_packs", errors)
    reference_pack_map: dict[str, dict[str, Any]] = {}
    rebuilt_reference_binding_map: dict[str, str] = {}
    stable_pages = packs_document.get("stable_pages") if packs_document is not None else None
    approved_hashes = packs_document.get("approved_hashes") if packs_document is not None else None
    if not _string_list(stable_pages) or len(set(stable_pages or [])) != len(stable_pages or []):
        if packs_document is not None:
            errors.append("style_reference_packs stable_pages must be a unique string list")
        stable_pages = []
    if not isinstance(approved_hashes, dict) or any(
        not _nonempty_string(path) or not _nonempty_string(digest)
        for path, digest in (approved_hashes.items() if isinstance(approved_hashes, dict) else [])
    ):
        if packs_document is not None:
            errors.append("style_reference_packs approved_hashes invalid")
        approved_hashes = {}
    packs = packs_document.get("reference_packs") if packs_document is not None else None
    if not isinstance(packs, list):
        if packs_document is not None:
            errors.append("style_reference_packs reference_packs must be a list")
    else:
        for position, pack in enumerate(packs, start=1):
            if not isinstance(pack, dict):
                errors.append(f"style reference pack {position} must be an object")
                continue
            pack_id = pack.get("reference_pack_id")
            cluster_id = pack.get("cluster_id")
            if not _nonempty_string(pack_id) or not _nonempty_string(cluster_id):
                errors.append(f"style reference pack identity missing at position {position}")
                continue
            if pack_id in reference_pack_map:
                errors.append(f"duplicate reference_pack_id: {pack_id!r}")
                continue
            references = pack.get("references")
            try:
                validate_reference_pack(
                    pack, stable_pages=stable_pages, contract_version="v3"
                )
            except ValueError as exc:
                errors.append(f"style reference pack {pack_id!r} invalid: {exc}")
            if isinstance(references, list):
                canonical_references: list[dict[str, str]] = []
                for reference_position, reference in enumerate(references, start=1):
                    if not isinstance(reference, dict):
                        errors.append(
                            f"style reference pack {pack_id!r} reference {reference_position} invalid"
                        )
                        continue
                    path = reference.get("path")
                    role = reference.get("role")
                    digest = reference.get("sha256")
                    if (
                        not _nonempty_string(path)
                        or not _nonempty_string(role)
                        or not _nonempty_string(digest)
                    ):
                        errors.append(
                            f"style reference pack {pack_id!r} reference trace missing"
                        )
                        continue
                    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                        errors.append(
                            f"style reference pack {pack_id!r} sha256 must be 64 hexadecimal characters"
                        )
                        continue
                    canonical_references.append(
                        {"path": path, "role": role, "sha256": digest}
                    )
                    if approved_hashes.get(path) != digest:
                        errors.append(
                            f"style reference pack {pack_id!r} approved hash mismatch"
                        )
                    try:
                        reference_path = ensure_safe_subpath(root, Path(path))
                    except (OSError, ValueError) as exc:
                        errors.append(
                            f"style reference pack {pack_id!r} path unsafe: {exc}"
                        )
                    else:
                        if not reference_path.is_file() or sha256_file(reference_path) != digest:
                            errors.append(
                                f"style reference pack {pack_id!r} content hash mismatch"
                            )
                try:
                    rebuilt = build_reference_pack(
                        {"cluster_id": cluster_id},
                        canonical_references,
                        stable_pages=stable_pages,
                        contract_version="v3",
                    )
                except ValueError:
                    rebuilt = None
                if rebuilt is not None:
                    rebuilt_reference_binding_map[pack_id] = rebuilt[
                        "reference_binding_hash"
                    ]
                    if rebuilt["references"] != references:
                        errors.append(
                            f"style reference pack references are not canonical for {pack_id!r}"
                        )
                    if rebuilt["reference_pack_id"] != pack_id:
                        errors.append(
                            f"reference_pack_id content mismatch for {pack_id!r}"
                        )
                    if pack.get("reference_binding_hash") != rebuilt["reference_binding_hash"]:
                        errors.append(
                            f"reference_binding_hash mismatch for {pack_id!r}"
                        )
            reference_pack_map[pack_id] = pack

    for cluster_id, cluster in cluster_map.items():
        pack_id = cluster.get("reference_pack_id")
        pack = reference_pack_map.get(pack_id) if _nonempty_string(pack_id) else None
        if pack is None or pack.get("cluster_id") != cluster_id:
            errors.append(f"cluster reference pack mismatch for {cluster_id!r}")

    for index, output_name in enumerate(expected_names, start=1):
        run_row = run_pages[index - 1] if index <= len(run_pages) else None
        repair_row = repair_pages[index - 1] if index <= len(repair_pages) else None
        if run_row is None or repair_row is None:
            continue
        for field in ("cluster_id", "reference_pack_id", "task_id"):
            if not _nonempty_string(run_row.get(field)):
                errors.append(f"{field} missing in manifest at page {index}")
            if not _nonempty_string(repair_row.get(field)):
                errors.append(f"{field} missing in repair log at page {index}")
            if run_row.get(field) != repair_row.get(field):
                errors.append(f"{field} mismatch between manifest and repair log at page {index}")

        expected_cluster_id = page_to_cluster.get(output_name)
        if repair_row.get("cluster_id") != expected_cluster_id:
            errors.append(f"page cluster_id does not match scene registry at page {index}")
        expected_cluster = cluster_map.get(expected_cluster_id)
        if (
            expected_cluster is not None
            and repair_row.get("reference_pack_id") != expected_cluster.get("reference_pack_id")
        ):
            errors.append(f"page reference_pack_id does not match scene registry at page {index}")

        reference_pack_id = repair_row.get("reference_pack_id")
        reference_pack = (
            reference_pack_map.get(reference_pack_id)
            if _nonempty_string(reference_pack_id)
            else None
        )
        if reference_pack is None:
            errors.append(f"reference_pack_id is not traceable at page {index}")
        elif reference_pack.get("cluster_id") != repair_row.get("cluster_id"):
            errors.append(f"reference_pack_id cluster mismatch at page {index}")

        for identity_field in ("generated_by", "reviewed_by"):
            run_identity = run_row.get(identity_field)
            repair_identity = repair_row.get(identity_field)
            if not _nonempty_string(run_identity) or not _nonempty_string(repair_identity):
                errors.append(f"{identity_field} missing at page {index}")
            elif run_identity != repair_identity:
                errors.append(
                    f"{identity_field} mismatch between manifest and repair log at page {index}"
                )
        if (
            repair_row.get("repair_scope") != "unchanged"
            and _nonempty_string(repair_row.get("generated_by"))
            and repair_row.get("generated_by") == repair_row.get("reviewed_by")
        ):
            errors.append(f"processed page requires independent review at page {index}")
        page_qa = repair_row.get("page_qa")
        if (
            isinstance(page_qa, dict)
            and repair_row.get("reviewed_by") != page_qa.get("reviewer")
        ):
            errors.append(f"reviewed_by must match page_qa reviewer at page {index}")

        task_id = repair_row.get("task_id")
        task = task_map.get(task_id) if _nonempty_string(task_id) else None
        if task is None:
            errors.append(f"task_id is not traceable at page {index}")
        else:
            try:
                expected_page_id = normalize_page_id(output_name)
            except ValueError:
                expected_page_id = Path(output_name).stem
            if task.get("page_id") != expected_page_id:
                errors.append(f"task_id page mapping mismatch at page {index}")
            if task.get("cluster_id") != repair_row.get("cluster_id"):
                errors.append(f"task_id cluster mapping mismatch at page {index}")
            if task.get("state") == "completed" and task.get("candidate_hash") != run_row.get("output_sha256"):
                errors.append(f"task candidate hash mismatch at page {index}")
            if task.get("state") == "completed" and task.get("completed_by") != repair_row.get("generated_by"):
                errors.append(f"task completed_by must match generated_by at page {index}")
            if (
                reference_pack is not None
                and task.get("prompt_reference_hash")
                != rebuilt_reference_binding_map.get(reference_pack_id)
            ):
                errors.append(
                    f"task prompt_reference_hash must match reference pack binding at page {index}"
                )

        run_rule_ids = run_row.get("failure_rule_ids")
        repair_rule_ids = repair_row.get("failure_rule_ids")
        if (
            not _string_list(run_rule_ids)
            or len(set(run_rule_ids or [])) != len(run_rule_ids or [])
            or not _string_list(repair_rule_ids)
            or len(set(repair_rule_ids or [])) != len(repair_rule_ids or [])
            or run_rule_ids != repair_rule_ids
        ):
            errors.append(f"failure_rule_ids invalid or mismatched at page {index}")
        elif any(rule_id not in rule_ids for rule_id in repair_rule_ids):
            errors.append(f"failure_rule_ids are not traceable at page {index}")

        if repair_row.get("visual_edit_mode") == "full_page_regeneration":
            generated_by = repair_row.get("generated_by")
            reviewed_by = repair_row.get("reviewed_by")
            if (
                not _nonempty_string(generated_by)
                or not _nonempty_string(reviewed_by)
                or generated_by == reviewed_by
            ):
                errors.append(
                    f"full-page regeneration requires independent review at page {index}"
                )
            for label in ("style_review", "visual_review"):
                review = repair_row.get(label)
                if (
                    not isinstance(review, dict)
                    or not _nonempty_string(review.get("reviewer"))
                    or review.get("reviewer") == generated_by
                ):
                    errors.append(
                        f"full-page candidate requires independent {label} at page {index}"
                    )

    cluster_qa_document = _load_json(evidence / "scene_cluster_qa.json", errors)
    _validate_registry_hash(cluster_qa_document, "scene_cluster_qa", errors)
    qa_rows = cluster_qa_document.get("clusters") if cluster_qa_document is not None else None
    qa_map: dict[str, dict[str, Any]] = {}
    pass_ids: set[str] = set()
    if not isinstance(qa_rows, list):
        if cluster_qa_document is not None:
            errors.append("scene_cluster_qa clusters must be a list")
    else:
        generated_by_page = {
            row.get("output_name"): row.get("generated_by")
            for row in repair_pages
            if _nonempty_string(row.get("output_name"))
        }
        for position, qa_row in enumerate(qa_rows, start=1):
            if not isinstance(qa_row, dict) or not _nonempty_string(qa_row.get("cluster_id")):
                errors.append(f"scene_cluster_qa identity invalid at position {position}")
                continue
            cluster_id = qa_row["cluster_id"]
            if cluster_id in qa_map:
                errors.append(f"scene_cluster_qa must contain exactly one pass for {cluster_id!r}")
                continue
            qa_map[cluster_id] = qa_row
            cluster = cluster_map.get(cluster_id)
            if cluster is None:
                errors.append(f"scene_cluster_qa references unknown cluster {cluster_id!r}")
                continue
            if (
                qa_row.get("member_pages") != cluster.get("member_pages")
                or qa_row.get("canary_page") != cluster.get("canary_page")
            ):
                errors.append(f"scene_cluster_qa membership/canary mismatch for {cluster_id!r}")
            pass_id = qa_row.get("pass_id")
            if qa_row.get("status") != "passed" or not _nonempty_string(pass_id):
                errors.append(f"scene_cluster_qa pass invalid for {cluster_id!r}")
            elif pass_id in pass_ids:
                errors.append(f"scene_cluster_qa pass_id must be unique: {pass_id!r}")
            else:
                pass_ids.add(pass_id)
            reviewer = qa_row.get("reviewed_by")
            if not _nonempty_string(reviewer) or _parse_zoned_datetime(qa_row.get("reviewed_at")) is None:
                errors.append(f"scene_cluster_qa reviewer/time invalid for {cluster_id!r}")
            member_generators = {
                generated_by_page.get(member)
                for member in cluster.get("member_pages", [])
                if _nonempty_string(generated_by_page.get(member))
            }
            if reviewer in member_generators:
                errors.append(f"cluster reviewer must be independent for {cluster_id!r}")
    if set(qa_map) != set(cluster_map):
        errors.append("scene_cluster_qa must contain exactly one passed row per scene cluster")


def validate_project(
    root: Path,
    evidence_dir: Path,
    expected_count: int | None = None,
) -> dict:
    if expected_count is not None and (
        not isinstance(expected_count, int)
        or isinstance(expected_count, bool)
        or not 1 <= expected_count <= 9999
    ):
        raise ValueError("expected_count must be an integer within 1..9999")
    errors: list[str] = []
    counts = {"expected": expected_count or 0, "input": 0, "output": 0, "manifest": 0}
    try:
        project = discover_project(root)
        evidence = ensure_safe_subpath(project.root, evidence_dir)
        input_pages = sorted_input_pages(project.input_dir)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        return {"ok": False, "errors": errors, "counts": counts}

    actual_count = len(input_pages)
    if not 1 <= actual_count <= 9999:
        raise ValueError(f"input page count must be within 1..9999, found {actual_count}")
    if expected_count is None:
        expected_count = actual_count
    elif expected_count != actual_count:
        raise ValueError(
            f"input page count mismatch: expected {expected_count}, found {actual_count}"
        )
    counts["expected"] = expected_count
    counts["input"] = actual_count

    preload_errors: list[str] = []
    preloaded_run = _load_json(evidence / "comic_run_manifest.json", preload_errors)
    if (
        preloaded_run is not None
        and preloaded_run.get("schema_version") == "4.0"
        and preloaded_run.get("pipeline_mode") == "continuity_v4"
    ):
        result = _validate_v4_project(
            project, evidence, input_pages, expected_count, preloaded_run
        )
        if preload_errors:
            result["errors"] = preload_errors + result["errors"]
            result["ok"] = False
        return result

    output_entries = sorted(project.output_dir.iterdir(), key=lambda path: path.name)
    output_pages = sorted(
        path
        for path in project.output_dir.iterdir()
        if path.is_file() and path.suffix.casefold() in INPUT_PAGE_EXTENSIONS
    )
    counts["output"] = len(output_pages)
    expected_names = [f"{index:04d}.jpg" for index in range(1, expected_count + 1)]
    actual_names = sorted(path.name for path in output_pages)
    output_by_name = {path.name: path for path in output_pages}
    if len(output_pages) != expected_count:
        errors.append(f"output count mismatch: expected {expected_count}, found {len(output_pages)}")
    if actual_names != expected_names:
        errors.append("output names must be contiguous 0001.jpg through expected count")
    if [path.name for path in output_entries] != expected_names or any(not path.is_file() for path in output_entries):
        errors.append("output directory must contain only exact contiguous JPG output files")
    for output in output_pages:
        try:
            verify_readable_image(output)
        except ValueError as exc:
            errors.append(str(exc))

    run = _load_json(evidence / "comic_run_manifest.json", errors)
    alignment = _load_json(evidence / "novel_alignment.json", errors)
    repair = _load_json(evidence / "repair_log.json", errors)
    bible = _load_json(evidence / "continuity_bible.json", errors)
    run_pages = _indexed_pages(run, "comic_run_manifest", errors)
    alignment_pages = _indexed_pages(alignment, "novel_alignment", errors)
    repair_pages = _indexed_pages(repair, "repair_log", errors)
    counts["manifest"] = len(run_pages)
    for label, pages in (
        ("manifest", run_pages),
        ("novel alignment", alignment_pages),
        ("repair log", repair_pages),
    ):
        if len(pages) != expected_count:
            errors.append(f"{label} page count mismatch: expected {expected_count}, found {len(pages)}")

    _validate_scene_cluster_contract(
        project.root,
        evidence,
        run,
        repair,
        run_pages,
        repair_pages,
        input_pages,
        expected_names,
        errors,
    )

    if run is not None:
        if run.get("status") != "batch_qa_passed":
            errors.append("run status is not batch_qa_passed")
        if run.get("expected_count") != expected_count:
            errors.append("manifest expected_count mismatch")
        if run.get("novel_name") != project.novel.name or run.get("novel_sha256") != sha256_file(project.novel):
            errors.append("novel identity/hash mismatch")
    if alignment is not None and alignment.get("status") != "confirmed":
        errors.append("novel alignment status is not confirmed")
    if repair is not None and repair.get("status") != "batch_qa_passed":
        errors.append("repair status is not batch_qa_passed")
    lock_map = _validate_continuity_bible(bible, expected_names, errors)
    for index, input_page in enumerate(input_pages):
        if index >= len(run_pages):
            break
        row = run_pages[index]
        expected_output = f"{index + 1:04d}.jpg"
        if row.get("index") != index + 1 or row.get("output_name") != expected_output:
            errors.append(f"manifest mapping mismatch at page {index + 1}")
        if row.get("input_name") != input_page.name or row.get("input_sha256") != sha256_file(input_page):
            errors.append(f"input hash/identity mismatch at page {index + 1}")
        if row.get("state") != "batch_qa_passed":
            errors.append(f"state is not batch_qa_passed at page {index + 1}")
        else:
            recorded_output_hash = row.get("output_sha256")
            output_path = output_by_name.get(expected_output)
            if not isinstance(recorded_output_hash, str) or not recorded_output_hash:
                errors.append(f"output hash missing at page {index + 1}")
            elif output_path is None or recorded_output_hash != sha256_file(output_path):
                errors.append(f"output hash mismatch at page {index + 1}")
        lock_ids = row.get("continuity_lock_ids")
        if not _string_list(lock_ids, nonempty=True):
            errors.append(f"continuity_lock_ids missing at page {index + 1}")
        else:
            for lock_id in lock_ids:
                lock = lock_map.get(lock_id)
                if lock is None:
                    errors.append(f"continuity_lock_ids references unknown lock {lock_id!r} at page {index + 1}")
                elif expected_output not in lock.get("applies_to_pages", []):
                    errors.append(f"continuity lock {lock_id!r} does not apply to page {expected_output}")

    try:
        novel_text = project.novel.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"novel text unreadable: {exc}")
        novel_text = ""
    current_novel_hash = sha256_file(project.novel)
    for index, row in enumerate(alignment_pages, start=1):
        if row.get("index") != index or row.get("output_name") != f"{index:04d}.jpg":
            errors.append(f"novel alignment mapping mismatch at page {index}")
        if row.get("status") != "confirmed" or not str(row.get("evidence", "")).strip():
            errors.append(f"novel alignment not confirmed with evidence at page {index}")
            continue
        if row.get("novel_sha256") != current_novel_hash:
            errors.append(f"novel_sha256 mismatch at page {index}")
        for field in ("chapter", "context_excerpt", "located_by", "located_at"):
            if not _nonempty_string(row.get(field)):
                errors.append(f"novel alignment {field} missing at page {index}")
        for field in ("scene_summary", "location", "story_time"):
            if not _nonempty_string(row.get(field)):
                errors.append(f"novel alignment {field} missing at page {index}")
        if not _string_list(row.get("involved_characters"), nonempty=True):
            errors.append(f"involved_characters must be a nonempty string list at page {index}")
        for field in ("dialogue_owners", "props"):
            if field not in row or not _string_list(row.get(field)):
                errors.append(f"{field} must be a string list at page {index}")
        start = row.get("start_offset")
        end = row.get("end_offset")
        valid_offsets = (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(novel_text)
        )
        if not valid_offsets:
            errors.append(f"novel alignment offset range invalid at page {index}")
        elif row.get("source_excerpt") != novel_text[start:end]:
            errors.append(f"source_excerpt does not match novel slice at page {index}")
        confidence = row.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            errors.append(f"confidence out of range at page {index}")
    seen_pass_ids: dict[str, str] = {}
    page_qa_times: dict[str, datetime | None] = {}
    page_batch_refs: dict[str, Any] = {}
    for index, row in enumerate(repair_pages, start=1):
        output_name = f"{index:04d}.jpg"
        if row.get("index") != index or row.get("output_name") != f"{index:04d}.jpg":
            errors.append(f"repair log mapping mismatch at page {index}")
        page_batch_refs[output_name] = row.get("batch_id")
        repair_lock_ids = row.get("continuity_lock_ids")
        run_lock_ids = run_pages[index - 1].get("continuity_lock_ids") if index <= len(run_pages) else None
        if (
            not _string_list(repair_lock_ids, nonempty=True)
            or len(set(repair_lock_ids)) != len(repair_lock_ids)
        ):
            errors.append(f"repair continuity_lock_ids invalid at page {index}")
        else:
            for lock_id in repair_lock_ids:
                lock = lock_map.get(lock_id)
                if lock is None or output_name not in lock.get("applies_to_pages", []):
                    errors.append(f"repair continuity_lock_ids contains invalid lock {lock_id!r} at page {index}")
        if not isinstance(run_lock_ids, list) or not isinstance(repair_lock_ids, list) or set(run_lock_ids) != set(repair_lock_ids):
            errors.append(f"continuity_lock_ids mismatch between run and repair at page {index}")
        if row.get("action") not in {"unchanged_copy", "edited"}:
            errors.append(f"action invalid at page {index}")
        if row.get("text_reset_mode") not in {"none", "block", "page"}:
            errors.append(f"text_reset_mode invalid at page {index}")
        if row.get("repair_scope") not in {"unchanged", "text_only", "visual"}:
            errors.append(f"repair_scope invalid at page {index}")
        if row.get("text_recognition") not in {"not_applicable", "reliable", "unreliable"}:
            errors.append(f"text_recognition invalid at page {index}")
        if not _nonempty_string(row.get("text_reset_reason")):
            errors.append(f"text_reset_reason missing at page {index}")
        elif row.get("text_reset_reason") not in {
            "unchanged_page",
            "visual_repair_requires_page_reset",
            "no_ordinary_text",
            "reliable_block_match",
            "unreliable_text_page_reset",
        }:
            errors.append(f"text_reset_reason invalid at page {index}")
        if not _valid_repair_policy(row):
            errors.append(f"repair policy combination invalid at page {index}")
        _validate_style_contract(
            project.root,
            row,
            index,
            expected_names,
            input_pages,
            errors,
        )
        _validate_hashed_project_file(
            project.root, row, "candidate_path", "candidate_sha256", "candidate", index, errors
        )
        _validate_hashed_project_file(
            project.root,
            row,
            "text_snapshot_path",
            "text_snapshot_sha256",
            "text snapshot",
            index,
            errors,
        )
        _validate_external_candidate(row, index, errors)
        visual_review = row.get("visual_review")
        if not isinstance(visual_review, dict):
            errors.append(f"visual_review missing at page {index}")
            visual_review = {}
        if (
            visual_review.get("status") != "passed"
            or visual_review.get("full_size") is not True
            or not _nonempty_string(visual_review.get("reviewer"))
        ):
            errors.append(f"visual_review not passed full-size with reviewer/time at page {index}")
        if not _nonempty_string(visual_review.get("reviewer")):
            errors.append(f"visual_review reviewer missing at page {index}")
        visual_time = _parse_zoned_datetime(visual_review.get("reviewed_at"))
        if visual_time is None:
            errors.append(f"visual_review reviewed_at must be timezone-aware ISO8601 at page {index}")
        _validate_checks(visual_review, VISUAL_REVIEW_CHECKS, "visual_review", index, errors)

        page_qa = row.get("page_qa")
        if not isinstance(page_qa, dict):
            errors.append(f"page_qa missing at page {index}")
            page_qa = {}
        if page_qa.get("status") != "passed" or not _nonempty_string(page_qa.get("pass_id")):
            errors.append(f"page/batch QA page_qa not passed with pass_id/time at page {index}")
        if not _nonempty_string(page_qa.get("reviewer")):
            errors.append(f"page_qa reviewer missing at page {index}")
        page_time = _parse_zoned_datetime(page_qa.get("reviewed_at"))
        page_qa_times[output_name] = page_time
        if page_time is None:
            errors.append(f"page_qa reviewed_at must be timezone-aware ISO8601 at page {index}")
        page_pass_id = page_qa.get("pass_id")
        if _nonempty_string(page_pass_id):
            if page_pass_id in seen_pass_ids:
                errors.append(f"pass_id must be globally unique: {page_pass_id!r} reused by page_qa page {index}")
            else:
                seen_pass_ids[page_pass_id] = f"page_qa page {index}"
        _validate_checks(page_qa, PAGE_QA_CHECKS, "page_qa", index, errors)

    batches = repair.get("batches") if repair is not None else None
    if not isinstance(batches, list):
        errors.append("repair log batches must be a list")
        batches = []
    canonical_batch_sizes = expected_batch_sizes(expected_count)
    if len(batches) != expected_batch_count(expected_count):
        errors.append(
            f"batch count mismatch: expected {expected_batch_count(expected_count)}, found {len(batches)}"
        )
    all_members: list[str] = []
    membership_batch: dict[str, str] = {}
    seen_batch_ids: set[str] = set()
    cursor = 0
    for batch_position, batch in enumerate(batches, start=1):
        if not isinstance(batch, dict):
            errors.append(f"batch {batch_position} must be an object")
            continue
        batch_id = batch.get("batch_id")
        if not _nonempty_string(batch_id) or batch_id in seen_batch_ids:
            errors.append(f"batch {batch_position} batch_id missing or duplicate")
            batch_id = f"invalid-batch-{batch_position}"
        seen_batch_ids.add(batch_id)
        members = batch.get("member_pages")
        if not _string_list(members, nonempty=True):
            errors.append(f"batch membership invalid for {batch_id}")
            members = []
        if not 1 <= len(members) <= 12:
            errors.append(f"batch membership size invalid for {batch_id}")
        if batch_position <= len(canonical_batch_sizes) and len(members) != canonical_batch_sizes[batch_position - 1]:
            errors.append(f"batch size does not match deterministic partition for {batch_id}")
        expected_members = expected_names[cursor : cursor + len(members)]
        if members != expected_members:
            errors.append(f"batch membership order/coverage invalid for {batch_id}")
        expected_before = expected_names[max(0, cursor - 2) : cursor]
        expected_after = expected_names[cursor + len(members) : cursor + len(members) + 2]
        if batch.get("context_before") != expected_before or batch.get("context_after") != expected_after:
            errors.append(f"batch context invalid for {batch_id}")
        for member in members:
            if member in membership_batch:
                errors.append(f"batch membership duplicate page {member}")
            membership_batch[member] = batch_id
        all_members.extend(members)
        cursor += len(members)
        batch_qa = batch.get("batch_qa")
        if not isinstance(batch_qa, dict):
            errors.append(f"batch_qa missing for {batch_id}")
            batch_qa = {}
        if batch_qa.get("status") != "passed" or not _nonempty_string(batch_qa.get("pass_id")):
            errors.append(f"page/batch QA batch_qa not passed with pass_id/time for {batch_id}")
        if not _nonempty_string(batch_qa.get("reviewer")):
            errors.append(f"batch_qa reviewer missing for {batch_id}")
        batch_time = _parse_zoned_datetime(batch_qa.get("reviewed_at"))
        if batch_time is None:
            errors.append(f"batch_qa reviewed_at must be timezone-aware ISO8601 for {batch_id}")
        batch_pass_id = batch_qa.get("pass_id")
        if _nonempty_string(batch_pass_id):
            if batch_pass_id in seen_pass_ids:
                errors.append(f"pass_id must be globally unique: {batch_pass_id!r} reused by batch_qa {batch_id}")
            else:
                seen_pass_ids[batch_pass_id] = f"batch_qa {batch_id}"
        _validate_checks(batch_qa, BATCH_QA_CHECKS, "batch_qa", batch_position, errors)
        member_times = [page_qa_times.get(member) for member in members]
        if (
            batch_time is not None
            and member_times
            and all(value is not None for value in member_times)
            and batch_time <= max(value for value in member_times if value is not None)
        ):
            errors.append(f"batch_qa reviewed_at must be strictly later than all page_qa times for {batch_id}")
    if all_members != expected_names:
        errors.append("batch membership has duplicate or missing pages")
    for output_name in expected_names:
        if page_batch_refs.get(output_name) != membership_batch.get(output_name):
            errors.append(f"batch_id reference mismatch for page {output_name}")

    report_path = evidence / "FINAL_QA_REPORT.md"
    if not report_path.is_file():
        errors.append("final QA report missing")
    else:
        try:
            report = report_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"final QA report unreadable: {exc}")
        else:
            report_values: dict[str, str] = {}
            parsed_keys: list[str] = []
            title_seen = False
            format_invalid = False
            for line in report.splitlines():
                if not line.strip():
                    continue
                if not title_seen:
                    if line.strip() != "# FINAL QA REPORT":
                        format_invalid = True
                    title_seen = True
                    continue
                match = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
                if match is None:
                    format_invalid = True
                    continue
                key, value = match.groups()
                parsed_keys.append(key)
                if key in report_values:
                    format_invalid = True
                report_values[key] = value
            if not title_seen or tuple(parsed_keys) != FINAL_REPORT_KEYS or len(parsed_keys) != 10:
                format_invalid = True
            if format_invalid:
                errors.append("final QA report format invalid")
            if report_values.get("status") != "passed":
                errors.append("final QA report status is not passed")
            if not _nonempty_string(report_values.get("reviewer")):
                errors.append("final QA report reviewer missing")
            if _parse_zoned_datetime(report_values.get("reviewed_at")) is None:
                errors.append("final QA report reviewed_at invalid")
            expected_report_numbers = {
                "input_count": counts["input"],
                "output_count": counts["output"],
                "page_qa_passed": expected_count,
                "batch_count": expected_batch_count(expected_count),
                "batch_qa_passed": expected_batch_count(expected_count),
                "blocking_issues": 0,
                "unresolved_issues": 0,
            }
            for key, expected_value in expected_report_numbers.items():
                try:
                    actual_value = int(report_values.get(key, ""))
                except ValueError:
                    actual_value = None
                if actual_value != expected_value:
                    errors.append(
                        f"final QA report {key} mismatch: expected {expected_value}, found {report_values.get(key)!r}"
                    )
    return {"ok": not errors, "errors": errors, "counts": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Project root")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="Evidence directory")
    parser.add_argument(
        "--expected-count",
        type=int,
        help="Optional required page count; defaults to decodable JPG/JPEG input count",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)
    try:
        result = validate_project(args.root, args.evidence_dir, args.expected_count)
    except (OSError, ValueError) as exc:
        result = {
            "ok": False,
            "errors": [str(exc)],
            "counts": {
                "expected": args.expected_count or 0,
                "input": 0,
                "output": 0,
                "manifest": 0,
            },
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"PASS: {result['counts']['output']} output pages")
    else:
        print("FAIL")
        for error in result["errors"]:
            print(f"- {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
