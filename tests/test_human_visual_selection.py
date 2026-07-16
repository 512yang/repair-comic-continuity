import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_human_visual_selection import validate_human_visual_selection  # noqa: E402


class HumanVisualSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.input_dir = Path(self.temp.name) / "输入"
        self.input_dir.mkdir()
        for name, payload in (
            ("2.jpg", b"page-2"),
            ("10.jpg", b"page-10"),
            ("20.jpg", b"page-20"),
        ):
            (self.input_dir / name).write_bytes(payload)
        self.input_names = ["2.jpg", "10.jpg", "20.jpg"]

    def tearDown(self):
        self.temp.cleanup()

    def row(self, name):
        path = self.input_dir / name
        return {
            "path": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def manifest(self, selected_pages=None):
        return {
            "version": 1,
            "status": "confirmed",
            "mode": "human_visual_auto_text",
            "selected_pages": selected_pages if selected_pages is not None else [],
            "visual_policy": "selected_pages_only",
            "text_policy": "all_input_pages_page_reset_preserve_style",
            "output_policy": "exact_input_bijection",
        }

    def test_accepts_hash_bound_natural_ordered_selection_and_empty_selection(self):
        result = validate_human_visual_selection(
            self.manifest([self.row("2.jpg"), self.row("20.jpg")]),
            self.input_dir,
            self.input_names,
        )
        self.assertEqual(result["selected_pages"], ["2.jpg", "20.jpg"])
        self.assertEqual(result["selected_page_count"], 2)

        empty = validate_human_visual_selection(
            self.manifest(), self.input_dir, self.input_names
        )
        self.assertEqual(empty["selected_pages"], [])

    def test_rejects_unknown_duplicate_or_reordered_pages(self):
        unknown = self.row("2.jpg")
        unknown["path"] = "3.jpg"
        with self.assertRaisesRegex(ValueError, "unknown input page"):
            validate_human_visual_selection(
                self.manifest([unknown]), self.input_dir, self.input_names
            )

        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_human_visual_selection(
                self.manifest([self.row("2.jpg"), self.row("2.jpg")]),
                self.input_dir,
                self.input_names,
            )

        with self.assertRaisesRegex(ValueError, "natural input order"):
            validate_human_visual_selection(
                self.manifest([self.row("20.jpg"), self.row("2.jpg")]),
                self.input_dir,
                self.input_names,
            )

    def test_rejects_source_hash_drift_and_unsafe_paths(self):
        drifted = self.row("10.jpg")
        drifted["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "sha256 mismatch"):
            validate_human_visual_selection(
                self.manifest([drifted]), self.input_dir, self.input_names
            )

        unsafe = self.row("2.jpg")
        unsafe["path"] = "../2.jpg"
        with self.assertRaisesRegex(ValueError, "safe relative path"):
            validate_human_visual_selection(
                self.manifest([unsafe]), self.input_dir, self.input_names
            )

    def test_rejects_policy_or_schema_drift(self):
        wrong_policy = self.manifest()
        wrong_policy["text_policy"] = "selected_pages_only"
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            validate_human_visual_selection(
                wrong_policy, self.input_dir, self.input_names
            )

        extra_key = self.manifest()
        extra_key["notes"] = "unchecked override"
        with self.assertRaisesRegex(ValueError, "exact keys"):
            validate_human_visual_selection(
                extra_key, self.input_dir, self.input_names
            )


if __name__ == "__main__":
    unittest.main()
