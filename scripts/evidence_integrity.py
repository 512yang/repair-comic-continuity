"""Evidence-integrity gates that reject post-hoc or incomplete review data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


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
