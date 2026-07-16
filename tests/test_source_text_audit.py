import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_source_text_audit import validate_source_text_audit  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SourceTextAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "input").mkdir()
        (self.root / "evidence").mkdir()
        self.page = self.root / "input" / "195.jpg"
        self.novel = self.root / "novel.txt"
        self.novel.write_text("强壮，人人平等，遇见", encoding="utf-8")
        image = Image.new("RGB", (240, 180), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 30, 180, 100), fill=(225, 220, 210), outline="black")
        draw.line((30, 45, 160, 85), fill="black", width=3)
        image.save(self.page)
        self.bbox = [20, 30, 180, 100]
        self.crop = self.root / "evidence" / "195-b1.png"
        with Image.open(self.page) as source:
            source.crop(tuple(self.bbox)).save(self.crop)

    def tearDown(self):
        self.temporary.cleanup()

    def artifact(self, path: Path) -> dict[str, str]:
        return {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": sha256(path),
        }

    def valid_audit(self) -> dict:
        block_ref = {
            "block_id": "p1-b1",
            "bbox": self.bbox,
            "transcription": "强壮，人人平等，遇见",
        }
        return {
            "version": 1,
            "status": "confirmed",
            "page": self.artifact(self.page),
            "novel": self.artifact(self.novel),
            "machine_detector_id": "rapidocr-v1",
            "visual_reviewer_id": "reviewer-2",
            "source_has_ordinary_text": True,
            "textless_review": None,
            "machine_blocks": [block_ref],
            "visual_blocks": [block_ref],
            "blocks": [
                {
                    "block_id": "p1-b1",
                    "bbox": self.bbox,
                    "crop": self.artifact(self.crop),
                    "transcription": "强壮，人人平等，遇见",
                    "decision": "passed",
                    "reason": "full-resolution source crop inspected",
                    "novel_alignment": {
                        "status": "confirmed",
                        "start": 0,
                        "end": 10,
                        "excerpt": "强壮，人人平等，遇见",
                        "semantic_decision": "faithful",
                        "reason": "meaning matches the bound novel excerpt",
                    },
                    "repeat_reviews": [
                        {
                            "text": "人",
                            "start": 3,
                            "end": 5,
                            "decision": "intentional",
                            "reason": "lexical reduplication confirmed against source",
                        }
                    ],
                    "glyph_checks": [
                        {
                            "character": "强",
                            "offset": 0,
                            "decision": "passed",
                            "reason": "bow radical and right component inspected",
                            "ocr_only": False,
                        },
                        {
                            "character": "遇",
                            "offset": 8,
                            "decision": "passed",
                            "reason": "walk radical and inner component inspected",
                            "ocr_only": False,
                        },
                    ],
                }
            ],
        }

    def test_accepts_independent_exact_coverage_with_crop_and_shape_evidence(self):
        result = validate_source_text_audit(self.valid_audit(), self.root)
        self.assertEqual(result["status"], "confirmed")

    def test_missing_visual_block_or_tampered_crop_is_rejected(self):
        audit = self.valid_audit()
        audit["visual_blocks"] = []
        with self.assertRaisesRegex(ValueError, "visual_blocks"):
            validate_source_text_audit(audit, self.root)

        audit = self.valid_audit()
        Image.new("RGB", (160, 70), "black").save(self.crop)
        audit["blocks"][0]["crop"]["sha256"] = sha256(self.crop)
        with self.assertRaisesRegex(ValueError, "crop pixels"):
            validate_source_text_audit(audit, self.root)

    def test_machine_and_visual_transcriptions_must_agree_with_audit(self):
        audit = self.valid_audit()
        audit["visual_blocks"][0]["transcription"] = "强壮，人人平等"
        with self.assertRaisesRegex(ValueError, "visual_blocks"):
            validate_source_text_audit(audit, self.root)

    def test_unreviewed_duplicate_text_is_rejected(self):
        audit = self.valid_audit()
        audit["blocks"][0]["transcription"] = "自己笨笨练错了方向"
        audit["blocks"][0]["repeat_reviews"] = []
        audit["blocks"][0]["glyph_checks"] = []
        with self.assertRaisesRegex(ValueError, "repeat_reviews"):
            validate_source_text_audit(audit, self.root)

    def test_strong_and_encounter_need_every_non_ocr_shape_check(self):
        audit = self.valid_audit()
        audit["blocks"][0]["glyph_checks"].pop()
        with self.assertRaisesRegex(ValueError, "glyph_checks"):
            validate_source_text_audit(audit, self.root)

        audit = self.valid_audit()
        audit["blocks"][0]["glyph_checks"][0]["ocr_only"] = True
        with self.assertRaisesRegex(ValueError, "ocr_only"):
            validate_source_text_audit(audit, self.root)

    def test_blocked_detail_forces_page_audit_blocked(self):
        audit = self.valid_audit()
        audit["blocks"][0]["glyph_checks"][0]["decision"] = "evidence_blocked"
        with self.assertRaisesRegex(ValueError, "status"):
            validate_source_text_audit(audit, self.root)

    def test_empty_inventory_requires_independent_textless_attestation(self):
        audit = self.valid_audit()
        audit.update(
            source_has_ordinary_text=False,
            machine_blocks=[],
            visual_blocks=[],
            blocks=[],
        )
        with self.assertRaisesRegex(ValueError, "textless_review"):
            validate_source_text_audit(audit, self.root)

        audit["textless_review"] = {
            "reviewer_id": "reviewer-3",
            "full_resolution": True,
            "reason": "full page contains artwork only",
        }
        result = validate_source_text_audit(audit, self.root)
        self.assertEqual(result["blocks"], [])

    def test_every_block_requires_exact_novel_offsets_and_semantic_decision(self):
        audit = self.valid_audit()
        audit["blocks"][0]["novel_alignment"]["excerpt"] = "wrong"
        with self.assertRaisesRegex(ValueError, "novel_alignment"):
            validate_source_text_audit(audit, self.root)

        audit = self.valid_audit()
        audit["blocks"][0]["novel_alignment"]["semantic_decision"] = "evidence_blocked"
        with self.assertRaisesRegex(ValueError, "status"):
            validate_source_text_audit(audit, self.root)


if __name__ == "__main__":
    unittest.main()
