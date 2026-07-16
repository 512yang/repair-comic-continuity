"""Fail-closed orchestration contracts for the V5.5 comic repair loop."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from failure_learning import (
    new_failure_store,
    record_outcome,
    select_effective_rules,
    validate_failure_store,
)
from human_issue_learning import ingest_human_issue_annotations
from pipeline_contracts import canonical_hash, normalize_relative_image_path


SCHEMA_VERSION = "continuity-v5.5-closed-loop-v1"
ANNOTATION_BINDING_VERSION = "human-issue-binding-v1"
ANNOTATION_REVIEW_VERSION = "human-issue-review-v1"
RULE_SCOPES = ("page", "cluster", "project", "skill_candidate")
PAGE_CLASSES = frozenset(
    {"unchanged", "text_only", "full_page_redraw", "evidence_blocked"}
)
TRAIT_CODES = frozenset(
    {
        "skin_tone",
        "facial_hair",
        "hair_style",
        "hair_color",
        "headwear",
        "clothing",
        "prop",
        "body_build",
        "face_shape",
        "anatomy",
        "scene",
        "text_glyph",
        "text_style",
        "balloon_geometry",
        "sound_effect",
    }
)
STAGES = (
    "workspace_prepared",
    "inventory_sealed",
    "audit_passed",
    "annotations_ingested",
    "tasks_released",
    "candidate_reviewed",
    "learning_recorded",
    "outputs_promoted",
    "release_passed",
)
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_STABLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,126}[A-Za-z0-9]\Z")
_ACTION_BY_CODE = {
    "style_drift": "preserve_established_style",
    "identity_drift": "preserve_character_identity",
    "costume_prop_drift": "preserve_costume_and_props",
    "composition_drift": "preserve_panel_topology",
    "anatomy_error": "correct_anatomy",
    "scene_drift": "preserve_scene_continuity",
    "text_leak": "enforce_textless_output",
    "over_rendering": "avoid_over_rendering",
}


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _sha256(value: object, name: str) -> str:
    result = _text(value, name).lower()
    if _SHA256_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    return result


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    return parsed.astimezone(timezone.utc).isoformat()


def _stable_id(value: object, name: str) -> str:
    result = _text(value, name)
    if _STABLE_ID_RE.fullmatch(result) is None:
        raise ValueError(f"{name} must be a stable identifier")
    return result


def _artifact(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
        raise ValueError(f"{name} must contain exactly path and sha256")
    path = _safe_relative_path(value["path"], f"{name}.path")
    return {"path": path, "sha256": _sha256(value["sha256"], f"{name}.sha256")}


def _safe_relative_path(value: object, name: str) -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ValueError(f"{name} must be a safe relative path")
    raw = os.fspath(value)
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a safe relative path")
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in parts)
        or any(any(ord(character) < 32 for character in part) for part in parts)
    ):
        raise ValueError(f"{name} must be a safe relative path")
    return "/".join(parts)


def _annotation_rows(document: Mapping[str, Any], page_path: str) -> list[dict[str, Any]]:
    rows = document.get("annotations")
    if not isinstance(rows, list) or not rows:
        raise ValueError("human issue annotations must be a nonempty list")
    matches = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"annotations[{index}] must be a mapping")
        page = raw.get("page")
        if not isinstance(page, Mapping):
            raise ValueError(f"annotations[{index}].page must be a mapping")
        if normalize_relative_image_path(page.get("path")) != page_path:
            continue
        regions = raw.get("regions")
        if not isinstance(regions, list) or not regions:
            raise ValueError(f"annotations[{index}].regions must be nonempty")
        normalized_regions = []
        for region_index, region in enumerate(regions):
            if not isinstance(region, Mapping):
                raise ValueError("annotation region must be a mapping")
            bbox = region.get("bbox_norm")
            if (
                not isinstance(bbox, list)
                or len(bbox) != 4
                or any(not isinstance(item, (int, float)) for item in bbox)
                or not (0 <= bbox[0] < bbox[2] <= 1)
                or not (0 <= bbox[1] < bbox[3] <= 1)
            ):
                raise ValueError(
                    f"annotations[{index}].regions[{region_index}].bbox_norm is invalid"
                )
            normalized_regions.append(
                {
                    "region_id": _text(region.get("region_id"), "region_id"),
                    "bbox_norm": [float(item) for item in bbox],
                    "description": _text(region.get("description"), "description"),
                }
            )
        targets = raw.get("targets")
        codes = raw.get("defect_codes")
        if not isinstance(targets, list) or not targets:
            raise ValueError("annotation targets must be nonempty")
        if not isinstance(codes, list) or not codes:
            raise ValueError("annotation defect_codes must be nonempty")
        traits = raw.get("trait_codes", [])
        if not isinstance(traits, list):
            raise ValueError("annotation trait_codes must be a list")
        normalized_traits = sorted({_text(item, "trait code") for item in traits})
        unknown_traits = sorted(set(normalized_traits) - TRAIT_CODES)
        if unknown_traits:
            raise ValueError(f"unknown trait code(s): {unknown_traits!r}")
        matches.append(
            {
                "annotation_id": _text(raw.get("annotation_id"), "annotation_id"),
                "regions": normalized_regions,
                "targets": [_text(item, "target") for item in targets],
                "defect_codes": sorted({_text(item, "defect code") for item in codes}),
                "trait_codes": normalized_traits,
                "observed_state": _text(raw.get("observed_state"), "observed_state"),
                "required_state": _text(raw.get("required_state"), "required_state"),
                "instruction": _text(raw.get("instruction"), "instruction"),
            }
        )
    if not matches:
        raise ValueError(f"annotation page is missing: {page_path}")
    identifiers = [row["annotation_id"] for row in matches]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("annotation ids must be unique")
    return sorted(matches, key=lambda row: row["annotation_id"])


def build_annotation_binding(
    document: Mapping[str, Any],
    *,
    page_path: object,
    source_sha256: object,
    manifest_path: object,
    manifest_sha256: object,
) -> dict[str, Any]:
    """Bind the exact user-confirmed defects for one redraw page."""
    if not isinstance(document, Mapping):
        raise ValueError("annotation document must be a mapping")
    if (
        document.get("status") != "confirmed"
        or document.get("mode") != "human_visual_auto_text"
        or document.get("learning_policy") != "evidence_gated"
    ):
        raise ValueError("annotation document is not confirmed evidence")
    path = normalize_relative_image_path(page_path)
    digest = _sha256(source_sha256, "source sha256")
    rows = _annotation_rows(document, path)
    source_hashes = {
        _sha256(row["page"]["sha256"], "annotation source hash")
        for row in document["annotations"]
        if isinstance(row, Mapping)
        and isinstance(row.get("page"), Mapping)
        and normalize_relative_image_path(row["page"].get("path")) == path
    }
    if source_hashes != {digest}:
        raise ValueError("annotation source hash does not match current source hash")
    body = {
        "binding_version": ANNOTATION_BINDING_VERSION,
        "manifest": {
            "path": _safe_relative_path(manifest_path, "manifest path"),
            "sha256": _sha256(manifest_sha256, "manifest sha256"),
        },
        "page": {"path": path, "sha256": digest},
        "annotation_ids": [row["annotation_id"] for row in rows],
        "annotations": rows,
    }
    return {**body, "binding_sha256": canonical_hash(body)}


def validate_annotation_binding(
    value: object,
    *,
    page_path: object | None = None,
    source_sha256: object | None = None,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("human issue binding must be a mapping")
    required = {
        "binding_version",
        "manifest",
        "page",
        "annotation_ids",
        "annotations",
        "binding_sha256",
    }
    if set(value) != required or value.get("binding_version") != ANNOTATION_BINDING_VERSION:
        raise ValueError("human issue binding fields or version are invalid")
    manifest = _artifact(value["manifest"], "human issue manifest")
    page = value["page"]
    if not isinstance(page, Mapping) or set(page) != {"path", "sha256"}:
        raise ValueError("human issue binding page is invalid")
    normalized_page = {
        "path": normalize_relative_image_path(page["path"]),
        "sha256": _sha256(page["sha256"], "human issue page sha256"),
    }
    annotations = value["annotations"]
    if not isinstance(annotations, list) or not annotations:
        raise ValueError("human issue binding annotations must be nonempty")
    annotation_ids = value["annotation_ids"]
    if not isinstance(annotation_ids, list) or annotation_ids != [
        row.get("annotation_id") for row in annotations if isinstance(row, Mapping)
    ]:
        raise ValueError("human issue binding annotation_ids mismatch")
    body = {
        "binding_version": ANNOTATION_BINDING_VERSION,
        "manifest": manifest,
        "page": normalized_page,
        "annotation_ids": list(annotation_ids),
        "annotations": copy.deepcopy(annotations),
    }
    expected_hash = canonical_hash(body)
    if _sha256(value["binding_sha256"], "binding_sha256") != expected_hash:
        raise ValueError("human issue binding_sha256 mismatch")
    if page_path is not None and normalized_page["path"] != normalize_relative_image_path(page_path):
        raise ValueError("human issue binding page path mismatch")
    if source_sha256 is not None and normalized_page["sha256"] != _sha256(
        source_sha256, "source sha256"
    ):
        raise ValueError("human issue binding source hash mismatch")
    return {**body, "binding_sha256": expected_hash}


def resolve_rule_conflicts(rules: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Apply page>cluster>project>skill precedence and fail on peer conflicts."""
    normalized = [copy.deepcopy(dict(rule)) for rule in rules]
    rank = {scope: index for index, scope in enumerate(RULE_SCOPES)}
    winners: dict[tuple[str | None, str], dict[str, Any]] = {}
    for rule in sorted(normalized, key=lambda row: (rank.get(row.get("scope"), 99), row.get("rule_id", ""))):
        scope = rule.get("scope")
        if scope not in rank:
            raise ValueError("effective rule has invalid scope")
        codes = rule.get("codes")
        if not isinstance(codes, list) or not codes:
            raise ValueError("effective rule codes must be nonempty")
        action = _text(rule.get("corrective_action"), "corrective_action")
        for code in codes:
            key = (rule.get("character"), _text(code, "failure code"))
            current = winners.get(key)
            if current is None:
                winners[key] = rule
                continue
            current_rank = rank[current["scope"]]
            candidate_rank = rank[scope]
            if candidate_rank == current_rank and current["corrective_action"] != action:
                raise ValueError(
                    f"conflicting effective rules at the same scope for {key!r}"
                )
            if candidate_rank < current_rank:
                winners[key] = rule
    selected_ids = {rule["rule_id"] for rule in winners.values()}
    return sorted(
        [rule for rule in normalized if rule.get("rule_id") in selected_ids],
        key=lambda row: (rank[row["scope"]], row["rule_id"]),
    )


def load_effective_rules_for_page(
    store: dict[str, Any],
    *,
    page_id: object,
    cluster_id: str,
    targets: list[str],
    review_artifacts: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Select, conflict-check, and compile reviewed rules for one V4 prompt."""
    validate_failure_store(store)
    if not isinstance(targets, list) or len(targets) != len(set(targets)):
        raise ValueError("targets must be a unique list")
    if not isinstance(review_artifacts, Mapping):
        raise ValueError("review_artifacts must be a mapping")
    selected_by_id: dict[str, dict[str, Any]] = {}
    for target in [None, *targets]:
        rows = select_effective_rules(
            store,
            page_id=page_id,
            cluster_id=cluster_id,
            character=target,
        )
        for row in rows:
            selected_by_id[row["rule_id"]] = row
    selected = resolve_rule_conflicts(selected_by_id.values())
    source_by_id = {row["rule_id"]: row for row in store["rules"]}
    registry_hash = canonical_hash(store)
    compiled = []
    for rule in selected:
        source = source_by_id[rule["rule_id"]]
        review = review_artifacts.get(rule["rule_id"])
        if review is None:
            raise ValueError(f"independent review artifact is missing for {rule['rule_id']}")
        normalized_review = _artifact(review, "rule review artifact")
        expected_review_hash = source["promotion_evidence"]["independent_review"][
            "artifact_hash"
        ]
        if normalized_review["sha256"] != expected_review_hash:
            raise ValueError(f"independent review artifact hash mismatch for {rule['rule_id']}")
        prompt_character = rule["character"]
        if isinstance(prompt_character, str) and prompt_character.startswith("character:"):
            prompt_character = prompt_character.split(":", 1)[1]
        elif isinstance(prompt_character, str) and ":" in prompt_character:
            prompt_character = None
        for code in rule["codes"]:
            action_code = _ACTION_BY_CODE.get(code)
            if action_code is None:
                raise ValueError(f"failure code has no controlled prompt action: {code}")
            rule_id = rule["rule_id"]
            if len(rule["codes"]) > 1:
                rule_id = "rule-" + canonical_hash(
                    {"source_rule_id": rule["rule_id"], "code": code}
                )
            compiled.append(
                {
                    "rule_id": rule_id,
                    "registry_version": f"failure-learning-r{store['revision']}",
                    "registry_sha256": registry_hash,
                    "status": "independently_approved",
                    "review_evidence_path": normalized_review["path"],
                    "review_evidence_sha256": normalized_review["sha256"],
                    "action_code": action_code,
                    "parameters": {
                        "reviewed_corrective_action": rule["corrective_action"]
                    },
                    "scope": rule["scope"],
                    "codes": [code],
                    "page_id": rule["page_id"],
                    "cluster_id": rule["cluster_id"],
                    "character": prompt_character,
                }
            )
    rank = {scope: index for index, scope in enumerate(RULE_SCOPES)}
    return sorted(compiled, key=lambda row: (rank[row["scope"]], row["rule_id"]))


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=target.parent, prefix=target.name + ".", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def ingest_annotations_transactionally(
    *,
    store_path: Path,
    validated_annotations: dict[str, Any],
    cluster_by_page: Mapping[str, str],
    prompt_reference_hash: str,
) -> list[dict[str, Any]]:
    """Load, ingest, validate, and atomically persist user-confirmed failures."""
    path = Path(store_path)
    if path.exists():
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("failure learning store is unreadable") from exc
    else:
        store = new_failure_store()
    validate_failure_store(store)
    before = copy.deepcopy(store)
    failures = ingest_human_issue_annotations(
        store,
        validated_annotations,
        cluster_by_page,
        prompt_reference_hash,
    )
    validate_failure_store(store)
    if store != before or not path.exists():
        _atomic_write_json(path, store)
    return failures


def record_outcomes_transactionally(
    *,
    store_path: Path,
    failure_ids: list[str],
    after_candidate_path: str,
    after_candidate_sha256: str,
    effective: bool,
    reviewed_by: str,
    reviewed_at: str,
) -> list[dict[str, Any]]:
    """Record all after-results in one commit; any invalid outcome writes nothing."""
    path = Path(store_path)
    if not path.is_file():
        raise ValueError("failure learning store is missing")
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("failure learning store is unreadable") from exc
    validate_failure_store(store)
    if (
        not isinstance(failure_ids, list)
        or not failure_ids
        or len(failure_ids) != len(set(failure_ids))
    ):
        raise ValueError("failure_ids must be a unique nonempty list")
    working = copy.deepcopy(store)
    for failure_id in failure_ids:
        record_outcome(
            working,
            failure_id,
            after_candidate_path,
            after_candidate_sha256,
            effective,
            reviewed_by,
            reviewed_at,
        )
    validate_failure_store(working)
    _atomic_write_json(path, working)
    by_id = {row["failure_id"]: row for row in working["failures"]}
    return [by_id[failure_id] for failure_id in failure_ids]


def build_run_preview(
    *,
    input_pages: list[str],
    selected_visual_pages: list[str],
    annotations_by_page: Mapping[str, list[str]],
    page_classes: Mapping[str, str],
    effective_rule_ids: Mapping[str, list[str]],
) -> dict[str, Any]:
    pages = [normalize_relative_image_path(item) for item in input_pages]
    if len(pages) != len(set(pages)):
        raise ValueError("input pages must be unique")
    selected = [normalize_relative_image_path(item) for item in selected_visual_pages]
    if any(item not in pages for item in selected):
        raise ValueError("selected visual page is not an input page")
    if set(page_classes) != set(pages):
        raise ValueError("page classes must exactly cover input pages")
    rows = []
    blockers = []
    for page in pages:
        page_class = page_classes[page]
        if page_class not in PAGE_CLASSES:
            raise ValueError(f"invalid page class for {page}: {page_class}")
        if page_class == "full_page_redraw" and page not in selected:
            raise ValueError("unselected page cannot enter full_page_redraw")
        if page_class == "evidence_blocked":
            blockers.append(page)
        rows.append(
            {
                "path": page,
                "page_class": page_class,
                "visual_selected": page in selected,
                "text_policy": "page_reset_preserve_style",
                "annotations": list(annotations_by_page.get(page, [])),
                "effective_rule_ids": list(effective_rule_ids.get(page, [])),
                "generation_call_required": page_class == "full_page_redraw",
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "input_count": len(pages),
        "expected_output_count": len(pages),
        "text_audit_page_count": len(pages),
        "selected_visual_page_count": len(selected),
        "generation_call_count": sum(row["generation_call_required"] for row in rows),
        "blocked_pages": blockers,
        "pages": rows,
        "preview_sha256": canonical_hash(rows),
    }


def new_closed_loop_state(
    *, run_id: object, created_at: object, annotation_count: int
) -> dict[str, Any]:
    if not isinstance(annotation_count, int) or annotation_count < 0:
        raise ValueError("annotation_count must be a nonnegative integer")
    body = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _stable_id(run_id, "run_id"),
        "created_at": _timestamp(created_at, "created_at"),
        "annotation_count": annotation_count,
        "receipts": [],
    }
    return {**body, "state_sha256": canonical_hash(body)}


def _required_stages(annotation_count: int) -> tuple[str, ...]:
    if annotation_count:
        return STAGES
    return tuple(stage for stage in STAGES if stage != "annotations_ingested")


def validate_closed_loop_state(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("closed-loop state must be a mapping")
    required = {
        "schema_version",
        "run_id",
        "created_at",
        "annotation_count",
        "receipts",
        "state_sha256",
    }
    if set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("closed-loop state fields or version are invalid")
    count = value["annotation_count"]
    if not isinstance(count, int) or count < 0:
        raise ValueError("annotation_count must be a nonnegative integer")
    receipts = value["receipts"]
    if not isinstance(receipts, list):
        raise ValueError("receipts must be a list")
    required_stages = _required_stages(count)
    if [row.get("stage") for row in receipts] != list(required_stages[: len(receipts)]):
        raise ValueError("closed-loop receipts are missing, duplicated, or out of order")
    normalized_receipts = []
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, Mapping):
            raise ValueError("stage receipt must be a mapping")
        body = {
            "stage": receipt.get("stage"),
            "artifacts": [_artifact(item, "stage artifact") for item in receipt.get("artifacts", [])],
            "actor": _stable_id(receipt.get("actor"), "actor"),
            "timestamp": _timestamp(receipt.get("timestamp"), "timestamp"),
        }
        if receipt.get("receipt_sha256") != canonical_hash(body):
            raise ValueError(f"receipt_sha256 mismatch at index {index}")
        normalized_receipts.append({**body, "receipt_sha256": canonical_hash(body)})
    body = {
        "schema_version": SCHEMA_VERSION,
        "run_id": _stable_id(value["run_id"], "run_id"),
        "created_at": _timestamp(value["created_at"], "created_at"),
        "annotation_count": count,
        "receipts": normalized_receipts,
    }
    if value.get("state_sha256") != canonical_hash(body):
        raise ValueError("state_sha256 mismatch")
    return {**body, "state_sha256": canonical_hash(body)}


def advance_stage(
    state: dict[str, Any],
    stage: object,
    *,
    artifacts: list[Mapping[str, Any]],
    actor: object,
    timestamp: object,
) -> dict[str, Any]:
    validate_closed_loop_state(state)
    required = _required_stages(state["annotation_count"])
    next_index = len(state["receipts"])
    if next_index >= len(required):
        raise ValueError("closed-loop run is already complete")
    expected = required[next_index]
    requested = _text(stage, "stage")
    if requested != expected:
        if expected == "annotations_ingested":
            raise ValueError("annotation ingestion receipt is required before task release")
        raise ValueError(f"next required stage is {expected}")
    normalized_artifacts = [_artifact(item, "stage artifact") for item in artifacts]
    if not normalized_artifacts:
        raise ValueError("stage receipt requires at least one artifact")
    receipt_body = {
        "stage": requested,
        "artifacts": normalized_artifacts,
        "actor": _stable_id(actor, "actor"),
        "timestamp": _timestamp(timestamp, "timestamp"),
    }
    working = copy.deepcopy(state)
    working["receipts"].append(
        {**receipt_body, "receipt_sha256": canonical_hash(receipt_body)}
    )
    body = {key: working[key] for key in working if key != "state_sha256"}
    working["state_sha256"] = canonical_hash(body)
    validate_closed_loop_state(working)
    state.clear()
    state.update(working)
    return state["receipts"][-1]


def build_annotation_review(
    *,
    binding: Mapping[str, Any],
    candidate_sha256: object,
    preflight_id: object,
    generator: object,
    reviewer: object,
    reviewed_at: object,
    checks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized_binding = validate_annotation_binding(binding)
    author = _stable_id(generator, "generator")
    judge = _stable_id(reviewer, "reviewer")
    if author.casefold() == judge.casefold():
        raise ValueError("reviewer must differ from generator")
    if not isinstance(preflight_id, str) or re.fullmatch(
        r"preflight-[0-9a-f]{64}", preflight_id
    ) is None:
        raise ValueError("preflight_id is invalid")
    normalized_checks = []
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {
            "annotation_id",
            "status",
            "required_state_met",
            "unaffected_content_preserved",
            "evidence",
        }:
            raise ValueError("annotation check fields are invalid")
        if check["status"] not in {"pass", "fail", "blocked"}:
            raise ValueError("annotation check status is invalid")
        normalized_checks.append(
            {
                "annotation_id": _text(check["annotation_id"], "annotation_id"),
                "status": check["status"],
                "required_state_met": check["required_state_met"] is True,
                "unaffected_content_preserved": check["unaffected_content_preserved"] is True,
                "evidence": _artifact(check["evidence"], "annotation review evidence"),
            }
        )
    if [row["annotation_id"] for row in normalized_checks] != normalized_binding["annotation_ids"]:
        raise ValueError("annotation review must cover every annotation exactly once")
    if any(not row["unaffected_content_preserved"] for row in normalized_checks):
        raise ValueError("annotation review found unaffected content damage")
    status = "passed" if all(
        row["status"] == "pass" and row["required_state_met"]
        for row in normalized_checks
    ) else "failed"
    body = {
        "review_version": ANNOTATION_REVIEW_VERSION,
        "binding_sha256": normalized_binding["binding_sha256"],
        "candidate_sha256": _sha256(candidate_sha256, "candidate_sha256"),
        "preflight_id": preflight_id,
        "generator": author,
        "reviewer": judge,
        "reviewed_at": _timestamp(reviewed_at, "reviewed_at"),
        "checks": normalized_checks,
        "status": status,
    }
    return {**body, "review_sha256": canonical_hash(body)}


def validate_annotation_review(
    value: object, *, binding: Mapping[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("annotation review must be a mapping")
    expected = {
        "review_version",
        "binding_sha256",
        "candidate_sha256",
        "preflight_id",
        "generator",
        "reviewer",
        "reviewed_at",
        "checks",
        "status",
        "review_sha256",
    }
    if set(value) != expected or value.get("review_version") != ANNOTATION_REVIEW_VERSION:
        raise ValueError("annotation review fields or version are invalid")
    rebuilt = build_annotation_review(
        binding=binding,
        candidate_sha256=value["candidate_sha256"],
        preflight_id=value["preflight_id"],
        generator=value["generator"],
        reviewer=value["reviewer"],
        reviewed_at=value["reviewed_at"],
        checks=value["checks"],
    )
    if rebuilt != dict(value):
        raise ValueError("annotation review hash or canonical content mismatch")
    if rebuilt["status"] != "passed":
        raise ValueError("annotation review is not passed")
    return rebuilt


def _load_json_file(path: Path, name: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is unreadable") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("new-state")
    create.add_argument("--state", type=Path, required=True)
    create.add_argument("--run-id", required=True)
    create.add_argument("--created-at", required=True)
    create.add_argument("--annotation-count", type=int, required=True)

    validate = commands.add_parser("validate-state")
    validate.add_argument("--state", type=Path, required=True)

    advance = commands.add_parser("advance-stage")
    advance.add_argument("--state", type=Path, required=True)
    advance.add_argument("--stage", choices=STAGES, required=True)
    advance.add_argument("--artifacts", type=Path, required=True)
    advance.add_argument("--actor", required=True)
    advance.add_argument("--timestamp", required=True)

    preview = commands.add_parser("preview")
    preview.add_argument("--input-pages", type=Path, required=True)
    preview.add_argument("--selected-visual-pages", type=Path, required=True)
    preview.add_argument("--annotations-by-page", type=Path, required=True)
    preview.add_argument("--page-classes", type=Path, required=True)
    preview.add_argument("--effective-rule-ids", type=Path, required=True)
    preview.add_argument("--output", type=Path, required=True)

    ingest = commands.add_parser("ingest-annotations")
    ingest.add_argument("--store", type=Path, required=True)
    ingest.add_argument("--annotations", type=Path, required=True)
    ingest.add_argument("--cluster-map", type=Path, required=True)
    ingest.add_argument("--prompt-reference-hash", required=True)

    outcome = commands.add_parser("record-outcomes")
    outcome.add_argument("--store", type=Path, required=True)
    outcome.add_argument("--failure-ids", type=Path, required=True)
    outcome.add_argument("--after-candidate-path", required=True)
    outcome.add_argument("--after-candidate-sha256", required=True)
    outcome.add_argument("--effective", choices=("true", "false"), required=True)
    outcome.add_argument("--reviewed-by", required=True)
    outcome.add_argument("--reviewed-at", required=True)

    rules = commands.add_parser("load-rules")
    rules.add_argument("--store", type=Path, required=True)
    rules.add_argument("--page-id", required=True)
    rules.add_argument("--cluster-id", required=True)
    rules.add_argument("--targets", type=Path, required=True)
    rules.add_argument("--review-artifacts", type=Path, required=True)
    rules.add_argument("--output", type=Path, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "new-state":
            result = new_closed_loop_state(
                run_id=args.run_id,
                created_at=args.created_at,
                annotation_count=args.annotation_count,
            )
            _atomic_write_json(args.state, result)
        elif args.command == "validate-state":
            result = validate_closed_loop_state(
                _load_json_file(args.state, "closed-loop state")
            )
        elif args.command == "advance-stage":
            state = _load_json_file(args.state, "closed-loop state")
            artifacts = _load_json_file(args.artifacts, "stage artifacts")
            if not isinstance(artifacts, list):
                raise ValueError("stage artifacts must be a list")
            advance_stage(
                state,
                args.stage,
                artifacts=artifacts,
                actor=args.actor,
                timestamp=args.timestamp,
            )
            _atomic_write_json(args.state, state)
            result = state
        elif args.command == "preview":
            result = build_run_preview(
                input_pages=_load_json_file(args.input_pages, "input pages"),
                selected_visual_pages=_load_json_file(
                    args.selected_visual_pages, "selected visual pages"
                ),
                annotations_by_page=_load_json_file(
                    args.annotations_by_page, "annotations by page"
                ),
                page_classes=_load_json_file(args.page_classes, "page classes"),
                effective_rule_ids=_load_json_file(
                    args.effective_rule_ids, "effective rule ids"
                ),
            )
            _atomic_write_json(args.output, result)
        elif args.command == "ingest-annotations":
            result = ingest_annotations_transactionally(
                store_path=args.store,
                validated_annotations=_load_json_file(
                    args.annotations, "validated annotations"
                ),
                cluster_by_page=_load_json_file(args.cluster_map, "cluster map"),
                prompt_reference_hash=args.prompt_reference_hash,
            )
        elif args.command == "record-outcomes":
            result = record_outcomes_transactionally(
                store_path=args.store,
                failure_ids=_load_json_file(args.failure_ids, "failure ids"),
                after_candidate_path=args.after_candidate_path,
                after_candidate_sha256=args.after_candidate_sha256,
                effective=args.effective == "true",
                reviewed_by=args.reviewed_by,
                reviewed_at=args.reviewed_at,
            )
        else:
            store = _load_json_file(args.store, "failure learning store")
            result = load_effective_rules_for_page(
                store,
                page_id=args.page_id,
                cluster_id=args.cluster_id,
                targets=_load_json_file(args.targets, "targets"),
                review_artifacts=_load_json_file(
                    args.review_artifacts, "rule review artifacts"
                ),
            )
            _atomic_write_json(args.output, {"effective_rules": result})
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
