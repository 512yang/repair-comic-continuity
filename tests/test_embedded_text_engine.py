import copy
import hashlib
import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline_contracts import canonical_hash  # noqa: E402
from test_prompt_compiler import base_v4_text_spec, refresh_text_inventory  # noqa: E402


STYLE_LOCK = {
    "font": {
        "family": "Noto Sans SC",
        "asset_sha256": "a" * 64,
        "match_method": "exact_asset",
        "confidence": 1.0,
    },
    "font_size_px": 32.0,
    "fill_rgba": [18, 18, 18, 255],
    "stroke_rgba": [255, 255, 255, 0],
    "stroke_width_px": 0.0,
    "letter_spacing_px": 0.0,
    "line_spacing_px": 6.0,
    "writing_mode": "horizontal-tb",
    "alignment": "left",
    "rotation_deg": 0.0,
    "anchor": [100.0, 120.0],
    "line_boxes": [[100, 120, 420, 170]],
}


def with_style_lock():
    spec = base_v4_text_spec()
    lock = copy.deepcopy(STYLE_LOCK)
    lock["style_sha256"] = canonical_hash(lock)
    spec["source_text_inventory"]["regions"][0]["style_lock"] = copy.deepcopy(lock)
    spec["blocks"][0]["style_lock"] = copy.deepcopy(lock)
    refresh_text_inventory(spec)
    return spec


class EmbeddedTextEngineTests(unittest.TestCase):
    def test_text_engine_code_fonts_and_licenses_are_bundled(self):
        expected = [
            ROOT / "scripts" / "text_engine_pipeline.py",
            ROOT / "scripts" / "comic_repair" / "style_analysis.py",
            ROOT / "scripts" / "comic_repair" / "font_matcher.py",
            ROOT / "scripts" / "comic_repair" / "font_catalog.py",
            ROOT / "scripts" / "comic_repair" / "input_order.py",
            ROOT / "scripts" / "comic_repair" / "qa.py",
            ROOT / "assets" / "text_fonts" / "catalog.json",
        ]
        for path in expected:
            with self.subTest(path=path):
                self.assertTrue(path.is_file(), f"missing bundled text resource: {path}")

        catalog = importlib.import_module("comic_repair.font_catalog")
        items = catalog.load_catalog(ROOT / "assets" / "text_fonts" / "catalog.json")
        self.assertGreaterEqual(len(items), 7)
        self.assertTrue(all(item.path.is_file() for item in items))
        self.assertTrue(all(item.license_path.is_file() for item in items))

    def test_text_request_rejects_missing_original_style_lock(self):
        compiler = importlib.import_module("prompt_compiler")
        spec = base_v4_text_spec()
        del spec["source_text_inventory"]["regions"][0]["style_lock"]
        del spec["blocks"][0]["style_lock"]
        refresh_text_inventory(spec)
        with self.assertRaisesRegex(ValueError, "style_lock"):
            compiler.compile_text_repair_request(spec)

    def test_text_request_preserves_exact_original_style_lock(self):
        compiler = importlib.import_module("prompt_compiler")
        request = compiler.compile_text_repair_request(with_style_lock())
        self.assertEqual(request["declaration"]["blocks"][0]["style_lock"], {
            **STYLE_LOCK,
            "style_sha256": canonical_hash(STYLE_LOCK),
        })

    def test_render_manifest_rejects_style_or_geometry_drift(self):
        preflight = importlib.import_module("candidate_preflight")
        request = importlib.import_module("prompt_compiler").compile_text_repair_request(
            with_style_lock()
        )
        block = request["declaration"]["blocks"][0]
        valid = {
            "block_id": block["block_id"],
            "bbox": block["bbox"],
            "style_lock": copy.deepcopy(block["style_lock"]),
        }
        self.assertTrue(preflight.validate_render_style_contract([block], [valid]))

        for field, value in (
            ("font_size_px", 28.0),
            ("fill_rgba", [0, 0, 0, 255]),
            ("anchor", [101.0, 120.0]),
            ("line_boxes", [[100, 121, 420, 171]]),
        ):
            with self.subTest(field=field):
                drifted = copy.deepcopy(valid)
                drifted["style_lock"][field] = value
                drifted["style_lock"]["style_sha256"] = canonical_hash(
                    {k: v for k, v in drifted["style_lock"].items() if k != "style_sha256"}
                )
                with self.assertRaisesRegex(ValueError, "style|geometry"):
                    preflight.validate_render_style_contract([block], [drifted])

    def test_text_engine_preserves_exact_relative_input_name(self):
        engine = importlib.import_module("text_engine_pipeline")
        for name in ("0003.jpg", "252（1）.jpg", "章节一/页面 01.PNG"):
            with self.subTest(name=name):
                self.assertEqual(engine.final_relative_output_name(name), name)

    def test_runtime_does_not_require_legacy_external_project(self):
        engine = importlib.import_module("text_engine_pipeline")
        resource_root = engine.bundled_font_root()
        self.assertTrue(resource_root.is_relative_to(ROOT))
        self.assertNotIn("漫画文字修复", str(resource_root))

    def test_measured_style_and_font_match_compile_to_hash_bound_style_lock(self):
        contract = importlib.import_module("text_style_contract")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSansSC-VF.ttf"
        style = styles.TextStyle(
            fill=(18, 18, 18),
            orientation="horizontal",
            font_size=32,
            weight_score=0.5,
            width_ratio=1.0,
            char_gap=0,
            line_gap=6,
            stroke_width=0,
            category_scores={"sans": 1.0},
            confidence=0.98,
            warnings=[],
        )
        match = matcher.FontMatch(
            family="Noto Sans SC",
            path=font_path,
            weight=400,
            score=0.96,
            confidence=0.97,
            fallback_used=False,
            top_candidates=[],
            warnings=[],
        )
        lock = contract.style_lock_from_measurements(
            style,
            match,
            bbox=[100, 120, 420, 330],
            line_boxes=[[100, 120, 420, 170]],
            alignment="left",
        )
        self.assertEqual(lock["font"]["asset_sha256"], hashlib.sha256(font_path.read_bytes()).hexdigest())
        self.assertEqual(lock["fill_rgba"], [18, 18, 18, 255])
        self.assertEqual(lock["font_size_px"], 32.0)
        self.assertEqual(lock["anchor"], [100.0, 120.0])
        body = {key: value for key, value in lock.items() if key != "style_sha256"}
        self.assertEqual(lock["style_sha256"], canonical_hash(body))

    def test_uncertain_font_or_style_is_blocked_instead_of_guessed(self):
        contract = importlib.import_module("text_style_contract")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSansSC-VF.ttf"
        style = styles.TextStyle(
            fill=(0, 0, 0), orientation="horizontal", font_size=24,
            weight_score=0.4, width_ratio=1.0, char_gap=0, line_gap=4,
            stroke_width=0, category_scores={"sans": 1.0}, confidence=0.70,
            warnings=["style_low_confidence"],
        )
        match = matcher.FontMatch(
            family="Noto Sans SC", path=font_path, weight=400, score=0.7,
            confidence=0.70, fallback_used=True, top_candidates=[], warnings=["font_match_low_confidence"],
        )
        with self.assertRaisesRegex(contract.StyleEvidenceBlocked, "confidence"):
            contract.style_lock_from_measurements(
                style, match, bbox=[0, 0, 200, 100], line_boxes=[[0, 0, 200, 50]]
            )


if __name__ == "__main__":
    unittest.main()
