import copy
import importlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageEnhance


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pipeline_contracts import canonical_hash  # noqa: E402


def candidate_preflight():
    """Import lazily so missing production code is a RED assertion."""
    module_path = SCRIPTS / "candidate_preflight.py"
    if not module_path.is_file():
        raise AssertionError("candidate_preflight.py must exist")
    return importlib.import_module("candidate_preflight")


VALID_OCR = {"ordinary_text_count": 0, "unexpected_texts": []}
REQUIRED_CHECKS = {
    "decodable",
    "dimensions",
    "blank_page",
    "edge_density_delta",
    "rgb_mean_delta",
    "rgb_std_delta",
    "text_policy",
}


def draw_pattern(path, *, size=(896, 1200), variant="normal"):
    image = Image.new("RGB", size, (78, 105, 132))
    draw = ImageDraw.Draw(image)
    width, height = size
    if variant == "blank":
        image = Image.new("RGB", size, (245, 245, 245))
    elif variant == "stripes":
        for x in range(0, width, 4):
            color = (15, 15, 15) if (x // 4) % 2 == 0 else (240, 240, 240)
            draw.rectangle((x, 0, min(x + 3, width - 1), height - 1), fill=color)
    else:
        draw.rectangle((48, 60, width - 48, height // 3), fill=(184, 116, 92))
        draw.rectangle((72, height // 2, width - 72, height - 90), fill=(52, 72, 104))
        draw.ellipse((width // 3, 180, 2 * width // 3, 520), fill=(224, 188, 142))
        for offset in range(0, min(width, height), 70):
            draw.line((0, offset, min(width - 1, offset + 280), 0), fill=(235, 225, 198), width=5)
        draw.line((40, height - 150, width - 40, 120), fill=(20, 28, 38), width=9)
    image.save(path, format="JPEG", quality=95)
    return path


class CandidatePreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.original = draw_pattern(self.root / "original.jpg")
        self.candidate = draw_pattern(self.root / "candidate.jpg")

    def tearDown(self):
        self.tempdir.cleanup()

    def run_preflight(self, candidate=None, **overrides):
        module = candidate_preflight()
        return module.run_candidate_preflight(
            candidate or self.candidate,
            self.original,
            ocr_metadata=VALID_OCR,
            **overrides,
        )

    def test_normal_candidate_passes_with_complete_deterministic_report(self):
        module = candidate_preflight()

        report = self.run_preflight()

        self.assertEqual(report["status"], "pass")
        self.assertEqual(set(report["checks"]), REQUIRED_CHECKS)
        for name, check in report["checks"].items():
            with self.subTest(check=name):
                self.assertEqual(
                    set(check),
                    {"status", "value", "threshold", "reason"},
                )
                self.assertEqual(check["status"], "pass")
                self.assertIsInstance(check["reason"], str)
                self.assertTrue(check["reason"])
        self.assertEqual(report["metrics"]["candidate_size"], [896, 1200])
        self.assertRegex(report["hashes"]["candidate"], r"\A[0-9a-f]{64}\Z")
        self.assertRegex(report["hashes"]["original"], r"\A[0-9a-f]{64}\Z")
        self.assertTrue(Path(report["paths"]["candidate"]).is_absolute())
        self.assertRegex(report["preflight_id"], r"\Apreflight-[0-9a-f]{64}\Z")
        self.assertTrue(module.validate_preflight_report(report))
        self.assertTrue(module.candidate_is_machine_eligible(report))
        self.assertEqual(report, self.run_preflight())

    def test_report_binds_normalized_evaluation_inputs_and_file_provenance(self):
        report = self.run_preflight()

        self.assertIn("evaluation_inputs", report)
        self.assertIn("provenance", report)
        self.assertEqual(
            report["evaluation_inputs"],
            {
                "expected_size": [896, 1200],
                "thresholds": report["thresholds"],
                "text_policy": "textless",
                "ocr_metadata": report["ocr_metadata"],
            },
        )
        self.assertEqual(
            report["provenance"],
            {
                "candidate_path": report["paths"]["candidate"],
                "original_path": report["paths"]["original"],
                "candidate_sha256": report["hashes"]["candidate"],
                "original_sha256": report["hashes"]["original"],
            },
        )

    def test_strong_validation_recomputes_forged_report_and_current_files(self):
        module = candidate_preflight()

        forged = self.run_preflight()
        forged["metrics"]["grayscale_variance"] += 10
        forged_body = {key: value for key, value in forged.items() if key != "preflight_id"}
        forged["preflight_id"] = "preflight-" + canonical_hash(forged_body)

        changed_path = draw_pattern(self.root / "changed.jpg")
        changed = self.run_preflight(changed_path)
        draw_pattern(changed_path, variant="stripes")

        missing_path = draw_pattern(self.root / "missing.jpg")
        missing = self.run_preflight(missing_path)
        missing_path.unlink()

        for name, report in (
            ("forged", forged),
            ("changed", changed),
            ("missing", missing),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    module.validate_preflight_report(report)

    def test_machine_eligibility_and_review_use_strong_file_validation(self):
        module = candidate_preflight()
        replaceable = draw_pattern(self.root / "replaceable.jpg")
        report = self.run_preflight(replaceable)
        draw_pattern(replaceable, variant="stripes")

        with self.assertRaises(ValueError):
            module.candidate_is_machine_eligible(report)
        with self.assertRaises(ValueError):
            module.record_independent_review(
                report,
                "generator-a",
                "reviewer-b",
                "accepted",
                "2026-07-13T12:00:00+00:00",
            )

    def test_corrupt_wrong_size_and_blank_candidates_fail_required_checks(self):
        module = candidate_preflight()
        corrupt = self.root / "corrupt.jpg"
        corrupt.write_bytes(b"not-an-image")
        wrong_size = draw_pattern(self.root / "wrong.jpg", size=(640, 900))
        blank = draw_pattern(self.root / "blank.jpg", variant="blank")

        corrupt_report = self.run_preflight(corrupt)
        wrong_report = self.run_preflight(wrong_size)
        blank_report = self.run_preflight(blank)

        self.assertEqual(corrupt_report["checks"]["decodable"]["status"], "fail")
        self.assertEqual(corrupt_report["status"], "fail")
        self.assertEqual(wrong_report["checks"]["dimensions"]["status"], "fail")
        self.assertEqual(wrong_report["status"], "fail")
        self.assertEqual(blank_report["checks"]["blank_page"]["status"], "fail")
        self.assertEqual(blank_report["status"], "fail")
        self.assertFalse(module.candidate_is_machine_eligible(blank_report))

    def test_edge_and_rgb_deltas_fail_when_thresholds_are_exceeded(self):
        stripes = draw_pattern(self.root / "stripes.jpg", variant="stripes")
        edge_report = self.run_preflight(
            stripes,
            thresholds={"max_edge_density_delta": 0.01},
        )
        self.assertEqual(edge_report["checks"]["edge_density_delta"]["status"], "fail")

        with Image.open(self.original) as source:
            bright = ImageEnhance.Brightness(source.convert("RGB")).enhance(1.65)
            contrast = ImageEnhance.Contrast(source.convert("RGB")).enhance(1.8)
        bright_path = self.root / "bright.jpg"
        contrast_path = self.root / "contrast.jpg"
        bright.save(bright_path, format="JPEG", quality=95)
        contrast.save(contrast_path, format="JPEG", quality=95)
        mean_report = self.run_preflight(
            bright_path,
            thresholds={"max_rgb_mean_delta": 4.0},
        )
        std_report = self.run_preflight(
            contrast_path,
            thresholds={"max_rgb_std_delta": 4.0},
        )

        self.assertEqual(mean_report["checks"]["rgb_mean_delta"]["status"], "fail")
        self.assertEqual(std_report["checks"]["rgb_std_delta"]["status"], "fail")

    def test_textless_ocr_policy_blocks_missing_and_fails_unexpected_text(self):
        module = candidate_preflight()
        blocked = module.run_candidate_preflight(
            self.candidate,
            self.original,
            text_policy="textless",
            ocr_metadata=None,
        )
        self.assertEqual(blocked["checks"]["text_policy"]["status"], "blocked")
        self.assertEqual(blocked["status"], "blocked")

        for metadata in (
            {"ordinary_text_count": 1, "unexpected_texts": []},
            {"ordinary_text_count": 0, "unexpected_texts": ["hello"]},
        ):
            report = module.run_candidate_preflight(
                self.candidate,
                self.original,
                ocr_metadata=metadata,
            )
            self.assertEqual(report["checks"]["text_policy"]["status"], "fail")
            self.assertEqual(report["status"], "fail")

    def test_ocr_metadata_is_deeply_validated_and_preserve_policy_is_not_applicable(self):
        module = candidate_preflight()
        invalid_rows = (
            [],
            {"ordinary_text_count": True, "unexpected_texts": []},
            {"ordinary_text_count": -1, "unexpected_texts": []},
            {"ordinary_text_count": 0, "unexpected_texts": "none"},
            {"ordinary_text_count": 0, "unexpected_texts": [1]},
            {"ordinary_text_count": 0, "unexpected_texts": [], "typo": 1},
        )
        for metadata in invalid_rows:
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    module.run_candidate_preflight(
                        self.candidate,
                        self.original,
                        ocr_metadata=metadata,
                    )

        preserved = module.run_candidate_preflight(
            self.candidate,
            self.original,
            text_policy="preserve_art_text",
            ocr_metadata=None,
        )
        text_check = preserved["checks"]["text_policy"]
        self.assertEqual(text_check["status"], "not_applicable")
        self.assertIsNone(text_check["value"])
        self.assertEqual(text_check["threshold"], "N/A")
        self.assertIn("preserve", text_check["reason"])
        self.assertEqual(preserved["status"], "pass")

    def test_allowlisted_detected_art_text_passes_and_is_normalized(self):
        module = candidate_preflight()
        metadata = {
            "ordinary_text_count": 0,
            "unexpected_texts": [],
            "allowlisted_art_text": ["轰", "轰", "雨夜"],
            "detected_art_texts": ["轰"],
        }
        try:
            report = module.run_candidate_preflight(
                self.candidate,
                self.original,
                ocr_metadata=metadata,
            )
        except ValueError as exc:
            self.fail(f"valid allowlisted art text was rejected: {exc}")

        self.assertEqual(report["checks"]["text_policy"]["status"], "pass")
        self.assertEqual(
            report["ocr_metadata"]["allowlisted_art_text"],
            ["轰", "雨夜"],
        )
        self.assertEqual(report["ocr_metadata"]["detected_art_texts"], ["轰"])

    def test_detected_art_text_outside_allowlist_fails_text_policy(self):
        module = candidate_preflight()
        metadata = {
            "ordinary_text_count": 0,
            "unexpected_texts": [],
            "allowlisted_art_text": ["轰"],
            "detected_art_texts": ["雨夜"],
        }
        try:
            report = module.run_candidate_preflight(
                self.candidate,
                self.original,
                ocr_metadata=metadata,
            )
        except ValueError as exc:
            self.fail(f"well-typed art text metadata was rejected: {exc}")

        self.assertEqual(report["checks"]["text_policy"]["status"], "fail")
        self.assertEqual(report["status"], "fail")

    def test_art_text_ocr_fields_are_deeply_validated(self):
        module = candidate_preflight()
        invalid_fields = (
            {"allowlisted_art_text": "轰"},
            {"allowlisted_art_text": [""]},
            {"allowlisted_art_text": [1]},
            {"detected_art_texts": "轰"},
            {"detected_art_texts": [1]},
        )
        for override in invalid_fields:
            metadata = VALID_OCR | override
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValueError):
                    module.run_candidate_preflight(
                        self.candidate,
                        self.original,
                        ocr_metadata=metadata,
                    )

    def test_preflight_statistics_do_not_return_or_copy_long_lived_pil_images(self):
        module = candidate_preflight()
        with patch.object(Image.Image, "copy", side_effect=AssertionError("copy forbidden")):
            try:
                report = self.run_preflight()
            except AssertionError as exc:
                self.fail(f"preflight retained a Pillow copy: {exc}")
        self.assertEqual(report["status"], "pass")

    def test_corrupt_original_fails_decode_and_blocks_comparison_checks(self):
        module = candidate_preflight()
        corrupt_original = self.root / "corrupt-original.jpg"
        corrupt_original.write_bytes(b"not-an-image")

        report = module.run_candidate_preflight(
            self.candidate,
            corrupt_original,
            ocr_metadata=VALID_OCR,
        )

        self.assertEqual(report["checks"]["decodable"]["status"], "fail")
        self.assertFalse(report["checks"]["decodable"]["value"]["original"])
        for name in ("edge_density_delta", "rgb_mean_delta", "rgb_std_delta"):
            self.assertEqual(report["checks"][name]["status"], "blocked")
        self.assertTrue(module.validate_preflight_report(report))

    def test_default_thresholds_are_reported_and_variance_boundary_is_inclusive(self):
        module = candidate_preflight()
        baseline = self.run_preflight()
        variance = baseline["metrics"]["grayscale_variance"]
        self.assertEqual(
            baseline["checks"]["blank_page"]["threshold"]["minimum"],
            module.DEFAULT_THRESHOLDS["min_grayscale_variance"],
        )

        boundary = self.run_preflight(
            thresholds={"min_grayscale_variance": variance}
        )
        beyond = self.run_preflight(
            thresholds={"min_grayscale_variance": variance + 0.000001}
        )
        self.assertEqual(boundary["checks"]["blank_page"]["status"], "pass")
        self.assertEqual(beyond["checks"]["blank_page"]["status"], "fail")

    def test_preflight_validation_rejects_tampering(self):
        module = candidate_preflight()
        report = self.run_preflight()
        variants = []
        metric = copy.deepcopy(report)
        metric["metrics"]["grayscale_variance"] += 1
        variants.append(metric)
        status = copy.deepcopy(report)
        status["checks"]["blank_page"]["status"] = "fail"
        variants.append(status)
        identifier = copy.deepcopy(report)
        identifier["preflight_id"] = "preflight-" + "0" * 64
        variants.append(identifier)

        for forged in variants:
            with self.subTest(forged=forged):
                with self.assertRaises(ValueError):
                    module.validate_preflight_report(forged)

    def test_independent_acceptance_is_required_for_final_eligibility(self):
        module = candidate_preflight()
        report = self.run_preflight()
        reviewed_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

        review = module.record_independent_review(
            report,
            "generator-a",
            "reviewer-b",
            "accepted",
            reviewed_at,
            "machine checks and visual review passed",
        )

        self.assertRegex(review["review_id"], r"\Areview-[0-9a-f]{64}\Z")
        self.assertTrue(module.validate_review(review, report))
        self.assertTrue(module.candidate_is_finally_eligible(report, review))

        rejected = module.record_independent_review(
            report,
            "generator-a",
            "reviewer-b",
            "rejected",
            reviewed_at,
        )
        self.assertFalse(module.candidate_is_finally_eligible(report, rejected))

    def test_review_rejects_same_person_naive_time_and_nonpass_acceptance(self):
        module = candidate_preflight()
        passed = self.run_preflight()
        blocked = module.run_candidate_preflight(
            self.candidate,
            self.original,
            ocr_metadata=None,
        )
        with self.assertRaisesRegex(ValueError, "independent"):
            module.record_independent_review(
                passed, "same", "same", "accepted", "2026-07-13T12:00:00+00:00"
            )
        with self.assertRaisesRegex(ValueError, "timezone"):
            module.record_independent_review(
                passed, "a", "b", "accepted", "2026-07-13T12:00:00"
            )
        with self.assertRaisesRegex(ValueError, "pass"):
            module.record_independent_review(
                blocked, "a", "b", "accepted", "2026-07-13T12:00:00+00:00"
            )

    def test_review_validation_rejects_tampering(self):
        module = candidate_preflight()
        report = self.run_preflight()
        review = module.record_independent_review(
            report,
            "generator-a",
            "reviewer-b",
            "accepted",
            "2026-07-13T12:00:00+08:00",
        )
        forged = copy.deepcopy(review)
        forged["reviewer"] = "reviewer-c"

        with self.assertRaises(ValueError):
            module.validate_review(forged, report)

    def test_candidate_batch_reuses_strict_bijection_and_report_order(self):
        module = candidate_preflight()
        original_two = draw_pattern(self.root / "original-two.jpg")
        candidate_one = draw_pattern(self.root / "1.jpg")
        candidate_two = draw_pattern(self.root / "2.jpg")
        report_one = module.run_candidate_preflight(
            candidate_one, self.original, ocr_metadata=VALID_OCR
        )
        report_two = module.run_candidate_preflight(
            candidate_two, original_two, ocr_metadata=VALID_OCR
        )
        inputs = ["1.jpg", "2.jpg"]
        mappings = [
            {"source_page": "1.jpg", "output_name": "1.jpg"},
            {"source_page": "2.jpg", "output_name": "2.jpg"},
        ]

        self.assertTrue(
            module.validate_candidate_batch(
                inputs,
                mappings,
                [report_one, report_two],
                candidate_root=self.root,
                actual_outputs=["1.jpg", "2.jpg"],
            )
        )
        invalid_batches = (
            (mappings, [report_two, report_one], ["1.jpg", "2.jpg"]),
            (list(reversed(mappings)), [report_one, report_two], ["1.jpg", "2.jpg"]),
            (mappings, [report_one], ["1.jpg", "2.jpg"]),
            (mappings, [report_one, report_two], ["1.jpg"]),
        )
        for bad_mappings, reports, actual in invalid_batches:
            with self.subTest(mappings=bad_mappings, reports=len(reports), actual=actual):
                with self.assertRaises(ValueError):
                    module.validate_candidate_batch(
                        inputs,
                        bad_mappings,
                        reports,
                        candidate_root=self.root,
                        actual_outputs=actual,
                    )

    def test_candidate_batch_matches_nested_relative_paths_with_containment(self):
        module = candidate_preflight()
        candidate_root = self.root / "candidates"
        nested_candidate = candidate_root / "chapter" / "1.jpg"
        nested_candidate.parent.mkdir(parents=True)
        nested_report = module.run_candidate_preflight(
            draw_pattern(nested_candidate), self.original, ocr_metadata=VALID_OCR
        )

        self.assertTrue(
            module.validate_candidate_batch(
                ["chapter/1.jpg"],
                [{"source_page": "chapter/1.jpg", "output_name": "chapter/1.jpg"}],
                [nested_report],
                candidate_root=candidate_root,
                actual_outputs=["chapter/1.jpg"],
            )
        )

        outside_report = module.run_candidate_preflight(
            self.candidate, self.original, ocr_metadata=VALID_OCR
        )
        with self.assertRaisesRegex(ValueError, "outside candidate root"):
            module.validate_candidate_batch(
                ["candidate.jpg"],
                [{"source_page": "candidate.jpg", "output_name": "candidate.jpg"}],
                [outside_report],
                candidate_root=candidate_root,
                actual_outputs=["candidate.jpg"],
            )


if __name__ == "__main__":
    unittest.main()
