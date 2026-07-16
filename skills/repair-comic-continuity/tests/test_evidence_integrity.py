import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_integrity import (  # noqa: E402
    build_event_chain,
    validate_failed_run_summary,
    validate_v4_integrity,
)


class EvidenceIntegrityRegressionTests(unittest.TestCase):
    def valid_v4_summary(self):
        pages = ["1.jpg", "章节/2.PNG"]
        events = build_event_chain(
            [
                {"event_type": "candidate_created", "page": "1.jpg", "actor": "generator-a", "timestamp": "2026-07-15T08:00:00+08:00"},
                {"event_type": "candidate_created", "page": "章节/2.PNG", "actor": "generator-b", "timestamp": "2026-07-15T08:01:00+08:00"},
                {"event_type": "page_reviewed", "page": "1.jpg", "actor": "reviewer-a", "timestamp": "2026-07-15T08:10:00+08:00"},
                {"event_type": "page_reviewed", "page": "章节/2.PNG", "actor": "reviewer-b", "timestamp": "2026-07-15T08:11:00+08:00"},
                {"event_type": "cluster_qa", "cluster": "cluster-1", "actor": "cluster-reviewer", "timestamp": "2026-07-15T08:20:00+08:00"},
                {"event_type": "final_report", "actor": "final-reviewer", "timestamp": "2026-07-15T08:30:00+08:00"},
            ]
        )
        return {
            "schema_version": "4.0",
            "pipeline_mode": "continuity_v4",
            "input_names": pages,
            "output_names": pages,
            "alignments": [
                {"page": "1.jpg", "start_offset": 0, "end_offset": 20},
                {"page": "章节/2.PNG", "start_offset": 21, "end_offset": 45},
            ],
            "full_size_reviews": [
                {"page": page, "full_size": True, "artifact_exists": True, "artifact_hash": char * 64}
                for page, char in zip(pages, ("a", "b"))
            ],
            "required_cast": ["hero", "villain"],
            "reference_packs": [{"pack_id": "pack-1", "cast": ["hero", "villain"]}],
            "stable_pages": ["1.jpg"],
            "timeline_supported": True,
            "events": events,
            "pages": [
                {
                    "page": "1.jpg", "cluster_id": "cluster-1", "page_class": "unchanged",
                    "generator": "generator-a", "reviewer": "reviewer-a",
                    "candidate_created_at": "2026-07-15T08:00:00+08:00",
                    "reviewed_at": "2026-07-15T08:10:00+08:00",
                    "audit_status": "resolved", "preflight_status": "accepted", "task_status": "completed",
                },
                {
                    "page": "章节/2.PNG", "cluster_id": "cluster-1", "page_class": "text_only",
                    "generator": "generator-b", "reviewer": "reviewer-b",
                    "candidate_created_at": "2026-07-15T08:01:00+08:00",
                    "reviewed_at": "2026-07-15T08:11:00+08:00",
                    "audit_status": "resolved", "preflight_status": "accepted", "task_status": "completed",
                },
            ],
            "clusters": [{"cluster_id": "cluster-1", "status": "passed", "reviewed_at": "2026-07-15T08:20:00+08:00"}],
            "registry_statuses": {name: "passed" for name in (
                "scene_clusters", "style_reference_packs", "task_queue", "failure_learning",
                "scene_cluster_qa", "entity_state_timeline", "page_audit", "text_geometry", "regression_summary",
            )},
            "final_status": "passed",
            "final_reviewed_at": "2026-07-15T08:30:00+08:00",
            "unresolved_issues": 0,
        }

    def test_v4_complete_summary_passes(self):
        self.assertEqual(validate_v4_integrity(self.valid_v4_summary()), [])

    def test_v4_forged_mutations_are_rejected(self):
        cases = {
            "same_alignment_for_every_page": (
                lambda value: value["alignments"][1].update(start_offset=0, end_offset=20),
                "SUSPICIOUS_REPEATED_ALIGNMENT",
            ),
            "full_size_true_without_artifact": (
                lambda value: value["full_size_reviews"][0].update(artifact_exists=False),
                "FULL_SIZE_ARTIFACT_MISSING",
            ),
            "all_reference_packs_one_unrelated_identity": (
                lambda value: value.update(reference_packs=[{"pack_id": "pack-x", "cast": ["extra"]}]),
                "REFERENCE_CAST_COVERAGE_UNPROVEN",
            ),
            "review_before_candidate": (
                lambda value: value["pages"][0].update(reviewed_at="2026-07-15T07:59:00+08:00"),
                "REVIEW_TIME_ORDER_INVALID",
            ),
            "generator_equals_reviewer": (
                lambda value: value["pages"][0].update(reviewer="generator-a"),
                "REVIEW_NOT_INDEPENDENT",
            ),
            "cluster_pass_before_page_pass": (
                lambda value: value["clusters"][0].update(reviewed_at="2026-07-15T08:05:00+08:00"),
                "CLUSTER_QA_TIME_ORDER_INVALID",
            ),
            "passed_report_with_pending_registry": (
                lambda value: value["registry_statuses"].update(page_audit="pending"),
                "FINAL_REPORT_STATE_MISMATCH",
            ),
            "renamed_output": (
                lambda value: value.update(output_names=["0001.jpg", "0002.jpg"]),
                "OUTPUT_NAME_SET_MISMATCH",
            ),
        }
        import copy
        for name, (mutate, expected) in cases.items():
            with self.subTest(name=name):
                value = copy.deepcopy(self.valid_v4_summary())
                mutate(value)
                self.assertIn(expected, validate_v4_integrity(value))

    def test_broken_review_event_hash_chain_is_rejected(self):
        value = self.valid_v4_summary()
        value["events"][2]["event_hash"] = "f" * 64
        self.assertIn("REVIEW_EVENT_CHAIN_INVALID", validate_v4_integrity(value))

    def test_known_failed_run_is_rejected_for_every_known_gate(self):
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "failed_run_summary.json").read_text(
                encoding="utf-8"
            )
        )

        errors = validate_failed_run_summary(fixture)

        self.assertEqual(
            errors,
            [
                "OUTPUT_NAME_SET_MISMATCH",
                "ALIGNMENT_UNCONFIRMED",
                "PAGE_QA_PENDING",
                "CLUSTER_QA_MISSING",
                "STABLE_STYLE_ANCHOR_MISSING",
                "REFERENCE_CAST_COVERAGE_UNPROVEN",
                "FINAL_STATUS_NOT_PASSED",
            ],
        )

    def test_mass_filled_review_without_artifacts_is_rejected(self):
        errors = validate_failed_run_summary(
            {
                "input_names": ["191.jpg"],
                "output_names": ["191.jpg"],
                "alignment_statuses": ["confirmed"],
                "page_qa_statuses": ["passed"],
                "cluster_qa_count": 1,
                "stable_pages": ["191.jpg"],
                "reference_packs": [],
                "final_status": "passed",
                "review_artifacts": [],
                "full_size_flags": [True],
            }
        )

        self.assertEqual(errors, ["FULL_SIZE_ARTIFACT_MISSING"])


if __name__ == "__main__":
    unittest.main()
