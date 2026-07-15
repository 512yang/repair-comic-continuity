import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_detection_benchmark import validate_detection_benchmark  # noqa: E402


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class DetectionBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.golden = self.root / "golden.json"
        self.detected = self.root / "detected.json"
        self.base = {
            "schema_version": "1.0",
            "pipeline_id": "continuity_v5_unified",
            "pages": [
                {"page": "189.jpg", "status": "passed_current_review", "visual_issue_codes": [], "text_issue_codes": []},
                {"page": "191.jpg", "status": "confirmed_defect", "visual_issue_codes": ["SKIN_TONE_DRIFT"], "text_issue_codes": ["MALFORMED_GLYPH"]},
                {"page": "192.jpg", "status": "confirmed_defect", "visual_issue_codes": [], "text_issue_codes": ["TEXT_DUPLICATION"]},
                {"page": "201.jpg", "status": "evidence_blocked", "visual_issue_codes": [], "text_issue_codes": ["SFX_UNCERTAIN"]},
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_detection_passes_with_zero_misses_and_correct_page_protection(self):
        write_json(self.golden, self.base)
        write_json(self.detected, self.base)
        result = validate_detection_benchmark(self.golden, self.detected)
        self.assertEqual(result["status"], "benchmark_passed")
        self.assertEqual(result["missed_confirmed_defects"], [])
        self.assertEqual(result["false_positive_defects"], [])
        self.assertEqual(result["visual_recall"], 1.0)
        self.assertEqual(result["text_recall"], 1.0)
        self.assertEqual(result["correct_page_false_positive_count"], 0)

    def test_missed_defect_or_false_positive_blocks_release(self):
        write_json(self.golden, self.base)
        missed = json.loads(json.dumps(self.base))
        missed["pages"][1]["text_issue_codes"] = []
        write_json(self.detected, missed)
        with self.assertRaisesRegex(ValueError, "missed confirmed defect"):
            validate_detection_benchmark(self.golden, self.detected)

        false_positive = json.loads(json.dumps(self.base))
        false_positive["pages"][0]["status"] = "confirmed_defect"
        false_positive["pages"][0]["visual_issue_codes"] = ["COSTUME_DRIFT"]
        write_json(self.detected, false_positive)
        with self.assertRaisesRegex(ValueError, "false positive"):
            validate_detection_benchmark(self.golden, self.detected)

    def test_pipeline_or_page_coverage_mismatch_is_rejected(self):
        write_json(self.golden, self.base)
        wrong = json.loads(json.dumps(self.base))
        wrong["pipeline_id"] = "continuity_v4"
        write_json(self.detected, wrong)
        with self.assertRaisesRegex(ValueError, "pipeline_id"):
            validate_detection_benchmark(self.golden, self.detected)

        missing = json.loads(json.dumps(self.base))
        missing["pages"].pop()
        write_json(self.detected, missing)
        with self.assertRaisesRegex(ValueError, "page coverage"):
            validate_detection_benchmark(self.golden, self.detected)


if __name__ == "__main__":
    unittest.main()
