import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_appearance_matrix import validate_matrix  # noqa: E402


def valid_matrix():
    pages = ["189.jpg", "190.jpg"]
    return {
        "version": 1,
        "status": "confirmed",
        "cluster_id": "water-training",
        "cluster_pages": pages,
        "characters": [{
            "entity_id": "邓正虎",
            "reference": {"path": "人物参考图/邓正虎.png", "sha256": "a" * 64},
            "baseline": {
                "skin_tone": "暖调中浅肤色",
                "hair": "黑色高马尾",
                "facial_hair": "无",
                "clothing": "赤膊入水",
            },
            "status": "passed",
            "observations": [{
                "page": page,
                "present": True,
                "full_resolution": True,
                "skin_tone": "match",
                "hair": "match",
                "facial_hair": "match",
                "clothing": "match",
                "lighting_explanation": "same outdoor spring lighting",
                "evidence": {"path": f"evidence/{page}.png", "sha256": "b" * 64},
            } for page in pages],
        }],
    }


class AppearanceMatrixTests(unittest.TestCase):
    def test_confirmed_matrix_passes_only_with_reference_and_full_page_coverage(self):
        result = validate_matrix(valid_matrix())
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["characters"][0]["entity_id"], "邓正虎")

    def test_skin_tone_drift_blocks_confirmed_status(self):
        matrix = valid_matrix()
        matrix["characters"][0]["observations"][1]["skin_tone"] = "drift"
        with self.assertRaisesRegex(ValueError, "skin_tone drift"):
            validate_matrix(matrix)

    def test_missing_cluster_page_or_reference_is_rejected(self):
        matrix = valid_matrix()
        matrix["characters"][0]["observations"].pop()
        with self.assertRaisesRegex(ValueError, "exactly cover cluster_pages"):
            validate_matrix(matrix)
        matrix = valid_matrix()
        matrix["characters"][0]["reference"]["path"] = ""
        with self.assertRaisesRegex(ValueError, "reference.path"):
            validate_matrix(matrix)

    def test_not_visible_is_allowed_only_when_character_is_absent(self):
        matrix = valid_matrix()
        row = matrix["characters"][0]["observations"][1]
        row["skin_tone"] = "not_visible"
        with self.assertRaisesRegex(ValueError, "present observation"):
            validate_matrix(matrix)
