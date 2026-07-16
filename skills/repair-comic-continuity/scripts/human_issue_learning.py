"""Bridge validated human issue annotations into the durable failure-learning store."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

from failure_learning import record_failure, validate_failure_store


SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
REVISION_ORIGIN_CODES = {
    "missed_detection": ["composition_drift"],
    "unresolved": ["composition_drift"],
    "introduced_error": ["over_rendering"],
    "damaged_correct_content": ["composition_drift"],
    "text_result_error": ["text_leak"],
}


def _region_summary(regions: list[dict[str, Any]]) -> str:
    values = []
    for region in regions:
        box = ",".join(f"{float(value):g}" for value in region["bbox_norm"])
        values.append(
            f"{region['region_id']}=[{box}]:{region['description']}"
        )
    return " | ".join(values)


def ingest_human_issue_annotations(
    store: dict[str, Any],
    validated: dict[str, Any],
    cluster_by_page: Mapping[str, str],
    prompt_reference_hash: str,
) -> list[dict[str, Any]]:
    """Record annotation-target failures atomically; never promote them."""
    validate_failure_store(store)
    if (
        not isinstance(validated, dict)
        or validated.get("mode") != "human_visual_auto_text"
        or validated.get("learning_policy") != "evidence_gated"
        or validated.get("persistence_policy")
        != "page_cluster_project_skill_candidate"
        or not isinstance(validated.get("annotations"), list)
        or not validated["annotations"]
    ):
        raise ValueError("validated human issue annotation contract mismatch")
    if not isinstance(cluster_by_page, Mapping):
        raise ValueError("cluster mapping must be a mapping")
    if (
        not isinstance(prompt_reference_hash, str)
        or SHA256_RE.fullmatch(prompt_reference_hash) is None
    ):
        raise ValueError("prompt_reference_hash must be a SHA-256 hex digest")

    for annotation in validated["annotations"]:
        page = annotation["page"]["path"]
        cluster = cluster_by_page.get(page)
        if not isinstance(cluster, str) or not cluster.strip():
            raise ValueError(f"cluster mapping is missing for annotation page: {page}")

    working = copy.deepcopy(store)
    identifiers: list[str] = []
    for annotation in validated["annotations"]:
        page = annotation["page"]
        regions = _region_summary(annotation["regions"])
        traits = ",".join(annotation.get("trait_codes", [])) or "unspecified"
        diagnosis = (
            f"user_confirmed_by={validated['confirmed_by']}; "
            f"observed_state={annotation['observed_state']}; "
            f"traits={traits}; regions={regions}"
        )
        corrective_action = (
            f"required_correction={annotation['required_state']}; "
            f"instruction={annotation['instruction']}"
        )
        for target in annotation["targets"]:
            failure = record_failure(
                working,
                page["path"],
                cluster_by_page[page["path"]],
                target,
                annotation["defect_codes"],
                diagnosis,
                corrective_action,
                page["path"],
                page["sha256"],
                prompt_reference_hash=prompt_reference_hash.lower(),
                created_at=validated["confirmed_at"],
            )
            identifiers.append(failure["failure_id"])

    validate_failure_store(working)
    store.clear()
    store.update(working)
    by_id = {row["failure_id"]: row for row in store["failures"]}
    return [by_id[identifier] for identifier in identifiers]


def ingest_human_revision_feedback(
    store: dict[str, Any],
    validated: dict[str, Any],
    cluster_by_page: Mapping[str, str],
    prompt_reference_hash: str,
) -> list[dict[str, Any]]:
    """Record immutable output-review feedback as observed failures only."""
    validate_failure_store(store)
    if (
        not isinstance(validated, dict)
        or validated.get("mode") != "revision_feedback"
        or validated.get("learning_policy") != "evidence_gated"
        or not isinstance(validated.get("feedback"), list)
        or not validated["feedback"]
    ):
        raise ValueError("validated human revision feedback contract mismatch")
    if not isinstance(cluster_by_page, Mapping):
        raise ValueError("cluster mapping must be a mapping")
    if (
        not isinstance(prompt_reference_hash, str)
        or SHA256_RE.fullmatch(prompt_reference_hash) is None
    ):
        raise ValueError("prompt_reference_hash must be a SHA-256 hex digest")
    for feedback in validated["feedback"]:
        page = feedback["page"]["path"]
        cluster = cluster_by_page.get(page)
        if not isinstance(cluster, str) or not cluster.strip():
            raise ValueError(f"cluster mapping is missing for revision page: {page}")

    working = copy.deepcopy(store)
    identifiers: list[str] = []
    for feedback in validated["feedback"]:
        page = feedback["page"]
        candidate = feedback["candidate"]
        parent_ids = ",".join(feedback["parent_annotation_ids"]) or "none"
        for region in feedback["regions"]:
            box = ",".join(f"{float(value):g}" for value in region["bbox_norm"])
            diagnosis = (
                f"revision_origin={feedback['origin']}; attempt={feedback['attempt']}; "
                f"confirmed_by={validated['confirmed_by']}; source_sha256={page['sha256']}; "
                f"parent_annotations={parent_ids}; region={region['region_id']}=[{box}]:"
                f"{region['description']}; user_note={feedback['user_note']}"
            )
            corrective_action = (
                f"resolve revision feedback {feedback['feedback_id']} in the bound region; "
                "preserve all unaffected content"
            )
            failure = record_failure(
                working,
                page["path"],
                cluster_by_page[page["path"]],
                f"revision:{region['region_id']}",
                REVISION_ORIGIN_CODES[feedback["origin"]],
                diagnosis,
                corrective_action,
                candidate["path"],
                candidate["sha256"],
                prompt_reference_hash=prompt_reference_hash.lower(),
                created_at=validated["confirmed_at"],
            )
            identifiers.append(failure["failure_id"])

    validate_failure_store(working)
    store.clear()
    store.update(working)
    by_id = {row["failure_id"]: row for row in store["failures"]}
    return [by_id[identifier] for identifier in identifiers]
