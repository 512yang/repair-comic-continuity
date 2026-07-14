"""Build a read-only scene-pipeline migration proposal for a comic project."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from failure_learning import new_failure_store, record_failure
from build_output_manifest import (
    EVIDENCE_FILES,
    PAGE_QA_CHECKS,
    PIPELINE_MODE,
    SCHEMA_VERSION,
    STYLE_REVIEW_CHECKS,
    VISUAL_REVIEW_CHECKS,
    _build_batches,
)
from pipeline_contracts import (
    PAGE_CLASSES,
    canonical_hash,
    make_output_names,
    normalize_page_id,
    normalize_relative_image_path,
)
from project_common import (
    INPUT_PAGE_EXTENSIONS,
    REFERENCE_IMAGE_EXTENSIONS,
    atomic_write_json,
    discover_project,
    inventory_reference_images,
    sha256_file,
    sorted_input_pages,
)
from scene_clusters import bind_reference_pack, build_reference_pack, cluster_size_controls
from task_queue import add_task, new_queue, queue_metrics


APPLY_CONFIRM_TOKEN = "APPLY_SCENE_PIPELINE_V3"
DEFAULT_PREPARED_BY = "codex-migration-preparer"
PROPOSAL_FILENAMES = (
    "scene_clusters.json",
    "style_reference_packs.json",
    "task_queue.json",
    "failure_learning.json",
    "scene_cluster_qa.json",
    "comic_run_manifest.json",
    "repair_log.json",
    "pipeline_metrics.json",
    "pipeline_migration_report.json",
)
SOURCE_EVIDENCE_FILENAMES = (
    "inventory.json",
    "comic_run_manifest.json",
    "novel_alignment.json",
    "novel_alignment_candidates.json",
    "repair_log.json",
    "continuity_bible.json",
    "ocr_snapshot.json",
)
LOCKED_EVIDENCE_FILENAMES = tuple(
    dict.fromkeys(
        (
            "inventory.json",
            "novel_alignment_candidates.json",
            "ocr_snapshot.json",
            *EVIDENCE_FILES,
        )
    )
)
REGISTRY_FILENAMES = (
    "scene_clusters.json",
    "style_reference_packs.json",
    "task_queue.json",
    "failure_learning.json",
    "scene_cluster_qa.json",
)
REPLACEABLE_EXISTING = frozenset({"comic_run_manifest.json", "repair_log.json"})
JOURNAL_FILENAME = ".scene_pipeline_migration_journal.json"
_PAGE_STEM = re.compile(r"(?P<base>\d+)(?:\((?P<variant>\d+)\))?\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PAGE_CLASS_TO_ACTION = {
    "unchanged": "unchanged",
    "text_only": "text_only",
    "full_page_redraw": "full_page_regeneration",
    "evidence_blocked": "evidence_blocked",
}


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        current = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        current = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("now must not be empty")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            current = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("now must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError("now must be a datetime, ISO-8601 string, or None")
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must include a timezone")
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _file_records(root: Path, paths: Iterable[Path], kind: str) -> list[dict[str, str]]:
    return [
        {"path": _relative(root, path), "role": kind, "sha256": sha256_file(path)}
        for path in paths
    ]


def _snapshot_directory(root: Path, directory: Path) -> list[dict[str, str]]:
    if not directory.is_dir():
        return []
    return _file_records(
        root,
        sorted((path for path in directory.rglob("*") if path.is_file()), key=lambda p: p.as_posix().casefold()),
        "output_snapshot",
    )


def _supported_output_count(records: Iterable[Mapping[str, str]]) -> int:
    return sum(
        1
        for record in records
        if Path(record["path"]).suffix.casefold() in INPUT_PAGE_EXTENSIONS
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_artifact_dir(project: Any, artifact_dir: Path | str) -> Path:
    target = Path(artifact_dir).expanduser().resolve()
    protected_trees = (
        project.input_dir.resolve(),
        project.output_dir.resolve(),
        project.references.resolve(),
    )
    if any(_is_within(target, protected) for protected in protected_trees):
        raise ValueError("artifact_dir must not be inside an image or output tree")
    evidence = (project.root / "evidence").resolve()
    if target == project.root.resolve() or target == evidence:
        raise ValueError("artifact_dir must be an independent proposal directory")
    if _is_within(target, evidence):
        if target.parent != evidence or re.fullmatch(
            r"(?:skill_validation_|dry_run_)[A-Za-z0-9._-]*", target.name
        ) is None:
            raise ValueError(
                "artifact_dir under evidence must be a direct approved dry-run directory"
            )
    return target


def _snapshot_images(
    root: Path, artifact_dir: Path | None = None
) -> list[dict[str, str]]:
    resolved_root = root.resolve()
    excluded = artifact_dir.resolve() if artifact_dir is not None else None
    images = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.suffix.casefold() not in REFERENCE_IMAGE_EXTENSIONS:
            continue
        resolved = path.resolve()
        if excluded is not None and _is_within(resolved, excluded):
            continue
        images.append(
            {
                "path": resolved.relative_to(resolved_root).as_posix(),
                "sha256": sha256_file(resolved),
            }
        )
    return images


def _snapshot_difference_count(
    before: Iterable[Mapping[str, str]], after: Iterable[Mapping[str, str]]
) -> int:
    before_map = {item["path"]: item["sha256"] for item in before}
    after_map = {item["path"]: item["sha256"] for item in after}
    return sum(
        before_map.get(path) != after_map.get(path)
        for path in set(before_map) | set(after_map)
    )


def _with_registry_hash(document: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(document))
    result.pop("registry_hash", None)
    result["registry_hash"] = canonical_hash(result)
    return result


def _report_integrity_hash(report: Mapping[str, Any]) -> str:
    payload = copy.deepcopy(dict(report))
    payload.pop("report_integrity_hash", None)
    return canonical_hash(payload)


def _seal_report(report: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(report))
    result["report_integrity_hash"] = _report_integrity_hash(result)
    return result


def _validate_report_integrity(report: Mapping[str, Any]) -> None:
    recorded = report.get("report_integrity_hash")
    if not isinstance(recorded, str) or _SHA256.fullmatch(recorded) is None:
        raise ValueError("report integrity hash missing or malformed")
    if recorded != _report_integrity_hash(report):
        raise ValueError("report integrity hash mismatch")


def _confirmed_report_body(report: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(report))
    for key in (
        "migration_id",
        "report_integrity_hash",
        "confirmed_report_digest",
    ):
        result.pop(key, None)
    return result


def _confirmed_report_digest(report: Mapping[str, Any]) -> str:
    return canonical_hash(_confirmed_report_body(report))


def _validate_confirmed_report_digest(report: Mapping[str, Any]) -> None:
    recorded = report.get("confirmed_report_digest")
    if not isinstance(recorded, str) or _SHA256.fullmatch(recorded) is None:
        raise ValueError("confirmed report digest missing or malformed")
    if recorded != _confirmed_report_digest(report):
        raise ValueError("confirmed report digest mismatch")


def _capture_source_snapshot(
    project: Any, evidence_dir: Path
) -> tuple[list[Path], list[str], list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    """Re-discover and hash every migration source without reusing prior hashes."""
    pages = sorted_input_pages(project.input_dir)
    if not pages:
        raise ValueError("input must contain at least one supported image page")
    input_names = [page.relative_to(project.input_dir).as_posix() for page in pages]
    for page in pages:
        _validate_page_range(page)
    page_ids = [_migration_page_id(input_name) for input_name in input_names]
    if len(page_ids) != len(set(page_ids)):
        raise ValueError("duplicate page identity")
    inventory_reference_images(project.references)
    reference_paths = sorted(
        (
            path
            for path in project.references.iterdir()
            if path.is_file() and path.suffix.casefold() in REFERENCE_IMAGE_EXTENSIONS
        ),
        key=lambda path: path.name.casefold(),
    )
    input_records = _file_records(project.root, pages, "input_page")
    reference_records = _file_records(
        project.root, reference_paths, "identity_reference"
    )
    evidence_paths = [
        evidence_dir / filename
        for filename in LOCKED_EVIDENCE_FILENAMES
        if (evidence_dir / filename).is_file()
    ]
    source_records = (
        _file_records(project.root, [project.novel], "novel")
        + input_records
        + reference_records
        + _file_records(project.root, evidence_paths, "evidence_source")
    )
    return pages, page_ids, input_records, reference_records, source_records


def _load_evidence(
    root: Path, evidence_dir: Path
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, Any]]]:
    documents: dict[str, Any] = {}
    records: list[dict[str, str]] = []
    blockers: list[dict[str, Any]] = []
    for filename in SOURCE_EVIDENCE_FILENAMES:
        path = evidence_dir / filename
        if not path.is_file():
            continue
        records.extend(_file_records(root, [path], "evidence_source"))
        try:
            raw = path.read_bytes()
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                text = raw.decode("utf-16")
            else:
                text = raw.decode("utf-8-sig")
            documents[filename] = json.loads(text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            blockers.append(
                {
                    "code": "EVIDENCE_JSON_UNREADABLE",
                    "path": _relative(root, path),
                    "detail": str(exc),
                }
            )
    return documents, records, blockers


def _validate_page_range(page: Path) -> str:
    normalized = unicodedata.normalize("NFKC", page.stem).strip()
    match = _PAGE_STEM.fullmatch(normalized)
    if match is None:
        raise ValueError(f"input page name must be numeric and within 1..9999: {page.name}")
    base = int(match.group("base"))
    variant = int(match.group("variant")) if match.group("variant") else None
    if not 1 <= base <= 9999 or (variant is not None and not 1 <= variant <= 9999):
        raise ValueError(f"input page number must be within 1..9999: {page.name}")
    return normalize_page_id(page.name)


def _migration_page_id(relative_path: object) -> str:
    """Bind a task/page identity to the exact relative source path."""
    normalized = normalize_relative_image_path(relative_path)
    if "/" not in normalized:
        return normalize_page_id(normalized)
    return "P" + canonical_hash({"relative_page": normalized})


def _rows(document: object) -> list[Mapping[str, Any]]:
    if not isinstance(document, Mapping):
        return []
    pages = document.get("pages")
    if not isinstance(pages, list):
        return []
    return [row for row in pages if isinstance(row, Mapping)]


def _index_rows(
    rows: Iterable[Mapping[str, Any]], label: str
) -> tuple[dict[str, Mapping[str, Any]], dict[int, Mapping[str, Any]]]:
    exact: dict[str, Mapping[str, Any]] = {}
    indexes: dict[int, Mapping[str, Any]] = {}
    for row in rows:
        for field in ("input_name", "output_name"):
            name = row.get(field)
            if not isinstance(name, str) or not name.strip():
                continue
            if name in exact and exact[name] is not row:
                raise ValueError(f"duplicate exact name in {label}: {name!r}")
            exact[name] = row
        row_index = row.get("index")
        if isinstance(row_index, int) and not isinstance(row_index, bool):
            if row_index in indexes and indexes[row_index] is not row:
                raise ValueError(f"duplicate page index in {label}: {row_index}")
            indexes[row_index] = row
    return exact, indexes


def _page_row(
    lookup: tuple[dict[str, Mapping[str, Any]], dict[int, Mapping[str, Any]]],
    index: int,
    input_name: str,
    output_name: str,
) -> Mapping[str, Any] | None:
    exact, indexes = lookup
    if input_name in exact:
        return exact[input_name]
    if output_name in exact:
        return exact[output_name]
    return indexes.get(index)


def _alignment_state(row: Mapping[str, Any] | None) -> str:
    if row is None:
        return "unresolved"
    status = str(row.get("status", "")).strip().casefold()
    if status == "confirmed":
        return "confirmed"
    if status == "provisional":
        return "provisional"
    return "unresolved"


def _scene_values(row: Mapping[str, Any] | None) -> tuple[str, str, str, str]:
    if row is None:
        return ("unknown", "unknown", "unknown", "unknown")
    values = []
    for field in ("chapter", "location", "story_time", "scene_id"):
        value = row.get(field)
        text = str(value).strip() if value is not None else ""
        values.append(text or "unknown")
    return tuple(values)  # type: ignore[return-value]


def _cluster_id(member_pages: list[str], scene_key: tuple[str, ...], state: str) -> str:
    identity = {
        "member_pages": member_pages,
        "scene_key": scene_key,
        "alignment_state": state,
    }
    return f"cluster-{canonical_hash(identity)[:16]}"


def _make_clusters(page_infos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    positions = {page["output_name"]: offset for offset, page in enumerate(page_infos)}
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for page in page_infos:
        if page["alignment_state"] == "unresolved":
            if current:
                chunks.extend(current[offset : offset + 20] for offset in range(0, len(current), 20))
                current = []
            chunks.append([page])
            continue
        if current and (
            current[-1]["alignment_state"] == "unresolved"
            or current[-1]["scene_key"] != page["scene_key"]
        ):
            chunks.extend(current[offset : offset + 20] for offset in range(0, len(current), 20))
            current = []
        current.append(page)
    if current:
        chunks.extend(current[offset : offset + 20] for offset in range(0, len(current), 20))

    ordered_ids = [page["output_name"] for page in page_infos]
    clusters: list[dict[str, Any]] = []
    for chunk in chunks:
        member_pages = [page["output_name"] for page in chunk]
        states = {page["alignment_state"] for page in chunk}
        if "unresolved" in states:
            state = "unresolved"
        elif "provisional" in states:
            state = "provisional"
        else:
            state = "confirmed"
        first = positions[member_pages[0]]
        last = positions[member_pages[-1]]
        scene_key = chunk[0]["scene_key"]
        controls = cluster_size_controls(
            len(page_infos), len(member_pages), member_pages == ordered_ids
        )
        blocker_codes = list(controls["blocker_codes"])
        if state == "unresolved":
            blocker_codes.append("UNRESOLVED_ALIGNMENT")
        elif state == "provisional":
            blocker_codes.append("PROVISIONAL_ALIGNMENT")
        blocked = bool(blocker_codes)
        clusters.append(
            {
                "cluster_id": _cluster_id(member_pages, scene_key, state),
                "member_pages": member_pages,
                "source_pages": [
                    {
                        "output_name": page["output_name"],
                        "input_name": page["input_name"],
                        "source_page_id": page["page_id"],
                        "input_sha256": page["input_sha256"],
                    }
                    for page in chunk
                ],
                "context_before": ordered_ids[max(0, first - 2) : first],
                "context_after": ordered_ids[last + 1 : last + 3],
                "scene_key": list(scene_key),
                "alignment_state": state,
                "reference_pack_id": None,
                "reference_pack_state": "unbound",
                "canary_page": member_pages[(len(member_pages) - 1) // 2],
                **controls,
                "blocked": blocked,
                "blocker_codes": blocker_codes,
            }
        )
    return clusters


def _build_packs(
    root: Path,
    clusters: list[dict[str, Any]],
    page_infos: list[dict[str, Any]],
    reference_records: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pages = {page["output_name"]: page for page in page_infos}
    packs: list[dict[str, Any]] = []
    bound_clusters: list[dict[str, Any]] = []
    for cluster in clusters:
        canary = pages[cluster["canary_page"]]
        style_path = canary["input_path"]
        references: list[dict[str, str]] = [
            {
                "path": style_path,
                "role": "primary_style",
                "sha256": canary["input_sha256"],
            }
        ]
        for page_id in cluster["context_before"] + cluster["context_after"]:
            page = pages[page_id]
            references.append(
                {
                    "path": page["input_path"],
                    "role": "adjacent_style",
                    "sha256": page["input_sha256"],
                }
            )
        references.extend(
            {
                "path": record["path"],
                "role": "identity_only",
                "sha256": record["sha256"],
            }
            for record in reference_records
        )
        pack = build_reference_pack(
            cluster,
            references,
            stable_pages=[style_path],
            contract_version="v3",
        )
        bound_clusters.append(
            bind_reference_pack(cluster, pack, contract_version="v3")
        )
        packs.append(pack)
    return bound_clusters, packs


def _classify_task(
    alignment_state: str, repair_row: Mapping[str, Any] | None
) -> tuple[str, str]:
    if alignment_state == "unresolved":
        return "evidence_blocked", "evidence_resolution"
    raw = None
    if repair_row is not None:
        raw = repair_row.get("page_class") or repair_row.get("action")
    action = str(raw or "").strip().casefold().replace("-", "_")
    if action in {"unchanged", "copy_original", "pass_through", "no_change"}:
        return "unchanged", "continuity_check"
    if action in {"text_only", "text_repair", "block_replace", "page_reset"}:
        return "text_only", "text_repair"
    if action in {
        "full_page_redraw",
        "full_page_regeneration",
        "redraw",
        "regenerate",
    }:
        return "full_page_redraw", "full_page_redraw"
    return "evidence_blocked", "evidence_resolution"


def _make_manifest_proposal(
    project: Any,
    page_infos: list[dict[str, Any]],
    traces: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline_mode": PIPELINE_MODE,
        "evidence_files": list(EVIDENCE_FILES),
        "proposal_only": True,
        "status": "inventoried",
        "expected_count": len(page_infos),
        "novel_name": project.novel.name,
        "novel_sha256": sha256_file(project.novel),
        "pages": [
            {
                "index": page["index"],
                "input_name": page["input_name"],
                "input_sha256": page["input_sha256"],
                "output_name": page["output_name"],
                "output_sha256": None,
                "continuity_lock_ids": [],
                "cluster_id": traces[page["output_name"]]["cluster_id"],
                "reference_pack_id": traces[page["output_name"]]["reference_pack_id"],
                "task_id": traces[page["output_name"]]["task_id"],
                "failure_rule_ids": [],
                "page_class": traces[page["output_name"]]["page_class"],
                "generated_by": None,
                "reviewed_by": None,
                "state": "inventoried",
            }
            for page in page_infos
        ],
    }


def _make_repair_proposal(
    page_infos: list[dict[str, Any]], traces: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    output_names = [page["output_name"] for page in page_infos]
    batches, page_to_batch = _build_batches(output_names)
    pages = []
    for page in page_infos:
        trace = traces[page["output_name"]]
        pages.append(
            {
                "index": page["index"],
                "output_name": page["output_name"],
                "batch_id": page_to_batch[page["output_name"]],
                "continuity_lock_ids": [],
                "cluster_id": trace["cluster_id"],
                "reference_pack_id": trace["reference_pack_id"],
                "task_id": trace["task_id"],
                "failure_rule_ids": [],
                "page_class": trace["page_class"],
                "generated_by": None,
                "reviewed_by": None,
                "action": _PAGE_CLASS_TO_ACTION[trace["page_class"]],
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
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "pipeline_mode": PIPELINE_MODE,
        "evidence_files": list(EVIDENCE_FILES),
        "proposal_only": True,
        "status": "inventoried",
        "batches": batches,
        "pages": pages,
    }


def _migration_identity(
    source_hashes: Mapping[str, str],
    output_snapshot: Iterable[Mapping[str, str]],
    image_snapshot: Iterable[Mapping[str, str]],
    proposal_hashes: Mapping[str, str],
    blockers: Iterable[Mapping[str, Any]],
    ready_for_migration: bool,
    exact_bijection_planned: bool,
    confirmed_report_digest: str,
) -> str:
    return canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "locked_sources": dict(source_hashes),
            "output_snapshot": list(output_snapshot),
            "image_snapshot": list(image_snapshot),
            "proposal_hashes": dict(proposal_hashes),
            "blockers": list(blockers),
            "ready_for_migration": ready_for_migration,
            "exact_bijection_planned": exact_bijection_planned,
            "confirmed_report_digest": confirmed_report_digest,
        }
    )


def _canonical_blockers(blockers: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = [
        json.loads(
            json.dumps(
                dict(blocker),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        for blocker in blockers
    ]
    normalized.sort(
        key=lambda blocker: json.dumps(
            blocker,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return normalized


def _derive_migration_gate(
    project: Any,
    evidence_dir: Path,
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive apply gates from live source evidence and registries, never report flags."""
    _documents, _records, blockers = _load_evidence(project.root, evidence_dir)
    documents = _documents
    manifest = artifacts.get("comic_run_manifest.json")
    scene = artifacts.get("scene_clusters.json")
    run_pages = manifest.get("pages") if isinstance(manifest, Mapping) else None
    clusters = scene.get("clusters") if isinstance(scene, Mapping) else None
    alignment_rows = _rows(documents.get("novel_alignment.json"))
    if not alignment_rows:
        blockers.append(
            {
                "code": "ALIGNMENT_EVIDENCE_MISSING",
                "path": "evidence/novel_alignment.json",
                "detail": "no usable alignment pages; no status will be guessed",
            }
        )
    alignment_lookup = _index_rows(alignment_rows, "novel alignment")
    page_alignment: dict[str, tuple[str, str]] = {}
    unconfirmed: set[str] = set()
    if isinstance(run_pages, list):
        for position, page in enumerate(run_pages, start=1):
            if not isinstance(page, Mapping):
                continue
            input_name = page.get("input_name")
            output_name = page.get("output_name")
            if not isinstance(input_name, str) or not isinstance(output_name, str):
                continue
            alignment = _page_row(
                alignment_lookup, position, input_name, output_name
            )
            state = _alignment_state(alignment)
            try:
                source_page_id = _migration_page_id(input_name)
            except ValueError:
                source_page_id = Path(input_name).stem
            page_alignment[output_name] = (state, source_page_id)
            if state != "confirmed":
                unconfirmed.add(source_page_id)
    cluster_members: list[str] = []
    if isinstance(clusters, list):
        for cluster in clusters:
            if not isinstance(cluster, Mapping):
                continue
            members = cluster.get("member_pages")
            if not isinstance(members, list):
                continue
            cluster_members.extend(member for member in members if isinstance(member, str))
            claimed_state = cluster.get("alignment_state")
            claimed_blocked = cluster.get("blocked") is True or bool(cluster.get("blocker_codes"))
            for member in members:
                source_state, source_page_id = page_alignment.get(
                    member, ("unresolved", str(member))
                )
                if source_state != "confirmed" or claimed_state != "confirmed":
                    unconfirmed.add(source_page_id)
                if claimed_state != source_state or (
                    source_state == "unresolved" and not claimed_blocked
                ):
                    blockers.append(
                        {
                            "code": "CLUSTER_ALIGNMENT_CONTROL_MISMATCH",
                            "page": source_page_id,
                            "detail": "cluster controls contradict live alignment status",
                        }
                    )
    if unconfirmed:
        blockers.append(
            {
                "code": "UNCONFIRMED_ALIGNMENT_PAGES",
                "pages": sorted(unconfirmed),
                "detail": "only confirmed alignment pages may pass migration",
            }
        )
    input_relative_names = [
        page.relative_to(project.input_dir).as_posix()
        for page in sorted_input_pages(project.input_dir)
    ]
    relative_source_ids = {
        _relative_source_stem(name): _migration_page_id(name)
        for name in input_relative_names
    }
    input_page_ids = set(relative_source_ids.values())
    for candidate in _rejected_candidates(evidence_dir):
        if _candidate_page_id(
            candidate,
            input_page_ids,
            evidence_dir=evidence_dir,
            relative_source_ids=relative_source_ids,
        ) is None:
            blockers.append(
                {
                    "code": "REJECTED_EVIDENCE_UNBOUND",
                    "path": _relative(project.root, candidate),
                    "detail": "candidate could not be bound to an input page",
                }
            )
    bundle_errors = _validate_bundle_documents(artifacts)
    blockers.extend(
        {"code": "BUNDLE_VALIDATION_ERROR", "detail": error}
        for error in bundle_errors
    )
    mapped_outputs = [
        page.get("output_name") for page in run_pages if isinstance(page, Mapping)
    ] if isinstance(run_pages, list) else []
    mapped_inputs = [
        page.get("input_name") for page in run_pages if isinstance(page, Mapping)
    ] if isinstance(run_pages, list) else []
    try:
        expected_outputs = make_output_names(mapped_inputs) if mapped_inputs else []
    except ValueError:
        expected_outputs = []
    exact_bijection = (
        bool(run_pages)
        and mapped_outputs == expected_outputs
        and len(set(mapped_inputs)) == len(mapped_inputs) == len(expected_outputs)
        and cluster_members == expected_outputs
        and not bundle_errors
    )
    if not exact_bijection:
        blockers.append(
            {
                "code": "EXACT_BIJECTION_INVALID",
                "detail": "manifest, cluster, and task registries are not a strict bijection",
            }
        )
    canonical = _canonical_blockers(blockers)
    return {
        "blockers": canonical,
        "ready_for_migration": not canonical and exact_bijection,
        "exact_bijection_planned": exact_bijection,
    }


def _relative_source_stem(relative_path: object) -> str:
    normalized = normalize_relative_image_path(relative_path)
    return unicodedata.normalize("NFKC", str(Path(normalized).with_suffix(""))).replace(
        "\\", "/"
    ).casefold()


def _candidate_page_id(
    candidate: Path,
    page_ids: set[str],
    *,
    evidence_dir: Path | None = None,
    relative_source_ids: Mapping[str, str] | None = None,
) -> str | None:
    if evidence_dir is not None and relative_source_ids:
        legacy_root = evidence_dir / "page_candidates"
        try:
            relative_candidate = candidate.relative_to(legacy_root)
        except ValueError:
            pass
        else:
            source_stem = re.sub(
                r"_(?:repaired|candidate|visual|rejected).*$",
                "",
                unicodedata.normalize("NFKC", relative_candidate.stem).strip(),
                flags=re.IGNORECASE,
            )
            relative_key = unicodedata.normalize(
                "NFKC", (relative_candidate.parent / source_stem).as_posix()
            ).casefold()
            explicit_page_id = relative_source_ids.get(relative_key)
            if explicit_page_id is not None:
                return explicit_page_id

    candidates = [candidate.stem]
    candidates.extend(parent.name for parent in candidate.parents)
    for raw in candidates:
        normalized = unicodedata.normalize("NFKC", raw).strip()
        normalized = re.sub(
            r"_(?:repaired|candidate|visual|rejected).*$",
            "",
            normalized,
            flags=re.IGNORECASE,
        )
        variant = re.match(r"^(\d+)_(\d+)(?:_|$)", normalized)
        if variant:
            normalized = f"{variant.group(1)}({variant.group(2)})"
        else:
            numeric = re.match(r"^(\d+(?:\(\d+\))?)", normalized)
            if numeric:
                normalized = numeric.group(1)
        if normalized in page_ids:
            return normalized
    return None


def _rejected_candidates(evidence_dir: Path) -> list[Path]:
    candidates: set[Path] = set()
    legacy = evidence_dir / "page_candidates"
    if legacy.is_dir():
        candidates.update(
            path
            for path in legacy.rglob("*")
            if path.is_file() and path.suffix.casefold() in REFERENCE_IMAGE_EXTENSIONS
        )
    for path in evidence_dir.rglob("*"):
        if (
            path.is_file()
            and path.suffix.casefold() in REFERENCE_IMAGE_EXTENSIONS
            and "rejected" in path.as_posix().casefold()
        ):
            candidates.add(path)
    return sorted(candidates, key=lambda path: path.as_posix().casefold())


def _proposal_hashes(artifacts: Mapping[str, Any]) -> dict[str, str]:
    return {
        filename: canonical_hash(artifacts[filename])
        for filename in PROPOSAL_FILENAMES
        if filename != "pipeline_migration_report.json"
    }


def _source_digest_map(records: Iterable[Mapping[str, str]]) -> dict[str, str]:
    return {record["path"]: record["sha256"] for record in records}


def _write_proposals(
    artifact_dir: Path,
    artifacts: Mapping[str, Any],
    *,
    require_clean_directory: bool = True,
    filenames: Iterable[str] = PROPOSAL_FILENAMES,
) -> None:
    artifact_dir = artifact_dir.expanduser().resolve()
    if artifact_dir.exists() and not artifact_dir.is_dir():
        raise ValueError(f"artifact_dir is not a directory: {artifact_dir}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    unexpected = [
        path.name
        for path in artifact_dir.iterdir()
        if path.is_file() and path.name not in PROPOSAL_FILENAMES
    ]
    if require_clean_directory and unexpected:
        raise ValueError(f"artifact_dir contains unrelated files: {sorted(unexpected)!r}")
    selected = tuple(filenames)
    if any(filename not in PROPOSAL_FILENAMES for filename in selected):
        raise ValueError("proposal filename is not allowlisted")
    for filename in selected:
        atomic_write_json(artifact_dir / filename, artifacts[filename])


def migrate_project(
    project_root: Path | str,
    *,
    dry_run: bool = True,
    artifact_dir: Path | str | None = None,
    now: datetime | str | None = None,
    prepared_by: str = DEFAULT_PREPARED_BY,
) -> dict[str, Any]:
    """Discover a project and optionally write proposal-only JSON artifacts."""
    if dry_run is not True:
        raise ValueError(
            "migrate_project is proposal-only; use apply_project with the confirm token"
        )
    prepared_by = str(prepared_by).strip()
    if not prepared_by:
        raise ValueError("prepared_by must be non-empty")
    generated_at = _timestamp(now)
    project = discover_project(Path(project_root))
    evidence_dir = project.root / "evidence"
    resolved_artifact_dir = (
        _validate_artifact_dir(project, artifact_dir)
        if artifact_dir is not None
        else None
    )
    image_before = _snapshot_images(project.root, resolved_artifact_dir)
    (
        pages,
        page_ids,
        input_records,
        reference_source_records,
        source_records,
    ) = _capture_source_snapshot(project, evidence_dir)
    documents, _evidence_records, blockers = _load_evidence(
        project.root, evidence_dir
    )
    source_hashes_before = _source_digest_map(source_records)
    source_lock_hash = canonical_hash(source_hashes_before)
    output_before = _snapshot_directory(project.root, project.output_dir)
    output_count_before = _supported_output_count(output_before)

    alignment_rows = _rows(documents.get("novel_alignment.json"))
    repair_rows = _rows(documents.get("repair_log.json"))
    alignment_lookup = _index_rows(alignment_rows, "novel alignment")
    repair_lookup = _index_rows(repair_rows, "repair log")
    if not alignment_rows:
        blockers.append(
            {
                "code": "ALIGNMENT_EVIDENCE_MISSING",
                "path": "evidence/novel_alignment.json",
                "detail": "no usable alignment pages; no status will be guessed",
            }
        )

    input_names = [page.relative_to(project.input_dir).as_posix() for page in pages]
    output_names = make_output_names(input_names)
    page_infos: list[dict[str, Any]] = []
    for index, (page, page_id, input_name, output_name, input_record) in enumerate(
        zip(pages, page_ids, input_names, output_names, input_records), start=1
    ):
        alignment = _page_row(alignment_lookup, index, input_name, output_name)
        page_infos.append(
            {
                "index": index,
                "page_id": page_id,
                "input_name": input_name,
                "input_path": input_record["path"],
                "input_sha256": input_record["sha256"],
                "output_name": output_name,
                "alignment_state": _alignment_state(alignment),
                "scene_key": _scene_values(alignment),
                "repair_row": _page_row(repair_lookup, index, input_name, output_name),
            }
        )

    unresolved_pages = [
        page["page_id"]
        for page in page_infos
        if page["alignment_state"] == "unresolved"
    ]
    unconfirmed_pages = [
        page["page_id"]
        for page in page_infos
        if page["alignment_state"] != "confirmed"
    ]
    if unconfirmed_pages:
        blockers.append(
            {
                "code": "UNCONFIRMED_ALIGNMENT_PAGES",
                "pages": unconfirmed_pages,
                "detail": "only confirmed alignment pages may pass migration",
            }
        )

    clusters = _make_clusters(page_infos)
    clusters, packs = _build_packs(
        project.root, clusters, page_infos, reference_source_records
    )
    cluster_by_page = {
        page_id: cluster
        for cluster in clusters
        for page_id in cluster["member_pages"]
    }
    pack_by_id = {pack["reference_pack_id"]: pack for pack in packs}

    scene_doc = _with_registry_hash({
        "schema_version": SCHEMA_VERSION,
        "proposal_only": True,
        "generated_at": generated_at,
        "source_lock_hash": source_lock_hash,
        "clusters": clusters,
    })
    stable_pages = sorted(
        {
            reference["path"]
            for pack in packs
            for reference in pack["references"]
            if reference["role"] == "primary_style"
        }
    )
    approved_hashes = {
        reference["path"]: reference["sha256"]
        for pack in packs
        for reference in pack["references"]
    }
    pack_doc = _with_registry_hash({
        "schema_version": SCHEMA_VERSION,
        "proposal_only": True,
        "generated_at": generated_at,
        "source_lock_hash": source_lock_hash,
        "stable_pages": stable_pages,
        "approved_hashes": dict(sorted(approved_hashes.items())),
        "reference_packs": packs,
    })

    queue = new_queue()
    queue.update(
        {
            "proposal_only": True,
            "generated_at": generated_at,
            "source_lock_hash": source_lock_hash,
        }
    )
    page_plans: list[dict[str, Any]] = []
    for page in page_infos:
        cluster = cluster_by_page[page["output_name"]]
        pack = pack_by_id[cluster["reference_pack_id"]]
        page_class, task_type = _classify_task(
            page["alignment_state"], page["repair_row"]
        )
        if cluster["blocked"] and not cluster["boundary_exception"]:
            page_class, task_type = "evidence_blocked", "evidence_resolution"
        payload_hash = canonical_hash(
            {
                "page_id": page["output_name"],
                "page_class": page_class,
                "source_sha256": page["input_sha256"],
                "cluster_id": cluster["cluster_id"],
                "reference_binding_hash": pack["reference_binding_hash"],
            }
        )
        page_plans.append(
            {
                "page": page,
                "cluster": cluster,
                "pack": pack,
                "page_class": page_class,
                "task_type": task_type,
                "payload_hash": payload_hash,
            }
        )

    task_by_output: dict[str, dict[str, Any]] = {}
    canary_task_by_cluster: dict[str, str] = {}
    for plan in page_plans:
        page = plan["page"]
        cluster = plan["cluster"]
        if page["output_name"] != cluster["canary_page"]:
            continue
        task = add_task(
            queue,
            page_id=_migration_page_id(page["output_name"]),
            task_type=plan["task_type"],
            payload_hash=plan["payload_hash"],
            cluster_id=cluster["cluster_id"],
            prompt_reference_hash=plan["pack"]["reference_binding_hash"],
            now=generated_at,
            is_canary=True,
        )
        task["page_class"] = plan["page_class"]
        task_by_output[page["output_name"]] = task
        canary_task_by_cluster[cluster["cluster_id"]] = task["task_id"]

    for plan in page_plans:
        page = plan["page"]
        cluster = plan["cluster"]
        output_name = page["output_name"]
        if output_name in task_by_output:
            continue
        task = add_task(
            queue,
            page_id=_migration_page_id(output_name),
            task_type=plan["task_type"],
            payload_hash=plan["payload_hash"],
            cluster_id=cluster["cluster_id"],
            prompt_reference_hash=plan["pack"]["reference_binding_hash"],
            now=generated_at,
            canary_task_id=(
                canary_task_by_cluster[cluster["cluster_id"]]
                if plan["task_type"] == "full_page_redraw"
                else None
            ),
        )
        task["page_class"] = plan["page_class"]
        task_by_output[output_name] = task

    queue["tasks"] = [task_by_output[plan["page"]["output_name"]] for plan in page_plans]
    traces: dict[str, dict[str, Any]] = {}
    for plan in page_plans:
        page = plan["page"]
        cluster = plan["cluster"]
        pack = plan["pack"]
        task = task_by_output[page["output_name"]]
        traces[page["output_name"]] = {
            "cluster_id": cluster["cluster_id"],
            "reference_pack_id": pack["reference_pack_id"],
            "task_id": task["task_id"],
            "page_class": plan["page_class"],
        }
    queue = _with_registry_hash(queue)

    failure_store = new_failure_store()
    failure_store.update(
        {
            "proposal_only": True,
            "generated_at": generated_at,
            "source_lock_hash": source_lock_hash,
        }
    )
    page_id_set = set(page_ids)
    relative_source_ids = {
        _relative_source_stem(page["input_name"]): page["page_id"]
        for page in page_infos
    }
    source_to_output = {
        page["page_id"]: page["output_name"] for page in page_infos
    }
    for candidate in _rejected_candidates(evidence_dir):
        source_page_id = _candidate_page_id(
            candidate,
            page_id_set,
            evidence_dir=evidence_dir,
            relative_source_ids=relative_source_ids,
        )
        if source_page_id is None:
            blockers.append(
                {
                    "code": "REJECTED_EVIDENCE_UNBOUND",
                    "path": _relative(project.root, candidate),
                    "detail": "candidate could not be bound to an input page",
                }
            )
            continue
        output_name = source_to_output[source_page_id]
        cluster = cluster_by_page[output_name]
        pack = pack_by_id[cluster["reference_pack_id"]]
        record_failure(
            failure_store,
            page_id=_migration_page_id(output_name),
            cluster_id=cluster["cluster_id"],
            character="unknown",
            codes=["style_drift"],
            diagnosis="legacy candidate is known rejected evidence",
            corrective_action="never reuse the rejected candidate as a style source",
            before_candidate_path=_relative(project.root, candidate),
            before_candidate_hash=sha256_file(candidate),
            prompt_reference_hash=pack["reference_binding_hash"],
            created_at=generated_at,
        )
    failure_store = _with_registry_hash(failure_store)

    scene_qa = _with_registry_hash(
        {
            "schema_version": "1.0",
            "proposal_only": True,
            "generated_at": generated_at,
            "clusters": [
                {
                    "cluster_id": cluster["cluster_id"],
                    "member_pages": cluster["member_pages"],
                    "canary_page": cluster["canary_page"],
                    "status": "pending",
                    "pass_id": None,
                    "reviewed_by": None,
                    "reviewed_at": None,
                }
                for cluster in clusters
            ],
        }
    )
    manifest_doc = _make_manifest_proposal(project, page_infos, traces)
    repair_doc = _make_repair_proposal(page_infos, traces)

    metrics_doc = {
        "schema_version": "1.0",
        "proposal_only": True,
        "generated_at": generated_at,
        "source_lock_hash": source_lock_hash,
        **queue_metrics(queue, now=generated_at),
    }
    artifacts: dict[str, Any] = {
        "scene_clusters.json": scene_doc,
        "style_reference_packs.json": pack_doc,
        "task_queue.json": queue,
        "failure_learning.json": failure_store,
        "scene_cluster_qa.json": scene_qa,
        "comic_run_manifest.json": manifest_doc,
        "repair_log.json": repair_doc,
        "pipeline_metrics.json": metrics_doc,
    }
    planned_outputs = [
        {
            "index": page["index"],
            "source_page": page["input_name"],
            "source_sha256": page["input_sha256"],
            "output_name": page["output_name"],
        }
        for page in page_infos
    ]
    proposal_hashes = _proposal_hashes(artifacts)
    derived_gate = _derive_migration_gate(project, evidence_dir, artifacts)
    blockers = derived_gate["blockers"]
    exact_bijection_planned = derived_gate["exact_bijection_planned"]
    ready_for_migration = derived_gate["ready_for_migration"]
    if resolved_artifact_dir is not None:
        _write_proposals(
            resolved_artifact_dir,
            artifacts,
            filenames=PROPOSAL_FILENAMES[:-1],
        )

    (
        pages_after,
        _page_ids_after,
        _input_records_after,
        _reference_records_after,
        source_records_after,
    ) = _capture_source_snapshot(project, evidence_dir)
    source_hashes_after = _source_digest_map(source_records_after)
    output_after = _snapshot_directory(project.root, project.output_dir)
    image_after = _snapshot_images(project.root, resolved_artifact_dir)
    output_count_after = _supported_output_count(output_after)

    report_body = {
        "schema_version": SCHEMA_VERSION,
        "proposal_only": True,
        "prepared_by": prepared_by,
        "generated_at": generated_at,
        "project_root": str(project.root),
        "input_count": len(pages),
        "input_count_before": len(pages),
        "input_count_after": len(pages_after),
        "output_count_before": output_count_before,
        "output_count_after": output_count_after,
        "source_files": source_records,
        "source_hashes": source_hashes_before,
        "source_hashes_before": source_hashes_before,
        "source_hashes_after": source_hashes_after,
        "locked_sources_before": source_hashes_before,
        "locked_sources_after": source_hashes_after,
        "source_lock_hash": source_lock_hash,
        "output_snapshot_before": output_before,
        "output_snapshot_after": output_after,
        "image_snapshot_before": image_before,
        "image_snapshot_after": image_after,
        "proposal_hash_algorithm": "canonical-json-sha256",
        "proposal_hashes": proposal_hashes,
        "cluster_count": len(clusters),
        "task_count": len(queue["tasks"]),
        "unresolved_alignment_pages": unresolved_pages,
        "unconfirmed_alignment_pages": unconfirmed_pages,
        "blockers": blockers,
        "ready_for_generation": False,
        "ready_for_migration": ready_for_migration,
        "images_touched": _snapshot_difference_count(image_before, image_after),
        "output_unchanged": output_after == output_before,
        "source_unchanged": source_hashes_after == source_hashes_before,
        "locked_sources_unchanged": source_hashes_after == source_hashes_before,
        "exact_bijection_planned": exact_bijection_planned,
        "planned_outputs": planned_outputs,
    }
    confirmed_report_digest = canonical_hash(report_body)
    migration_id = _migration_identity(
        source_hashes_before,
        output_before,
        image_before,
        proposal_hashes,
        blockers,
        ready_for_migration,
        exact_bijection_planned,
        confirmed_report_digest,
    )
    report = _seal_report(
        {
            **report_body,
            "confirmed_report_digest": confirmed_report_digest,
            "migration_id": migration_id,
        }
    )
    artifacts["pipeline_migration_report.json"] = report

    if resolved_artifact_dir is not None:
        _write_proposals(
            resolved_artifact_dir,
            artifacts,
            filenames=("pipeline_migration_report.json",),
        )
        if output_after != output_before:
            raise RuntimeError("output changed during migration dry-run")
        if source_hashes_after != source_hashes_before:
            raise RuntimeError("source changed during migration dry-run")
        if image_after != image_before:
            raise RuntimeError("project images changed during migration dry-run")

    return {"report": report, "artifacts": artifacts}


def _load_bundle(directory: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for filename in PROPOSAL_FILENAMES:
        path = directory / filename
        if not path.is_file():
            raise ValueError(f"proposal bundle missing {filename}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"proposal bundle unreadable {filename}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"proposal bundle document must be an object: {filename}")
        artifacts[filename] = value
    return artifacts


def _load_report_argument(report_or_dir: Mapping[str, Any] | Path | str) -> tuple[dict[str, Any], Path | None]:
    if isinstance(report_or_dir, Mapping):
        return copy.deepcopy(dict(report_or_dir)), None
    path = Path(report_or_dir).expanduser().resolve()
    report_path = path / "pipeline_migration_report.json" if path.is_dir() else path
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"migration report is unreadable: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("migration report must be an object")
    return report, (path if path.is_dir() else report_path.parent)


def create_migration_review(
    report_or_dir: Mapping[str, Any] | Path | str,
    *,
    reviewed_by: str,
    reviewed_at: datetime | str,
    decision: str = "accepted",
    notes: str = "",
) -> dict[str, Any]:
    """Create one independent, content-addressed migration review record."""
    report, output_dir = _load_report_argument(report_or_dir)
    _validate_report_integrity(report)
    _validate_confirmed_report_digest(report)
    prepared_by = str(report.get("prepared_by", "")).strip()
    reviewer = str(reviewed_by).strip()
    if not prepared_by:
        raise ValueError("prepared_by must be non-empty")
    if not reviewer:
        raise ValueError("reviewed_by must be non-empty")
    if reviewer.casefold() == prepared_by.casefold():
        raise ValueError("migration review must be independent from prepared_by")
    normalized_decision = str(decision).strip().casefold()
    if normalized_decision not in {"accepted", "rejected"}:
        raise ValueError("review decision must be accepted or rejected")
    if not isinstance(notes, str):
        raise ValueError("review notes must be a string")
    review = {
        "migration_id": report.get("migration_id"),
        "confirmed_report_digest": report.get("confirmed_report_digest"),
        "prepared_by": prepared_by,
        "reviewed_by": reviewer,
        "reviewed_at": _timestamp(reviewed_at),
        "decision": normalized_decision,
        "notes": notes,
    }
    review["review_id"] = canonical_hash(review)
    if output_dir is not None:
        atomic_write_json(output_dir / "migration_review.json", review)
    return review


def _load_review_argument(review_or_path: Mapping[str, Any] | Path | str) -> dict[str, Any]:
    if isinstance(review_or_path, Mapping):
        return copy.deepcopy(dict(review_or_path))
    path = Path(review_or_path).expanduser().resolve()
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"migration review is unreadable: {exc}") from exc
    if not isinstance(review, dict):
        raise ValueError("migration review must be an object")
    return review


def validate_migration_review(
    review_or_path: Mapping[str, Any] | Path | str,
    report_or_dir: Mapping[str, Any] | Path | str | None = None,
) -> dict[str, Any]:
    """Validate review structure, identity hash, independence, and report binding."""
    errors: list[str] = []
    try:
        review = _load_review_argument(review_or_path)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}
    required = {
        "migration_id",
        "confirmed_report_digest",
        "prepared_by",
        "reviewed_by",
        "reviewed_at",
        "decision",
        "notes",
        "review_id",
    }
    if set(review) != required:
        errors.append("migration review fields do not match the strict schema")
    payload = {key: value for key, value in review.items() if key != "review_id"}
    review_id = review.get("review_id")
    if (
        not isinstance(review_id, str)
        or _SHA256.fullmatch(review_id) is None
        or review_id != canonical_hash(payload)
    ):
        errors.append("migration review review_id mismatch")
    prepared_by = review.get("prepared_by")
    reviewed_by = review.get("reviewed_by")
    if not isinstance(prepared_by, str) or not prepared_by.strip():
        errors.append("migration review prepared_by must be non-empty")
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        errors.append("migration review reviewed_by must be non-empty")
    if (
        isinstance(prepared_by, str)
        and isinstance(reviewed_by, str)
        and prepared_by.strip().casefold() == reviewed_by.strip().casefold()
    ):
        errors.append("migration review reviewer must be independent from prepared_by")
    if review.get("decision") not in {"accepted", "rejected"}:
        errors.append("migration review decision must be accepted or rejected")
    if not isinstance(review.get("notes"), str):
        errors.append("migration review notes must be a string")
    try:
        _timestamp(review.get("reviewed_at"))
    except ValueError as exc:
        errors.append(f"migration review reviewed_at timezone invalid: {exc}")
    for field in ("migration_id", "confirmed_report_digest"):
        value = review.get(field)
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            errors.append(f"migration review {field} is malformed")
    if report_or_dir is not None:
        try:
            report, _output_dir = _load_report_argument(report_or_dir)
            _validate_report_integrity(report)
            _validate_confirmed_report_digest(report)
        except ValueError as exc:
            errors.append(f"migration review report binding failed: {exc}")
        else:
            for field in ("migration_id", "confirmed_report_digest", "prepared_by"):
                if review.get(field) != report.get(field):
                    errors.append(f"migration review {field} does not match report")
    return {"ok": not errors, "errors": errors, "review": review}


def _validate_registry_document(
    filename: str, document: Mapping[str, Any], errors: list[str]
) -> None:
    recorded = document.get("registry_hash")
    payload = {key: value for key, value in document.items() if key != "registry_hash"}
    if recorded != canonical_hash(payload):
        errors.append(f"{filename} registry_hash mismatch")


def _validate_bundle_documents(artifacts: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        manifest = artifacts["comic_run_manifest.json"]
        repair = artifacts["repair_log.json"]
        scene = artifacts["scene_clusters.json"]
        packs_doc = artifacts["style_reference_packs.json"]
        queue = artifacts["task_queue.json"]
        learning = artifacts["failure_learning.json"]
        cluster_qa = artifacts["scene_cluster_qa.json"]
    except KeyError as exc:
        return [f"bundle missing document: {exc.args[0]}"]
    for filename, document in (
        ("scene_clusters.json", scene),
        ("style_reference_packs.json", packs_doc),
        ("task_queue.json", queue),
        ("failure_learning.json", learning),
        ("scene_cluster_qa.json", cluster_qa),
    ):
        if not isinstance(document, Mapping):
            errors.append(f"{filename} must be an object")
        else:
            _validate_registry_document(filename, document, errors)
    for label, document in (("manifest", manifest), ("repair", repair)):
        if (
            not isinstance(document, Mapping)
            or document.get("schema_version") != SCHEMA_VERSION
            or document.get("pipeline_mode") != PIPELINE_MODE
            or document.get("evidence_files") != list(EVIDENCE_FILES)
        ):
            errors.append(f"{label} schema declaration invalid")
    if errors:
        return errors
    run_pages = manifest.get("pages")
    repair_pages = repair.get("pages")
    clusters = scene.get("clusters")
    packs = packs_doc.get("reference_packs")
    tasks = queue.get("tasks")
    qa_rows = cluster_qa.get("clusters")
    if not all(isinstance(value, list) for value in (run_pages, repair_pages, clusters, packs, tasks, qa_rows)):
        return errors + ["bundle collections must be lists"]
    outputs = [row.get("output_name") for row in run_pages if isinstance(row, Mapping)]
    inputs = [row.get("input_name") for row in run_pages if isinstance(row, Mapping)]
    path_types_valid = all(isinstance(value, str) for value in inputs + outputs)
    if path_types_valid:
        try:
            expected_outputs = make_output_names(inputs)
        except ValueError:
            expected_outputs = None
    else:
        expected_outputs = None
    if (
        len(outputs) != len(run_pages)
        or not path_types_valid
        or (path_types_valid and len(set(outputs)) != len(outputs))
        or expected_outputs is None
        or outputs != expected_outputs
        or (path_types_valid and len(set(inputs)) != len(inputs))
    ):
        errors.append("manifest input/output bijection invalid")
    if not path_types_valid:
        return errors
    cluster_map: dict[str, Mapping[str, Any]] = {}
    page_to_cluster: dict[str, str] = {}
    ordered_members: list[str] = []
    blocked_undersized_pages: set[str] = set()
    for cluster in clusters:
        if not isinstance(cluster, Mapping) or not isinstance(cluster.get("cluster_id"), str):
            errors.append("scene cluster identity invalid")
            continue
        cluster_id = cluster["cluster_id"]
        members = cluster.get("member_pages")
        source_pages = cluster.get("source_pages")
        if cluster_id in cluster_map or not isinstance(members, list) or not isinstance(source_pages, list):
            errors.append("scene cluster membership invalid")
            continue
        cluster_map[cluster_id] = cluster
        ordered_members.extend(members)
        size = len(members)
        undersized = cluster.get("undersized")
        boundary_exception = cluster.get("boundary_exception")
        reason = cluster.get("undersized_reason")
        if (
            not isinstance(undersized, bool)
            or undersized != (size < 8)
            or size > 20
        ):
            errors.append(f"scene cluster size/undersized invalid: {cluster_id}")
        elif undersized and boundary_exception is True:
            if (
                len(outputs) >= 8
                or members != outputs
                or reason != "project_total_below_min"
            ):
                errors.append(f"undersized boundary exception invalid: {cluster_id}")
        elif undersized:
            if (
                boundary_exception is not False
                or cluster.get("blocked") is not True
                or reason != "scene_fragment_below_min"
                or "UNDERSIZED_CLUSTER" not in cluster.get("blocker_codes", [])
            ):
                errors.append(f"undersized cluster must remain blocked: {cluster_id}")
            blocked_undersized_pages.update(
                member for member in members if isinstance(member, str)
            )
        elif boundary_exception is not False or reason is not None:
            errors.append(f"normal cluster boundary fields invalid: {cluster_id}")
        for member in members:
            if member in page_to_cluster:
                errors.append("scene cluster page is duplicated")
            page_to_cluster[member] = cluster_id
    if ordered_members != outputs:
        errors.append("scene cluster pages do not form the manifest bijection")
    stable_pages = packs_doc.get("stable_pages")
    approved_hashes = packs_doc.get("approved_hashes")
    if not isinstance(stable_pages, list) or not isinstance(approved_hashes, Mapping):
        errors.append("style reference registry fields invalid")
        stable_pages = []
        approved_hashes = {}
    pack_map: dict[str, Mapping[str, Any]] = {}
    binding_map: dict[str, str] = {}
    for pack in packs:
        if not isinstance(pack, Mapping):
            errors.append("reference pack must be an object")
            continue
        pack_id = pack.get("reference_pack_id")
        cluster_id = pack.get("cluster_id")
        references = pack.get("references")
        if not isinstance(pack_id, str) or not isinstance(cluster_id, str) or not isinstance(references, list):
            errors.append("reference pack identity invalid")
            continue
        try:
            rebuilt = build_reference_pack(
                {"cluster_id": cluster_id},
                references,
                stable_pages=stable_pages,
                contract_version="v3",
            )
        except ValueError as exc:
            errors.append(f"reference pack invalid: {exc}")
            continue
        if rebuilt != dict(pack):
            errors.append(f"reference pack content identity mismatch: {pack_id}")
        if any(
            approved_hashes.get(reference.get("path")) != reference.get("sha256")
            for reference in references
            if isinstance(reference, Mapping)
        ):
            errors.append(f"reference pack approved hash mismatch: {pack_id}")
        pack_map[pack_id] = pack
        binding_map[pack_id] = rebuilt["reference_binding_hash"]
    for cluster_id, cluster in cluster_map.items():
        pack = pack_map.get(cluster.get("reference_pack_id"))
        if pack is None or pack.get("cluster_id") != cluster_id or cluster.get("reference_pack_state") != "bound":
            errors.append(f"cluster reference pack binding invalid: {cluster_id}")
    task_by_page: dict[str, Mapping[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, Mapping) or not isinstance(task.get("page_id"), str):
            errors.append("task identity invalid")
            continue
        page_id = task["page_id"]
        if page_id in task_by_page:
            errors.append("duplicate task page binding")
        task_by_page[page_id] = task
    try:
        expected_task_pages = {_migration_page_id(name) for name in outputs}
    except ValueError:
        expected_task_pages = set()
    if set(task_by_page) != expected_task_pages:
        errors.append("task queue page bijection invalid")
    repair_by_output = {
        row.get("output_name"): row for row in repair_pages if isinstance(row, Mapping)
    }
    for run_row in run_pages:
        if not isinstance(run_row, Mapping):
            continue
        output = run_row.get("output_name")
        repair_row = repair_by_output.get(output)
        if not isinstance(output, str) or not isinstance(repair_row, Mapping):
            errors.append("manifest repair page mapping invalid")
            continue
        cluster_id = page_to_cluster.get(output)
        cluster = cluster_map.get(cluster_id)
        pack_id = cluster.get("reference_pack_id") if cluster is not None else None
        try:
            task_page_id = _migration_page_id(output)
        except ValueError:
            task_page_id = None
        task = task_by_page.get(task_page_id)
        for field in ("cluster_id", "reference_pack_id", "task_id"):
            if run_row.get(field) != repair_row.get(field):
                errors.append(f"manifest repair trace mismatch: {output} {field}")
        if run_row.get("cluster_id") != cluster_id or run_row.get("reference_pack_id") != pack_id:
            errors.append(f"page cluster/pack trace invalid: {output}")
        if task is None or task.get("task_id") != run_row.get("task_id"):
            errors.append(f"page task trace invalid: {output}")
        elif (
            task.get("cluster_id") != cluster_id
            or task.get("prompt_reference_hash") != binding_map.get(pack_id)
        ):
            errors.append(f"task reference binding invalid: {output}")
        page_class = run_row.get("page_class")
        expected_action = _PAGE_CLASS_TO_ACTION.get(page_class)
        if (
            page_class not in PAGE_CLASSES
            or repair_row.get("page_class") != page_class
            or expected_action is None
        ):
            errors.append(f"manifest repair page class mismatch: {output}")
        elif repair_row.get("action") != expected_action:
            errors.append(f"repair page class/action mapping invalid: {output}")
        if task is not None and task.get("page_class") != page_class:
            errors.append(f"task page class mismatch: {output}")
        if output in blocked_undersized_pages and (
            page_class != "evidence_blocked"
            or task is None
            or task.get("task_type") != "evidence_resolution"
        ):
            errors.append(f"undersized blocked page released for work: {output}")
    qa_ids = {
        row.get("cluster_id") for row in qa_rows if isinstance(row, Mapping)
    }
    if qa_ids != set(cluster_map) or len(qa_rows) != len(cluster_map):
        errors.append("scene cluster QA bijection invalid")
    return errors


def validate_migration_bundle(
    project_root: Path | str, evidence_dir: Path | str
) -> dict[str, Any]:
    """Validate an applied migration bundle without requiring completed output."""
    root = Path(project_root).expanduser().resolve()
    directory = Path(evidence_dir).expanduser().resolve()
    try:
        artifacts = _load_bundle(directory)
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)]}
    errors = _validate_bundle_documents(artifacts)
    report = artifacts.get("pipeline_migration_report.json", {})
    if isinstance(report, Mapping):
        try:
            _validate_report_integrity(report)
            _validate_confirmed_report_digest(report)
        except ValueError as exc:
            errors.append(str(exc))
    else:
        errors.append("report integrity document is not an object")
    applied_hashes = report.get("applied_hashes") if isinstance(report, Mapping) else None
    expected_applied = {
        filename: canonical_hash(artifacts[filename])
        for filename in PROPOSAL_FILENAMES
        if filename != "pipeline_migration_report.json"
    }
    if not isinstance(applied_hashes, Mapping) or dict(applied_hashes) != expected_applied:
        errors.append("applied_hashes do not match applied bundle content")
    if not isinstance(report, Mapping) or not isinstance(report.get("source_migration_id"), str):
        errors.append("source_migration_id missing from applied report")
    else:
        try:
            project = discover_project(root)
            derived_gate = _derive_migration_gate(project, directory, artifacts)
        except (OSError, ValueError) as exc:
            errors.append(f"derived migration gate failed: {exc}")
        else:
            reported_blockers = report.get("blockers")
            if (
                not isinstance(reported_blockers, list)
                or any(not isinstance(item, Mapping) for item in reported_blockers)
                or _canonical_blockers(reported_blockers) != derived_gate["blockers"]
                or report.get("ready_for_migration")
                != derived_gate["ready_for_migration"]
                or report.get("exact_bijection_planned")
                != derived_gate["exact_bijection_planned"]
            ):
                errors.append("derived migration gate does not match applied report")
            else:
                expected_source_id = _migration_identity(
                    report.get("locked_sources_before", {}),
                    report.get("output_snapshot_before", []),
                    report.get("image_snapshot_before", []),
                    report.get("proposal_hashes", {}),
                    derived_gate["blockers"],
                    derived_gate["ready_for_migration"],
                    derived_gate["exact_bijection_planned"],
                    report.get("source_confirmed_report_digest", ""),
                )
                if report.get("source_migration_id") != expected_source_id:
                    errors.append("source_migration_id does not match derived gate")
    return {"ok": not errors, "errors": errors}


def _copy_with_fsync(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _write_migration_journal(path: Path, journal: Mapping[str, Any]) -> None:
    atomic_write_json(path, dict(journal))


def _trusted_migration_journal(
    evidence_dir: Path, journal: Mapping[str, Any]
) -> tuple[Path, Path, list[dict[str, Any]]]:
    if journal.get("schema_version") != "1.0":
        raise ValueError("migration journal schema is not trusted")
    if journal.get("state") not in {"intent", "prepared", "committing", "committed"}:
        raise ValueError("migration journal state is not trusted")
    migration_id = journal.get("migration_id")
    if not isinstance(migration_id, str) or _SHA256.fullmatch(migration_id) is None:
        raise ValueError("migration journal id is not trusted")

    stage_value = journal.get("stage_dir")
    backup_value = journal.get("backup_dir")
    if not isinstance(stage_value, str) or not isinstance(backup_value, str):
        raise ValueError("migration journal paths are not trusted")
    stage = Path(os.path.abspath(os.path.expanduser(stage_value)))
    backup = Path(os.path.abspath(os.path.expanduser(backup_value)))
    expected_backup = evidence_dir / "migration_backups" / migration_id
    if (
        stage.parent != evidence_dir
        or re.fullmatch(r"\.migration-staging-[A-Za-z0-9._-]+", stage.name) is None
        or backup != expected_backup
        or stage.resolve() != stage
        or backup.resolve() != backup
    ):
        raise ValueError("migration journal paths escape trusted migration directories")

    raw_targets = journal.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) != len(PROPOSAL_FILENAMES):
        raise ValueError("migration journal target registry is not trusted")
    targets: list[dict[str, Any]] = []
    for raw in raw_targets:
        if not isinstance(raw, Mapping):
            raise ValueError("migration journal target entry is not trusted")
        filename = raw.get("filename")
        had_existing = raw.get("had_existing")
        before_hash = raw.get("before_hash")
        backup_hash = raw.get("backup_hash")
        stage_hash = raw.get("stage_hash")
        if filename not in PROPOSAL_FILENAMES or not isinstance(had_existing, bool):
            raise ValueError("migration journal target entry is not trusted")
        if (
            stage_hash is not None
            and (not isinstance(stage_hash, str) or _SHA256.fullmatch(stage_hash) is None)
        ):
            raise ValueError("migration journal stage hash is not trusted")
        if had_existing:
            if (
                not isinstance(before_hash, str)
                or _SHA256.fullmatch(before_hash) is None
                or (
                    backup_hash is not None
                    and backup_hash != before_hash
                )
            ):
                raise ValueError("migration journal backup hash is not trusted")
        elif before_hash is not None or backup_hash is not None:
            raise ValueError("migration journal absent-target record is not trusted")
        targets.append(dict(raw))
    filenames = [record["filename"] for record in targets]
    if len(set(filenames)) != len(filenames) or set(filenames) != set(PROPOSAL_FILENAMES):
        raise ValueError("migration journal target registry is not trusted")
    committed = journal.get("committed")
    if (
        not isinstance(committed, list)
        or any(name not in PROPOSAL_FILENAMES for name in committed)
        or len(set(committed)) != len(committed)
    ):
        raise ValueError("migration journal committed registry is not trusted")
    if journal.get("state") == "committed" and set(committed) != set(PROPOSAL_FILENAMES):
        raise ValueError("migration journal committed state is incomplete")
    if journal.get("state") != "intent":
        for record in targets:
            if record["stage_hash"] is None or (
                record["had_existing"] and record["backup_hash"] is None
            ):
                raise ValueError("migration journal prepared evidence is incomplete")
    return stage, backup, targets


def _remove_empty_backup_parent(backup: Path) -> None:
    parent = backup.parent
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def recover_interrupted_migration(project_root: Path | str) -> dict[str, Any]:
    """Recover or finish cleanup for one trusted persistent migration journal."""
    project = discover_project(Path(project_root))
    evidence_dir = (project.root / "evidence").resolve()
    journal_path = evidence_dir / JOURNAL_FILENAME
    if not journal_path.is_file():
        return {"recovered": False, "state": "clean"}
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"migration journal is unreadable: {exc}") from exc
    if not isinstance(journal, Mapping):
        raise ValueError("migration journal root is not trusted")
    stage, backup, targets = _trusted_migration_journal(evidence_dir, journal)
    state = journal["state"]

    if state == "intent":
        changed = []
        for record in targets:
            target = evidence_dir / record["filename"]
            if record["had_existing"]:
                if not target.is_file() or sha256_file(target) != record["before_hash"]:
                    changed.append(record["filename"])
            elif target.exists():
                changed.append(record["filename"])
        if changed:
            raise ValueError(
                f"migration journal intent targets changed without a prepared transaction: {changed!r}"
            )
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        _remove_empty_backup_parent(backup)
        journal_path.unlink()
        return {"recovered": True, "state": "intent_cleaned"}

    if state in {"prepared", "committing"}:
        for record in targets:
            if not record["had_existing"]:
                continue
            source = backup / record["filename"]
            if not source.is_file() or sha256_file(source) != record["backup_hash"]:
                raise ValueError(
                    f"migration journal backup cannot be trusted: {record['filename']}"
                )
        for record in targets:
            target = evidence_dir / record["filename"]
            if record["had_existing"]:
                source = backup / record["filename"]
                handle = tempfile.NamedTemporaryFile(
                    dir=evidence_dir, prefix=".migration-recovery-", delete=False
                )
                restore = Path(handle.name)
                handle.close()
                try:
                    _copy_with_fsync(source, restore)
                    os.replace(restore, target)
                finally:
                    if restore.exists():
                        restore.unlink()
            elif target.exists():
                target.unlink()
        for record in targets:
            target = evidence_dir / record["filename"]
            if record["had_existing"]:
                if not target.is_file() or sha256_file(target) != record["before_hash"]:
                    raise ValueError(
                        f"migration journal recovery verification failed: {record['filename']}"
                    )
            elif target.exists():
                raise ValueError(
                    f"migration journal recovery verification failed: {record['filename']}"
                )
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(backup, ignore_errors=True)
        _remove_empty_backup_parent(backup)
        journal_path.unlink()
        return {"recovered": True, "state": "rolled_back"}

    for record in targets:
        target = evidence_dir / record["filename"]
        if not target.is_file() or sha256_file(target) != record["stage_hash"]:
            raise ValueError(
                f"migration journal committed target verification failed: {record['filename']}"
            )
    shutil.rmtree(stage, ignore_errors=True)
    journal_path.unlink()
    return {"recovered": True, "state": "committed_cleanup"}


def _prepare_applied_artifacts(
    proposal: Mapping[str, Any], source_migration_id: str
) -> dict[str, Any]:
    applied = copy.deepcopy(dict(proposal))
    for filename, document in applied.items():
        if filename == "pipeline_migration_report.json" or not isinstance(document, dict):
            continue
        document["proposal_only"] = False
        if filename in REGISTRY_FILENAMES:
            applied[filename] = _with_registry_hash(document)
    applied_hashes = {
        filename: canonical_hash(applied[filename])
        for filename in PROPOSAL_FILENAMES
        if filename != "pipeline_migration_report.json"
    }
    report = copy.deepcopy(dict(proposal["pipeline_migration_report.json"]))
    source_confirmed_report_digest = report["confirmed_report_digest"]
    report.update(
        {
            "proposal_only": False,
            "source_migration_id": source_migration_id,
            "source_confirmed_report_digest": source_confirmed_report_digest,
            "applied_hashes": applied_hashes,
            "migration_id": canonical_hash(
                {
                    "source_migration_id": source_migration_id,
                    "applied_hashes": applied_hashes,
                }
            ),
        }
    )
    report["confirmed_report_digest"] = _confirmed_report_digest(report)
    applied["pipeline_migration_report.json"] = _seal_report(report)
    return applied


def apply_project(
    project_root: Path | str,
    *,
    proposal_dir: Path | str,
    expected_migration_id: str,
    confirm_token: str,
    review_file: Path | str | None = None,
    now: datetime | str | None = None,
    fault_after_replacements: int | None = None,
    interrupt_after_replacements: int | None = None,
    interrupt_during_journal_update_at: int | None = None,
    interrupt_after_backup_dir_creation: bool = False,
    interrupt_after_first_backup_copy: bool = False,
    interrupt_after_stage_dir_creation: bool = False,
) -> dict[str, Any]:
    """Transactionally apply an independently verified dry-run proposal."""
    recover_interrupted_migration(project_root)
    if confirm_token != APPLY_CONFIRM_TOKEN:
        raise ValueError("apply requires the exact confirm token")
    project = discover_project(Path(project_root))
    evidence_dir = project.root / "evidence"
    proposal_path = _validate_artifact_dir(project, proposal_dir)
    if not proposal_path.is_dir():
        raise ValueError("proposal_dir does not exist")
    proposal = _load_bundle(proposal_path)
    report = proposal["pipeline_migration_report.json"]
    _validate_report_integrity(report)
    _validate_confirmed_report_digest(report)
    if report.get("migration_id") != expected_migration_id:
        raise ValueError("expected migration_id does not match proposal report")
    actual_proposal_hashes = _proposal_hashes(proposal)
    if actual_proposal_hashes != report.get("proposal_hashes"):
        raise ValueError("proposal hash verification failed")
    derived_gate = _derive_migration_gate(project, evidence_dir, proposal)
    reported_blockers = report.get("blockers")
    if not isinstance(reported_blockers, list) or any(
        not isinstance(blocker, Mapping) for blocker in reported_blockers
    ):
        raise ValueError("derived migration gate does not match report")
    if (
        _canonical_blockers(reported_blockers) != derived_gate["blockers"]
        or report.get("ready_for_migration")
        != derived_gate["ready_for_migration"]
        or report.get("exact_bijection_planned")
        != derived_gate["exact_bijection_planned"]
    ):
        raise ValueError("derived migration gate does not match report")
    recomputed_id = _migration_identity(
        report.get("locked_sources_before", {}),
        report.get("output_snapshot_before", []),
        report.get("image_snapshot_before", []),
        actual_proposal_hashes,
        derived_gate["blockers"],
        derived_gate["ready_for_migration"],
        derived_gate["exact_bijection_planned"],
        report["confirmed_report_digest"],
    )
    if recomputed_id != expected_migration_id:
        raise ValueError("migration_id content verification failed")
    if review_file is None:
        raise ValueError("apply requires an independent migration review file")
    review_path = Path(review_file).expanduser().resolve()
    if review_path.name != "migration_review.json" or not review_path.is_file():
        raise ValueError("review file must be an existing migration_review.json")
    review_validation = validate_migration_review(review_path, report)
    if not review_validation["ok"]:
        raise ValueError(
            "migration review invalid: " + "; ".join(review_validation["errors"])
        )
    if review_validation["review"].get("decision") != "accepted":
        raise ValueError("migration review decision must be accepted")
    if derived_gate["blockers"] or derived_gate["ready_for_migration"] is not True:
        raise ValueError("proposal contains blockers and is not ready for migration")
    existing = [
        filename
        for filename in PROPOSAL_FILENAMES
        if filename not in REPLACEABLE_EXISTING and (evidence_dir / filename).exists()
    ]
    if existing:
        raise ValueError(f"standard migration file already exists: {existing!r}")
    _, _, _, _, current_sources = _capture_source_snapshot(project, evidence_dir)
    if _source_digest_map(current_sources) != report.get("locked_sources_before"):
        raise ValueError("current source snapshot differs from dry-run")
    if _snapshot_directory(project.root, project.output_dir) != report.get("output_snapshot_before"):
        raise ValueError("current output snapshot differs from dry-run")
    if _snapshot_images(project.root, proposal_path) != report.get("image_snapshot_before"):
        raise ValueError("current image snapshot differs from dry-run")
    proposal_errors = _validate_bundle_documents(proposal)
    if proposal_errors:
        raise ValueError("proposal bundle validation failed: " + "; ".join(proposal_errors))
    applied_artifacts = _prepare_applied_artifacts(proposal, expected_migration_id)
    applied_errors = _validate_bundle_documents(applied_artifacts)
    if applied_errors:
        raise ValueError("applied bundle validation failed: " + "; ".join(applied_errors))

    evidence_dir.mkdir(parents=True, exist_ok=True)
    stage = evidence_dir / f".migration-staging-{uuid.uuid4().hex}"
    backup_parent = evidence_dir / "migration_backups"
    backup = backup_parent / expected_migration_id
    if backup.exists():
        raise ValueError("migration backup already exists")
    journal_path = evidence_dir / JOURNAL_FILENAME
    targets: list[dict[str, Any]] = []
    for filename in PROPOSAL_FILENAMES:
        source = evidence_dir / filename
        had_existing = source.is_file()
        targets.append(
            {
                "filename": filename,
                "had_existing": had_existing,
                "before_hash": sha256_file(source) if had_existing else None,
                "backup_hash": None,
                "stage_hash": None,
            }
        )
    journal: dict[str, Any] = {
        "schema_version": "1.0",
        "state": "intent",
        "migration_id": expected_migration_id,
        "stage_dir": str(stage),
        "backup_dir": str(backup),
        "targets": targets,
        "committed": [],
    }
    _write_migration_journal(journal_path, journal)
    try:
        backup.mkdir(parents=True)
        if interrupt_after_backup_dir_creation:
            raise KeyboardInterrupt("injected interruption after backup directory creation")
        backed_up = []
        backup_copy_count = 0
        for record in targets:
            if record["had_existing"]:
                filename = record["filename"]
                source = evidence_dir / filename
                _copy_with_fsync(source, backup / filename)
                record["backup_hash"] = sha256_file(backup / filename)
                backed_up.append(
                    {"filename": filename, "sha256": record["backup_hash"]}
                )
                _write_migration_journal(journal_path, journal)
                backup_copy_count += 1
                if interrupt_after_first_backup_copy and backup_copy_count == 1:
                    raise KeyboardInterrupt("injected interruption after first backup copy")
        atomic_write_json(
            backup / "backup_manifest.json",
            {
                "source_migration_id": expected_migration_id,
                "created_at": _timestamp(now),
                "files": backed_up,
            },
        )
        stage.mkdir()
        if interrupt_after_stage_dir_creation:
            raise KeyboardInterrupt("injected interruption after stage directory creation")
        _write_proposals(stage, applied_artifacts)
        for record in targets:
            record["stage_hash"] = sha256_file(stage / record["filename"])
        journal["state"] = "prepared"
        _write_migration_journal(journal_path, journal)
        journal["state"] = "committing"
        _write_migration_journal(journal_path, journal)
        replacements = 0
        for filename in PROPOSAL_FILENAMES:
            os.replace(stage / filename, evidence_dir / filename)
            replacements += 1
            if (
                interrupt_after_replacements is not None
                and replacements >= interrupt_after_replacements
            ):
                raise KeyboardInterrupt("injected interruption after replacement")
            if (
                fault_after_replacements is not None
                and replacements >= fault_after_replacements
            ):
                raise RuntimeError("injected migration replacement failure")
            journal["committed"].append(filename)
            if (
                interrupt_during_journal_update_at is not None
                and replacements >= interrupt_during_journal_update_at
            ):
                raise KeyboardInterrupt("injected interruption during journal update")
            _write_migration_journal(journal_path, journal)
        for record in targets:
            target = evidence_dir / record["filename"]
            if not target.is_file() or sha256_file(target) != record["stage_hash"]:
                raise RuntimeError(
                    f"migration commit verification failed: {record['filename']}"
                )
        journal["state"] = "committed"
        _write_migration_journal(journal_path, journal)
    except Exception:
        recover_interrupted_migration(project.root)
        raise
    shutil.rmtree(stage, ignore_errors=True)
    journal_path.unlink()
    return {
        "report": applied_artifacts["pipeline_migration_report.json"],
        "artifacts": applied_artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help="Comic project root")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Build proposal only (default)")
    mode.add_argument("--apply", action="store_true", help="Write new standard migration files")
    parser.add_argument("--artifact-dir", type=Path, help="Independent proposal output directory")
    parser.add_argument("--proposal-dir", type=Path, help="Verified dry-run proposal directory")
    parser.add_argument("--migration-id", help="Expected dry-run migration identifier")
    parser.add_argument("--confirm-token", help="Required exact token for --apply")
    parser.add_argument(
        "--review-file",
        type=Path,
        help="Required independent accepted migration_review.json for --apply",
    )
    parser.add_argument(
        "--prepared-by",
        default=DEFAULT_PREPARED_BY,
        help="Non-empty preparer identity recorded in a dry-run report",
    )
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if (
                args.proposal_dir is None
                or not args.migration_id
                or not args.confirm_token
                or args.review_file is None
            ):
                raise ValueError(
                    "--apply requires --proposal-dir, --migration-id, --confirm-token, and --review-file"
                )
            result = apply_project(
                args.project,
                proposal_dir=args.proposal_dir,
                expected_migration_id=args.migration_id,
                confirm_token=args.confirm_token,
                review_file=args.review_file,
            )
        else:
            result = migrate_project(
                args.project,
                artifact_dir=args.artifact_dir,
                prepared_by=args.prepared_by,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {"ok": True, "report": result["report"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
