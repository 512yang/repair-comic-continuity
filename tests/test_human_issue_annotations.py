import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_human_issue_annotations import validate_human_issue_annotations  # noqa: E402


class HumanIssueAnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.input_dir = Path(self.temp.name) / "输入"
        self.input_dir.mkdir()
        for name, payload in (("2.jpg", b"page-2"), ("10.jpg", b"page-10")):
            (self.input_dir / name).write_bytes(payload)
        self.input_names = ["2.jpg", "10.jpg"]

    def tearDown(self):
        self.temp.cleanup()

    def page(self, name="2.jpg"):
        path = self.input_dir / name
        return {
            "path": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def annotation(self, annotation_id="page-2-beard", page="2.jpg"):
        return {
            "annotation_id": annotation_id,
            "page": self.page(page),
            "regions": [{
                "region_id": "lower-face",
                "bbox_norm": [0.25, 0.10, 0.60, 0.48],
                "description": "lower face and beard",
            }],
            "targets": ["character:hero"],
            "defect_codes": ["identity_drift"],
            "observed_state": "beard is missing",
            "required_state": "restore the reference beard",
            "instruction": "change only the beard and related facial detail",
        }

    def manifest(self, annotations=None):
        return {
            "version": 1,
            "status": "confirmed",
            "mode": "human_visual_auto_text",
            "confirmed_by": "user",
            "confirmed_at": "2026-07-16T10:00:00+08:00",
            "annotations": annotations if annotations is not None else [self.annotation()],
            "learning_policy": "evidence_gated",
            "persistence_policy": "page_cluster_project_skill_candidate",
        }

    def validate(self, document, selected_pages=None):
        return validate_human_issue_annotations(
            document,
            self.input_dir,
            self.input_names,
            ["2.jpg"] if selected_pages is None else selected_pages,
        )

    def test_accepts_hash_bound_regions_and_normalizes_learning_evidence(self):
        document = self.manifest()
        document["annotations"][0]["trait_codes"] = ["facial_hair", "skin_tone"]
        document["annotations"][0]["regions"].append({
            "region_id": "chin",
            "bbox_norm": [0.30, 0.32, 0.55, 0.52],
            "description": "chin contour",
        })
        result = self.validate(document)
        self.assertEqual(result["annotation_count"], 1)
        self.assertEqual(result["region_count"], 2)
        self.assertEqual(result["target_count"], 1)
        self.assertEqual(result["annotated_pages"], ["2.jpg"])
        self.assertEqual(result["confirmed_at"], "2026-07-16T02:00:00+00:00")
        self.assertEqual(result["annotations"][0]["defect_codes"], ["identity_drift"])
        self.assertEqual(
            result["annotations"][0]["trait_codes"],
            ["facial_hair", "skin_tone"],
        )

    def test_rejects_unknown_or_duplicate_trait_codes(self):
        unknown = self.manifest()
        unknown["annotations"][0]["trait_codes"] = ["beard_magic"]
        with self.assertRaisesRegex(ValueError, "unknown trait code"):
            self.validate(unknown)

        duplicate = self.manifest()
        duplicate["annotations"][0]["trait_codes"] = ["facial_hair", "facial_hair"]
        with self.assertRaisesRegex(ValueError, "duplicate trait code"):
            self.validate(duplicate)

    def test_rejects_empty_annotations_and_schema_or_policy_drift(self):
        empty = self.manifest([])
        with self.assertRaisesRegex(ValueError, "annotations must be nonempty"):
            self.validate(empty)

        extra = self.manifest()
        extra["notes"] = "unchecked override"
        with self.assertRaisesRegex(ValueError, "exact keys"):
            self.validate(extra)

        policy = self.manifest()
        policy["learning_policy"] = "learn_immediately"
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            self.validate(policy)

    def test_rejects_unsafe_unknown_unselected_or_hash_drifted_pages(self):
        cases = []
        unsafe = self.manifest()
        unsafe["annotations"][0]["page"]["path"] = "../2.jpg"
        cases.append((unsafe, "safe relative path"))
        unknown = self.manifest()
        unknown["annotations"][0]["page"]["path"] = "3.jpg"
        cases.append((unknown, "unknown input page"))
        unselected = self.manifest([self.annotation(page="10.jpg")])
        cases.append((unselected, "not in human visual selection"))
        drifted = self.manifest()
        drifted["annotations"][0]["page"]["sha256"] = "0" * 64
        cases.append((drifted, "sha256 mismatch"))
        for document, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    self.validate(document)

    def test_rejects_duplicate_annotation_region_or_target_ids(self):
        duplicate_annotation = self.manifest(
            [self.annotation(), self.annotation()]
        )
        with self.assertRaisesRegex(ValueError, "duplicate annotation_id"):
            self.validate(duplicate_annotation)

        duplicate_region = self.manifest()
        duplicate_region["annotations"][0]["regions"].append(
            copy.deepcopy(duplicate_region["annotations"][0]["regions"][0])
        )
        with self.assertRaisesRegex(ValueError, "duplicate region_id"):
            self.validate(duplicate_region)

        duplicate_target = self.manifest()
        duplicate_target["annotations"][0]["targets"] = [
            "character:hero", "character:hero"
        ]
        with self.assertRaisesRegex(ValueError, "duplicate target"):
            self.validate(duplicate_target)

    def test_rejects_invalid_time_failure_code_or_normalized_box(self):
        naive = self.manifest()
        naive["confirmed_at"] = "2026-07-16T10:00:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            self.validate(naive)

        code = self.manifest()
        code["annotations"][0]["defect_codes"] = ["beard_magic"]
        with self.assertRaisesRegex(ValueError, "unknown defect code"):
            self.validate(code)

        for bbox in (
            [-0.1, 0.1, 0.5, 0.5],
            [0.5, 0.1, 0.5, 0.5],
            [0.2, 0.6, 0.5, 0.4],
            [0.2, 0.3, 1.1, 0.8],
            [0.2, 0.3, 0.8],
        ):
            with self.subTest(bbox=bbox):
                document = self.manifest()
                document["annotations"][0]["regions"][0]["bbox_norm"] = bbox
                with self.assertRaisesRegex(ValueError, "bbox_norm"):
                    self.validate(document)


if __name__ == "__main__":
    unittest.main()
