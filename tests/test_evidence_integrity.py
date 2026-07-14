import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_integrity import validate_failed_run_summary  # noqa: E402


class EvidenceIntegrityRegressionTests(unittest.TestCase):
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
