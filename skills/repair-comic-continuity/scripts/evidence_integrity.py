"""Evidence-integrity gates that reject post-hoc or incomplete review data."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pipeline_contracts import canonical_hash


V4_PAGE_CLASSES = frozenset({"unchanged", "text_only", "full_page_redraw"})
V4_REQUIRED_REGISTRIES = frozenset(
    {
        "scene_clusters",
        "style_reference_packs",
        "task_queue",
        "failure_learning",
        "scene_cluster_qa",
        "entity_state_timeline",
        "page_audit",
        "text_geometry",
        "regression_summary",
    }
)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _add(errors: list[str], code: str) -> None:
    if code not in errors:
        errors.append(code)


def build_event_chain(events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return canonical append-only review events with a SHA-256 hash chain."""
    chained: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for source in events:
        event = copy.deepcopy(dict(source))
        if "event_type" in event and "type" not in event:
            event["type"] = event.pop("event_type")
        if "event_hash" in event or "previous_event_hash" in event:
            raise ValueError("event source contains reserved chain fields")
        event["previous_event_hash"] = previous_hash
        event["event_hash"] = canonical_hash(event)
        chained.append(event)
        previous_hash = event["event_hash"]
    return chained


def _valid_event_chain(events: object) -> bool:
    if not isinstance(events, list) or not events:
        return False
    previous_hash: str | None = None
    for event in events:
        if not isinstance(event, Mapping) or event.get("previous_event_hash") != previous_hash:
            return False
        claimed = event.get("event_hash")
        if not isinstance(claimed, str) or len(claimed) != 64:
            return False
        body = {key: value for key, value in event.items() if key != "event_hash"}
        if canonical_hash(body) != claimed:
            return False
        previous_hash = claimed
    return True


def _valid_alignment_exception(row: Mapping[str, Any]) -> bool:
    exception = row.get("reviewed_exception")
    if not isinstance(exception, Mapping):
        return False
    page = row.get("page") or row.get("output_name")
    return (
        isinstance(exception.get("reviewer"), str)
        and bool(exception["reviewer"].strip())
        and isinstance(exception.get("reason"), str)
        and bool(exception["reason"].strip())
        and isinstance(exception.get("pages"), list)
        and page in exception["pages"]
        and exception.get("source_range")
        == [row.get("start_offset"), row.get("end_offset")]
    )


def validate_v4_integrity(summary: Mapping[str, Any]) -> list[str]:
    """Reject forged or incomplete V4 final evidence without mutating it."""
    errors: list[str] = []
    if not isinstance(summary, Mapping):
        return ["FINAL_REPORT_STATE_MISMATCH"]
    if summary.get("schema_version") != "4.0" or summary.get("pipeline_mode") != "continuity_v4":
        _add(errors, "FINAL_REPORT_STATE_MISMATCH")

    input_names = summary.get("input_names")
    output_names = summary.get("output_names")
    if (
        not isinstance(input_names, list)
        or not input_names
        or not all(isinstance(name, str) and name for name in input_names)
        or output_names != input_names
    ):
        _add(errors, "OUTPUT_NAME_SET_MISMATCH")
        input_names = input_names if isinstance(input_names, list) else []

    alignments = summary.get("alignments")
    if not isinstance(alignments, list) or len(alignments) != len(input_names):
        _add(errors, "SUSPICIOUS_REPEATED_ALIGNMENT")
    else:
        spans = [
            (row.get("start_offset"), row.get("end_offset"))
            for row in alignments
            if isinstance(row, Mapping)
        ]
        if (
            len(spans) > 1
            and len(set(spans)) == 1
            and not all(
                isinstance(row, Mapping) and _valid_alignment_exception(row)
                for row in alignments
            )
        ):
            _add(errors, "SUSPICIOUS_REPEATED_ALIGNMENT")

    reviews = summary.get("full_size_reviews")
    if not isinstance(reviews, list) or len(reviews) != len(input_names):
        _add(errors, "FULL_SIZE_ARTIFACT_MISSING")
    else:
        for review in reviews:
            digest = review.get("artifact_hash") if isinstance(review, Mapping) else None
            if (
                not isinstance(review, Mapping)
                or review.get("full_size") is not True
                or review.get("artifact_exists") is not True
                or not isinstance(digest, str)
                or len(digest) != 64
            ):
                _add(errors, "FULL_SIZE_ARTIFACT_MISSING")
                break

    required_cast = summary.get("required_cast")
    packs = summary.get("reference_packs")
    covered_cast: set[str] = set()
    if isinstance(packs, list):
        for pack in packs:
            if isinstance(pack, Mapping) and isinstance(pack.get("cast"), list):
                covered_cast.update(item for item in pack["cast"] if isinstance(item, str))
    if not isinstance(required_cast, list) or not set(required_cast).issubset(covered_cast):
        _add(errors, "REFERENCE_CAST_COVERAGE_UNPROVEN")
    if not summary.get("stable_pages"):
        _add(errors, "STABLE_STYLE_ANCHOR_MISSING")
    if summary.get("timeline_supported") is not True:
        _add(errors, "TIMELINE_TRANSITION_UNSUPPORTED")

    events = summary.get("events")
    if not _valid_event_chain(events):
        _add(errors, "REVIEW_EVENT_CHAIN_INVALID")

    pages = summary.get("pages")
    page_rows = pages if isinstance(pages, list) else []
    if len(page_rows) != len(input_names):
        _add(errors, "FINAL_REPORT_STATE_MISMATCH")
    page_review_times: dict[str, datetime] = {}
    page_clusters: dict[str, str] = {}
    for row in page_rows:
        if not isinstance(row, Mapping):
            _add(errors, "FINAL_REPORT_STATE_MISMATCH")
            continue
        page = row.get("page")
        cluster = row.get("cluster_id")
        created = _time(row.get("candidate_created_at"))
        reviewed = _time(row.get("reviewed_at"))
        if created is None or reviewed is None or reviewed <= created:
            _add(errors, "REVIEW_TIME_ORDER_INVALID")
        if row.get("generator") == row.get("reviewer") or not row.get("reviewer"):
            _add(errors, "REVIEW_NOT_INDEPENDENT")
        if (
            row.get("page_class") not in V4_PAGE_CLASSES
            or row.get("audit_status") != "resolved"
            or row.get("preflight_status") != "accepted"
            or row.get("task_status") != "completed"
        ):
            _add(errors, "FINAL_REPORT_STATE_MISMATCH")
        if isinstance(page, str) and reviewed is not None:
            page_review_times[page] = reviewed
        if isinstance(page, str) and isinstance(cluster, str) and cluster:
            page_clusters[page] = cluster
        else:
            _add(errors, "FINAL_REPORT_STATE_MISMATCH")

    clusters = summary.get("clusters")
    cluster_times: list[datetime] = []
    if not isinstance(clusters, list) or not clusters:
        _add(errors, "FINAL_REPORT_STATE_MISMATCH")
    else:
        for cluster in clusters:
            if not isinstance(cluster, Mapping) or cluster.get("status") != "passed":
                _add(errors, "FINAL_REPORT_STATE_MISMATCH")
                continue
            cluster_id = cluster.get("cluster_id")
            reviewed = _time(cluster.get("reviewed_at"))
            member_times = [
                page_review_times[page]
                for page, bound_cluster in page_clusters.items()
                if bound_cluster == cluster_id and page in page_review_times
            ]
            if reviewed is None or not member_times or reviewed <= max(member_times):
                _add(errors, "CLUSTER_QA_TIME_ORDER_INVALID")
            else:
                cluster_times.append(reviewed)

    statuses = summary.get("registry_statuses")
    if (
        not isinstance(statuses, Mapping)
        or not V4_REQUIRED_REGISTRIES.issubset(statuses)
        or any(statuses.get(name) != "passed" for name in V4_REQUIRED_REGISTRIES)
        or summary.get("final_status") != "passed"
        or summary.get("unresolved_issues") != 0
    ):
        _add(errors, "FINAL_REPORT_STATE_MISMATCH")
    final_time = _time(summary.get("final_reviewed_at"))
    if final_time is None or (cluster_times and final_time <= max(cluster_times)):
        _add(errors, "FINAL_REPORT_STATE_MISMATCH")
    return errors


def validate_failed_run_summary(summary: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if set(summary.get("input_names", [])) != set(summary.get("output_names", [])):
        errors.append("OUTPUT_NAME_SET_MISMATCH")
    if any(value != "confirmed" for value in summary.get("alignment_statuses", [])):
        errors.append("ALIGNMENT_UNCONFIRMED")
    if any(value != "passed" for value in summary.get("page_qa_statuses", [])):
        errors.append("PAGE_QA_PENDING")
    if summary.get("cluster_qa_count", 0) == 0:
        errors.append("CLUSTER_QA_MISSING")
    if not summary.get("stable_pages"):
        errors.append("STABLE_STYLE_ANCHOR_MISSING")
    if summary.get("reference_packs") and not summary.get("cast_coverage"):
        errors.append("REFERENCE_CAST_COVERAGE_UNPROVEN")
    if summary.get("final_status") != "passed":
        errors.append("FINAL_STATUS_NOT_PASSED")
    flags = summary.get("full_size_flags", [])
    artifacts = summary.get("review_artifacts", [])
    if any(flags) and not artifacts:
        errors.append("FULL_SIZE_ARTIFACT_MISSING")
    return errors
