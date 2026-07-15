"""Build deterministic repair manifests using Python 3.11 + Pillow."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

from pipeline_contracts import canonical_hash, make_output_names
from failure_learning import new_failure_store
from task_queue import new_queue, queue_registry_hash
from pipeline_version import EVIDENCE_PIPELINE_ID, EVIDENCE_SCHEMA_VERSION
from project_common import (
    atomic_write_json,
    discover_project,
    ensure_safe_subpath,
    inventory_reference_images,
    sha256_file,
    sorted_input_pages,
)


EVIDENCE_FILES = (
    "comic_run_manifest.json",
    "continuity_bible.json",
    "novel_alignment.json",
    "repair_log.json",
    "FINAL_QA_REPORT.md",
    "scene_clusters.json",
    "style_reference_packs.json",
    "task_queue.json",
    "failure_learning.json",
    "scene_cluster_qa.json",
    "entity_state_timeline.json",
    "page_audit.json",
    "review_events.jsonl",
    "text_geometry.json",
    "regression_summary.json",
)
FINAL_REPORT_KEYS = (
    "status",
    "reviewer",
    "reviewed_at",
    "input_count",
    "output_count",
    "page_qa_passed",
    "batch_count",
    "batch_qa_passed",
    "blocking_issues",
    "unresolved_issues",
)

VISUAL_REVIEW_CHECKS = (
    "original_candidate_comparison",
    "full_size_text",
    "full_size_artifacts",
    "reference_character_match",
    "novel_scene_match",
)
VISUAL_EDIT_MODES = (
    "not_applicable",
    "localized_inpaint",
    "localized_regeneration",
    "full_page_regeneration",
)
VISUAL_ISSUE_EXTENTS = ("not_applicable", "localized", "page_wide")
STYLE_REVIEW_CHECKS = (
    "unchanged_regions_preserved",
    "panel_geometry_preserved",
    "line_weight_brush_match",
    "color_texture_match",
    "face_simplification_match",
    "detail_density_match",
    "adjacent_comic_style_match",
    "character_reference_identity_only",
    "no_rendering_upgrade",
)
PAGE_QA_CHECKS = (
    "novel_fidelity", "character_identity", "skin_hair_crown",
    "anatomy_hands_face", "clothing_props", "extra_continuity",
    "scene_axis_time_weather", "text_accuracy_speaker",
    "text_density_contrast_layout", "sfx", "residual_text",
    "artifact_damage", "style_match", "mobile_readability",
)
BATCH_QA_CHECKS = (
    "story_order", "cross_page_character", "cross_page_extras",
    "cross_page_clothing_props", "cross_page_scene_axis",
    "cross_page_injuries_state", "text_sequence_speaker",
    "boundary_context", "page_count_order", "unresolved_issues_zero",
)
CONTINUITY_CATEGORIES = ("character", "extra", "clothing", "prop", "scene")
CONTINUITY_SOURCE_TYPES = (
    "novel",
    "reference",
    "adjacent_page",
    "established_page",
    "inference",
)
CONTINUITY_LOCK_FIELDS = frozenset(
    {
        "lock_id", "category", "subject", "attribute", "value", "source_type",
        "source_ref", "applies_to_pages", "confidence", "created_by", "created_at",
        "confirmed", "reviewer", "reviewed_at",
    }
)
PIPELINE_MODE = EVIDENCE_PIPELINE_ID
SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION


def partition_batch_sizes(page_count: int) -> list[int]:
    if (
        not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or not 1 <= page_count <= 9999
    ):
        raise ValueError("page_count must be an integer within 1..9999")
    batch_count = (page_count + 11) // 12
    base, remainder = divmod(page_count, batch_count)
    return [base + 1] * remainder + [base] * (batch_count - remainder)


def expected_batch_sizes(page_count: int) -> list[int]:
    return partition_batch_sizes(page_count)


def expected_batch_count(page_count: int) -> int:
    return len(expected_batch_sizes(page_count))


def _build_batches(output_names: list[str]) -> tuple[list[dict], dict[str, str]]:
    batches: list[dict] = []
    page_to_batch: dict[str, str] = {}
    cursor = 0
    for batch_number, size in enumerate(partition_batch_sizes(len(output_names)), start=1):
        batch_id = f"batch-{batch_number:03d}"
        members = output_names[cursor : cursor + size]
        batch = {
            "batch_id": batch_id,
            "member_pages": members,
            "context_before": output_names[max(0, cursor - 2) : cursor],
            "context_after": output_names[cursor + size : cursor + size + 2],
            "batch_qa": {
                "status": "pending",
                "pass_id": None,
                "reviewer": None,
                "reviewed_at": None,
                "checks": {name: False for name in BATCH_QA_CHECKS},
            },
        }
        batches.append(batch)
        page_to_batch.update({page: batch_id for page in members})
        cursor += size
    return batches, page_to_batch


def _copy_with_fsync(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(source, destination)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _stage_evidence_set(
    staging: Path,
    run_manifest: dict,
    continuity_bible: dict,
    novel_alignment: dict,
    repair_log: dict,
) -> None:
    atomic_write_json(staging / EVIDENCE_FILES[0], run_manifest)
    atomic_write_json(staging / EVIDENCE_FILES[1], continuity_bible)
    atomic_write_json(staging / EVIDENCE_FILES[2], novel_alignment)
    atomic_write_json(staging / EVIDENCE_FILES[3], repair_log)
    _atomic_write_text(
        staging / EVIDENCE_FILES[4],
        "# FINAL QA REPORT\n\nstatus: pending\n\nFinal validation has not been performed.\n",
    )
    queue_document = new_queue()
    queue_document["status"] = "pending"
    queue_document["registry_hash"] = queue_registry_hash(queue_document)
    registry_documents = {
        "scene_clusters.json": {
            "schema_version": "1.0",
            "status": "pending",
            "clusters": [],
        },
        "style_reference_packs.json": {
            "schema_version": "1.0",
            "status": "pending",
            "stable_pages": [],
            "approved_hashes": {},
            "reference_packs": [],
        },
        "task_queue.json": queue_document,
        "failure_learning.json": {
            **new_failure_store(),
            "status": "pending",
        },
        "scene_cluster_qa.json": {
            "schema_version": "1.0",
            "status": "pending",
            "clusters": [],
        },
        "entity_state_timeline.json": {
            "schema_version": "1.0",
            "status": "pending",
            "entities": [],
        },
        "page_audit.json": {
            "schema_version": "1.0",
            "status": "pending",
            "pages": [],
        },
        "text_geometry.json": {
            "schema_version": "1.0",
            "status": "pending",
            "pages": [],
        },
        "regression_summary.json": {
            "schema_version": "1.0",
            "status": "pending",
            "regressions": [],
        },
    }
    for filename, document in registry_documents.items():
        document = dict(document)
        if "registry_hash" not in document:
            document["registry_hash"] = canonical_hash(document)
        atomic_write_json(staging / filename, document)
    _atomic_write_text(staging / "review_events.jsonl", "")


def _commit_evidence_set(evidence: Path, staging: Path) -> None:
    backup: Path | None = None
    preserve_recovery_dirs = False
    try:
        backup = Path(tempfile.mkdtemp(prefix=".manifest-backup-", dir=evidence))
        originals = {name: (evidence / name).is_file() for name in EVIDENCE_FILES}
        for name, existed in originals.items():
            if existed:
                _copy_with_fsync(evidence / name, backup / name)
        try:
            for name in EVIDENCE_FILES:
                os.replace(staging / name, evidence / name)
        except Exception as commit_error:
            rollback_errors: list[OSError] = []
            for name, existed in originals.items():
                target = evidence / name
                restore_temporary = backup / f".restore-{name}"
                try:
                    if existed:
                        _copy_with_fsync(backup / name, restore_temporary)
                        os.replace(restore_temporary, target)
                    elif target.exists():
                        target.unlink()
                except OSError as rollback_error:
                    rollback_errors.append(rollback_error)
                finally:
                    if restore_temporary.exists():
                        restore_temporary.unlink()
            if rollback_errors:
                preserve_recovery_dirs = True
                raise OSError(
                    f"manifest replacement failed; rollback incomplete; "
                    f"backup retained at {backup.resolve()}: "
                    + "; ".join(str(error) for error in rollback_errors)
                ) from commit_error
            raise
    finally:
        if not preserve_recovery_dirs:
            shutil.rmtree(staging, ignore_errors=True)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)


def build_manifests(
    root: Path,
    evidence_dir: Path,
    expected_count: int | None = None,
    force: bool = False,
) -> dict:
    project = discover_project(root)
    evidence = ensure_safe_subpath(project.root, evidence_dir)
    pages = sorted_input_pages(project.input_dir)
    actual_count = len(pages)
    if not 1 <= actual_count <= 9999:
        raise ValueError(f"input page count must be within 1..9999, found {actual_count}")
    if expected_count is None:
        expected_count = actual_count
    if not isinstance(expected_count, int) or isinstance(expected_count, bool):
        raise ValueError("expected_count must be an integer")
    if not 1 <= expected_count <= 9999:
        raise ValueError("expected_count must be within 1..9999")
    reference_files = inventory_reference_images(project.references)
    if actual_count != expected_count:
        raise ValueError(f"input page count mismatch: expected {expected_count}, found {actual_count}")
    existing = [name for name in EVIDENCE_FILES if (evidence / name).exists()]
    if existing and not force:
        raise FileExistsError(f"evidence already exists: {', '.join(existing)}")

    input_names = [page.relative_to(project.input_dir).as_posix() for page in pages]
    output_names = make_output_names(input_names)
    page_rows = [
        {
            "index": index,
            "input_name": input_names[index - 1],
            "input_sha256": sha256_file(page),
            "output_name": output_names[index - 1],
            "output_sha256": None,
            "continuity_lock_ids": [],
            "cluster_id": None,
            "reference_pack_id": None,
            "task_id": None,
            "failure_rule_ids": [],
            "page_class": None,
            "generated_by": None,
            "reviewed_by": None,
            "state": "inventoried",
        }
        for index, page in enumerate(pages, start=1)
    ]
    run_manifest = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_mode": PIPELINE_MODE,
        "evidence_files": list(EVIDENCE_FILES),
        "status": "inventoried",
        "expected_count": expected_count,
        "novel_name": project.novel.name,
        "novel_sha256": sha256_file(project.novel),
        "pages": page_rows,
    }
    continuity_bible = {
        "status": "inventoried",
        "reviewer": None,
        "reviewed_at": None,
        "source_priority": list(CONTINUITY_SOURCE_TYPES),
        "coverage": {
            category: {"status": "pending", "rationale": None, "lock_ids": []}
            for category in CONTINUITY_CATEGORIES
        },
        "locks": [],
        "reference_dir": project.references.name,
        "reference_files": reference_files,
        "notes": [],
    }
    novel_alignment = {
        "status": "inventoried",
        "novel_name": project.novel.name,
        "novel_sha256": sha256_file(project.novel),
        "pages": [
            {
                "index": row["index"],
                "output_name": row["output_name"],
                "status": "unconfirmed",
                "evidence": "",
                "confidence": 0.0,
                "novel_sha256": None,
                "chapter": None,
                "start_offset": None,
                "end_offset": None,
                "source_excerpt": None,
                "context_excerpt": None,
                "located_by": None,
                "located_at": None,
                "scene_summary": None,
                "involved_characters": [],
                "dialogue_owners": [],
                "props": [],
                "location": None,
                "story_time": None,
            }
            for row in page_rows
        ],
    }
    repair_log = {
        "schema_version": SCHEMA_VERSION,
        "pipeline_mode": PIPELINE_MODE,
        "evidence_files": list(EVIDENCE_FILES),
        "status": "inventoried",
        "pages": [
            {
                "index": row["index"],
                "output_name": row["output_name"],
                "continuity_lock_ids": [],
                "cluster_id": None,
                "reference_pack_id": None,
                "task_id": None,
                "failure_rule_ids": [],
                "page_class": None,
                "generated_by": None,
                "reviewed_by": None,
                "action": "pending",
                "repair_scope": None,
                "visual_issue_extent": "not_applicable",
                "visual_edit_mode": "not_applicable",
                "style_reference_pages": [],
                "identity_reference_files": [],
                "character_reference_role": "not_applicable",
                "visual_mask_path": None,
                "visual_mask_sha256": None,
                "changed_region_ratio": 0.0,
                "full_page_regeneration_reason": None,
                "style_review": {
                    "status": "not_applicable",
                    "full_size": False,
                    "reviewer": None,
                    "reviewed_at": None,
                    "comparison_path": None,
                    "comparison_sha256": None,
                    "full_page_exception_approved": False,
                    "checks": {name: False for name in STYLE_REVIEW_CHECKS},
                },
                "had_ordinary_text": None,
                "text_recognition": None,
                "candidate_path": None,
                "candidate_sha256": None,
                "text_reset_mode": None,
                "text_reset_reason": None,
                "used_external_text_resource": False,
                "external_candidate": None,
                "text_snapshot_path": None,
                "text_snapshot_sha256": None,
                "visual_review": {
                    "status": "pending",
                    "full_size": False,
                    "reviewer": None,
                    "reviewed_at": None,
                    "checks": {name: False for name in VISUAL_REVIEW_CHECKS},
                },
                "page_qa": {
                    "status": "pending",
                    "pass_id": None,
                    "reviewer": None,
                    "reviewed_at": None,
                    "checks": {name: False for name in PAGE_QA_CHECKS},
                },
            }
            for row in page_rows
        ],
    }
    evidence.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".manifest-staging-", dir=evidence))
    try:
        _stage_evidence_set(
            staging, run_manifest, continuity_bible, novel_alignment, repair_log
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    _commit_evidence_set(evidence, staging)
    return run_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Project root")
    parser.add_argument("--evidence-dir", type=Path, required=True, help="Evidence directory")
    parser.add_argument(
        "--expected-count",
        type=int,
        help="Optional required page count; defaults to the decodable input count",
    )
    parser.add_argument("--force", action="store_true", help="Replace existing evidence files")
    args = parser.parse_args(argv)
    try:
        result = build_manifests(args.root, args.evidence_dir, args.expected_count, args.force)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"built {len(result['pages'])} page mappings in {args.evidence_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
