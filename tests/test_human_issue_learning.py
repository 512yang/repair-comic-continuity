import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from failure_learning import new_failure_store, promote_rule  # noqa: E402
from human_issue_learning import ingest_human_issue_annotations  # noqa: E402
from validate_human_issue_annotations import validate_human_issue_annotations  # noqa: E402


class HumanIssueLearningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.input_dir = Path(self.temp.name) / "输入"
        self.input_dir.mkdir()
        (self.input_dir / "2.jpg").write_bytes(b"page-2")
        digest = hashlib.sha256((self.input_dir / "2.jpg").read_bytes()).hexdigest()
        document = {
            "version": 1,
            "status": "confirmed",
            "mode": "human_visual_auto_text",
            "confirmed_by": "user-reviewer",
            "confirmed_at": "2026-07-16T10:00:00+08:00",
            "annotations": [{
                "annotation_id": "page-2-beard",
                "page": {"path": "2.jpg", "sha256": digest},
                "regions": [{
                    "region_id": "lower-face",
                    "bbox_norm": [0.25, 0.10, 0.60, 0.48],
                    "description": "lower face and beard",
                }],
                "targets": ["character:hero", "prop:crown"],
                "defect_codes": ["identity_drift"],
                "observed_state": "beard and crown are inconsistent",
                "required_state": "match the identity reference",
                "instruction": "change only the annotated details",
            }],
            "learning_policy": "evidence_gated",
            "persistence_policy": "page_cluster_project_skill_candidate",
        }
        self.validated = validate_human_issue_annotations(
            document, self.input_dir, ["2.jpg"], ["2.jpg"]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_ingests_one_observed_failure_per_target_with_exact_evidence(self):
        store = new_failure_store()
        failures = ingest_human_issue_annotations(
            store,
            self.validated,
            {"2.jpg": "cluster-a"},
            "a" * 64,
        )
        self.assertEqual(len(failures), 2)
        self.assertEqual(len(store["failures"]), 2)
        self.assertEqual(store["revision"], 2)
        self.assertEqual(
            {failure["character"] for failure in failures},
            {"character:hero", "prop:crown"},
        )
        for failure in failures:
            self.assertEqual(failure["status"], "observed")
            self.assertFalse(failure["effective"])
            self.assertEqual(failure["before_candidate"]["path"], "2.jpg")
            self.assertEqual(
                failure["before_candidate"]["hash"],
                self.validated["annotations"][0]["page"]["sha256"],
            )
            self.assertIn("user-reviewer", failure["diagnosis"])
            self.assertIn("lower-face=[0.25,0.1,0.6,0.48]", failure["diagnosis"])
            self.assertIn("match the identity reference", failure["corrective_action"])
            self.assertIn("change only the annotated details", failure["corrective_action"])
        with self.assertRaisesRegex(ValueError, "effective complete before/after evidence"):
            promote_rule(
                store,
                failures[0]["failure_id"],
                "page",
                promoted_by="reviewer",
                positive_regression_passed=True,
                positive_regression_artifact_hash="1" * 64,
                clean_control_passed=True,
                clean_control_artifact_hash="2" * 64,
                variation_passed=True,
                variation_artifact_hash="3" * 64,
                independently_reviewed=True,
                independent_review_artifact_hash="4" * 64,
            )

    def test_ingestion_is_idempotent(self):
        store = new_failure_store()
        first = ingest_human_issue_annotations(
            store, self.validated, {"2.jpg": "cluster-a"}, "a" * 64
        )
        revision = store["revision"]
        second = ingest_human_issue_annotations(
            store, self.validated, {"2.jpg": "cluster-a"}, "a" * 64
        )
        self.assertEqual([row["failure_id"] for row in first], [row["failure_id"] for row in second])
        self.assertEqual(store["revision"], revision)

    def test_ingestion_rolls_back_all_records_when_any_annotation_is_invalid(self):
        store = new_failure_store()
        before = copy.deepcopy(store)
        broken = copy.deepcopy(self.validated)
        second = copy.deepcopy(broken["annotations"][0])
        second["annotation_id"] = "missing-cluster"
        second["page"]["path"] = "10.jpg"
        broken["annotations"].append(second)
        with self.assertRaisesRegex(ValueError, "cluster mapping"):
            ingest_human_issue_annotations(
                store, broken, {"2.jpg": "cluster-a"}, "a" * 64
            )
        self.assertEqual(store, before)


if __name__ == "__main__":
    unittest.main()
