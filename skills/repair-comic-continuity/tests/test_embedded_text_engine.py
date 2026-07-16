import copy
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


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
    "horizontal_scale": 1.0,
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

    def test_serialized_analysis_block_contains_style_gate_evidence(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSansSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("测试", 0.99, (10, 20, 80, 50), [])],
            box=(10, 20, 100, 70),
            kind="dialogue",
            rewrite_text="测试",
            original_text="测试",
            confidence=0.99,
            orientation="horizontal",
            fill=(10, 10, 10),
            style=styles.TextStyle(
                fill=(10, 10, 10), orientation="horizontal", font_size=24,
                weight_score=0.4, width_ratio=1.0, char_gap=0, line_gap=4,
                stroke_width=0, category_scores={"sans": 1.0}, confidence=0.70,
                warnings=["style_low_confidence"],
            ),
            font_match=matcher.FontMatch(
                family="Noto Sans SC", path=font_path, weight=400, score=0.7,
                confidence=0.70, fallback_used=True, top_candidates=[],
                warnings=["font_match_low_confidence"],
            ),
        )
        payload = engine.block_to_json(block)
        self.assertEqual(payload["line_boxes"], [[10, 20, 80, 50]])
        self.assertIsNone(payload["style_lock"])
        self.assertEqual(payload["style_gate"]["status"], "evidence_blocked")
        self.assertIn("confidence", payload["style_gate"]["reason"])

    def test_dry_run_analysis_artifacts_persist_blocking_gate(self):
        engine = importlib.import_module("text_engine_pipeline")
        manifest = [{
            "file": "001.jpg",
            "source_relative": "001.jpg",
            "blocks": [{
                "kind": "dialogue",
                "style_lock": None,
                "style_gate": {
                    "status": "evidence_blocked",
                    "reason": "style/font confidence below 0.95",
                },
            }],
        }]
        with tempfile.TemporaryDirectory() as temporary:
            report = engine.write_analysis_artifacts(Path(temporary), manifest, [])
            self.assertEqual(report["status"], "evidence_blocked")
            analysis = json.loads(
                (Path(temporary) / "analysis_manifest.json").read_text(encoding="utf-8")
            )
            gate = json.loads(
                (Path(temporary) / "style_gate_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(analysis["pages"], manifest)
            self.assertEqual(gate["blocked_block_count"], 1)
            self.assertEqual(gate["blocked_pages"], ["001.jpg"])

    def test_page_scoped_source_matcher_repairs_duplicate_ocr_text(self):
        engine = importlib.import_module("text_engine_pipeline")
        matcher = engine.make_page_source_matcher(
            "邓正虎说道：“我没进入一道宗之前，修炼了家里祖传的一门功夫，"
            "叫做锻体五行诀。”"
        )
        matched = matcher("我没进入一道宗之前，修炼了家里祖祖传的的一门功夫")
        self.assertIsNotNone(matched)
        self.assertGreaterEqual(matched["score"], 0.90)
        self.assertEqual(
            matched["text"],
            "我没进入一道宗之前，修炼了家里祖传的一门功夫",
        )

    def test_only_confirmed_alignment_can_bind_page_source_matchers(self):
        engine = importlib.import_module("text_engine_pipeline")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "page_0001.jpg"
            image.write_bytes(b"test")
            page = engine.InputPage(1, "001.jpg", "page_0001.jpg", image)
            alignment = root / "alignment.json"
            payload = {
                "status": "confirmed",
                "pages": [{
                    "output_name": "001.jpg",
                    "status": "confirmed",
                    "source_excerpt": "原文测试句。",
                    "context_excerpt": "原文测试句。",
                }],
            }
            alignment.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            matchers = engine.load_confirmed_page_matchers(alignment, [page])
            self.assertIn("page_0001.jpg", matchers)
            self.assertEqual(matchers["page_0001.jpg"]("原文测试句")["text"], "原文测试句")

            payload["status"] = "candidate"
            alignment.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "confirmed"):
                engine.load_confirmed_page_matchers(alignment, [page])

    def test_reviewed_style_lock_never_silently_shrinks_text(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSerifSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("这是一段无法塞进小框的文字", 1.0, (0, 0, 24, 24), [])],
            box=(0, 0, 24, 24), kind="dialogue",
            rewrite_text="这是一段无法塞进小框的文字",
            original_text="原文", confidence=1.0, orientation="horizontal",
            fill=(18, 19, 20),
            style=styles.TextStyle(
                fill=(18, 19, 20), orientation="horizontal", font_size=32,
                weight_score=0.5, width_ratio=1.0, char_gap=0, line_gap=0,
                stroke_width=0, category_scores={"serif": 1.0}, confidence=1.0,
                warnings=[],
            ),
            font_match=matcher.FontMatch(
                family="Noto Serif SC", path=font_path, weight=400, score=1.0,
                confidence=1.0, fallback_used=False, top_candidates=[], warnings=[],
            ),
        )
        block.reviewed_style_lock = {"style_sha256": "b" * 64}
        layout = engine.layout_text_block(block)
        self.assertEqual(layout["font_size"], 32)
        self.assertTrue(layout["overflow"])

    def test_reviewed_style_lock_renders_exact_fill_color(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSerifSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("测", 1.0, (10, 10, 70, 60), [])],
            box=(10, 10, 90, 70), kind="dialogue", rewrite_text="测",
            original_text="测", confidence=1.0, orientation="horizontal",
            fill=(18, 19, 20),
            style=styles.TextStyle(
                fill=(18, 19, 20), orientation="horizontal", font_size=32,
                weight_score=0.5, width_ratio=1.0, char_gap=0, line_gap=0,
                stroke_width=0, category_scores={"serif": 1.0}, confidence=1.0,
                warnings=[],
            ),
            font_match=matcher.FontMatch(
                family="Noto Serif SC", path=font_path, weight=400, score=1.0,
                confidence=1.0, fallback_used=False, top_candidates=[], warnings=[],
            ),
        )
        block.reviewed_style_lock = {
            "fill_rgba": [18, 19, 20, 255],
            "stroke_rgba": [0, 0, 0, 0],
            "style_sha256": "c" * 64,
        }
        image = Image.new("RGB", (120, 90), "white")
        engine.draw_text_block(image, block)
        self.assertIn((18, 19, 20), set(image.getdata()))

    def test_reviewed_page_manifest_overrides_text_font_and_style_exactly(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        catalog = importlib.import_module("comic_repair.font_catalog")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSerifSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("错错字", 0.8, (10, 20, 100, 55), [])],
            box=(10, 20, 120, 70), kind="dialogue", rewrite_text="错错字",
            original_text="错错字", confidence=0.8, orientation="horizontal",
            fill=(0, 0, 0),
            style=styles.TextStyle(
                fill=(0, 0, 0), orientation="horizontal", font_size=28,
                weight_score=0.5, width_ratio=1.0, char_gap=0, line_gap=4,
                stroke_width=0, category_scores={"serif": 1.0}, confidence=0.6,
                warnings=[],
            ),
            font_match=matcher.FontMatch(
                family="Noto Serif SC", path=font_path, weight=400, score=0.6,
                confidence=0.6, fallback_used=True, top_candidates=[], warnings=[],
            ),
        )
        block.block_id = engine.stable_text_block_id("001.jpg", block)
        lock = copy.deepcopy(STYLE_LOCK)
        lock["font"]["family"] = "Noto Serif SC"
        lock["font"]["asset_sha256"] = hashlib.sha256(font_path.read_bytes()).hexdigest()
        lock["font_size_px"] = 28.0
        lock["anchor"] = [10.0, 20.0]
        lock["line_boxes"] = [[10, 20, 100, 55]]
        lock["style_sha256"] = canonical_hash(lock)
        review_rows = [{
            "block_id": block.block_id,
            "action": "replace",
            "replacement_text": "正确字",
            "font_weight": 600,
            "style_lock": lock,
        }]
        candidates = catalog.available_fonts(
            catalog.load_catalog(ROOT / "assets" / "text_fonts" / "catalog.json")
        )
        engine.apply_reviewed_page(blocks=[block], source_relative="001.jpg",
                                   review_rows=review_rows, font_candidates=candidates)
        self.assertEqual(block.rewrite_text, "正确字")
        self.assertEqual(block.font_match.weight, 600)
        self.assertFalse(block.font_match.fallback_used)
        self.assertEqual(block.style.fill, (18, 18, 18))
        self.assertEqual(block.reviewed_style_lock, lock)

    def test_review_manifest_rejects_source_hash_drift(self):
        engine = importlib.import_module("text_engine_pipeline")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "page_0001.jpg"
            image.write_bytes(b"live-source")
            page = engine.InputPage(1, "001.jpg", "page_0001.jpg", image)
            manifest = root / "review.json"
            manifest.write_text(json.dumps({
                "status": "confirmed",
                "pages": [{
                    "source_relative": "001.jpg",
                    "source_sha256": "0" * 64,
                    "blocks": [],
                }],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source hash"):
                engine.load_confirmed_review_pages(manifest, [page])

    def test_layout_preflight_blocks_overflow_before_lama(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSerifSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("很多很多文字", 1.0, (0, 0, 20, 20), [])],
            box=(0, 0, 20, 20), kind="dialogue", rewrite_text="很多很多文字",
            original_text="原文", confidence=1.0, orientation="horizontal",
            fill=(0, 0, 0), block_id="text-overflow",
            style=styles.TextStyle(
                fill=(0, 0, 0), orientation="horizontal", font_size=32,
                weight_score=0.4, width_ratio=1.0, char_gap=0, line_gap=0,
                stroke_width=0, category_scores={}, confidence=1.0, warnings=[],
            ),
            font_match=matcher.FontMatch(
                family="Noto Serif SC", path=font_path, weight=400, score=1.0,
                confidence=1.0, fallback_used=False, top_candidates=[], warnings=[],
            ),
            reviewed_style_lock={"style_sha256": "d" * 64},
        )
        with tempfile.TemporaryDirectory() as temporary:
            report = engine.write_layout_preflight(
                Path(temporary), {"page_0001.jpg": [block]},
                {"page_0001.jpg": engine.InputPage(1, "001.jpg", "page_0001.jpg", Path("x"))},
            )
            self.assertEqual(report["status"], "evidence_blocked")
            self.assertEqual(report["overflow_block_count"], 1)
            self.assertEqual(report["pages"][0]["blocks"][0]["font_size"], 32)
            self.assertTrue((Path(temporary) / "layout_preflight.json").is_file())

    def test_reviewed_horizontal_scale_fits_condensed_source_without_shrinking(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSerifSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("叫做锻体五行诀。", 1.0, (0, 0, 200, 37), [])],
            box=(0, 0, 200, 37), kind="dialogue", rewrite_text="叫做锻体五行诀。",
            original_text="叫做锻体五行诀。", confidence=1.0,
            orientation="horizontal", fill=(0, 0, 0),
            style=styles.TextStyle(
                fill=(0, 0, 0), orientation="horizontal", font_size=35,
                weight_score=0.4, width_ratio=0.7, char_gap=0, line_gap=0,
                stroke_width=0, category_scores={}, confidence=1.0, warnings=[],
            ),
            font_match=matcher.FontMatch(
                family="Noto Serif SC", path=font_path, weight=400, score=1.0,
                confidence=1.0, fallback_used=False, top_candidates=[], warnings=[],
            ),
            reviewed_style_lock={"horizontal_scale": 0.7, "style_sha256": "e" * 64},
        )
        layout = engine.layout_text_block(block)
        self.assertEqual(layout["font_size"], 35)
        self.assertFalse(layout["overflow"])

    def test_reviewed_layout_uses_locked_line_position_and_alignment(self):
        engine = importlib.import_module("text_engine_pipeline")
        styles = importlib.import_module("comic_repair.style_analysis")
        matcher = importlib.import_module("comic_repair.font_matcher")
        font_path = ROOT / "assets" / "text_fonts" / "fonts" / "NotoSerifSC-VF.ttf"
        block = engine.TextBlock(
            lines=[engine.TextLine("测试", 1.0, (20, 10, 100, 45), [])],
            box=(0, 0, 200, 80), kind="dialogue", rewrite_text="测试",
            original_text="测试", confidence=1.0, orientation="horizontal",
            fill=(0, 0, 0),
            style=styles.TextStyle(
                fill=(0, 0, 0), orientation="horizontal", font_size=32,
                weight_score=0.4, width_ratio=0.8, char_gap=0, line_gap=0,
                stroke_width=0, category_scores={}, confidence=1.0, warnings=[],
            ),
            font_match=matcher.FontMatch(
                family="Noto Serif SC", path=font_path, weight=400, score=1.0,
                confidence=1.0, fallback_used=False, top_candidates=[], warnings=[],
            ),
            reviewed_style_lock={
                "horizontal_scale": 0.8,
                "alignment": "left",
                "line_boxes": [[20, 10, 100, 45]],
                "style_sha256": "f" * 64,
            },
        )
        block.reviewed_lines = ["测试"]
        layout = engine.layout_text_block(block)
        self.assertFalse(layout["overflow"])
        self.assertEqual(layout["lines"], ["测试"])
        self.assertEqual(layout["line_boxes"][0][0], 20)
        self.assertEqual(layout["line_boxes"][0][1], 10)

    def test_reviewed_flat_inpaint_removes_text_without_touching_outside_mask(self):
        engine = importlib.import_module("text_engine_pipeline")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original_path = root / "original.png"
            clean_path = root / "clean.png"
            original = Image.new("RGB", (120, 80), "white")
            for x in range(35, 85):
                for y in range(30, 44):
                    original.putpixel((x, y), (0, 0, 0))
            original.putpixel((5, 5), (20, 100, 180))
            original.save(original_path)
            Image.new("RGB", (120, 80), (240, 240, 240)).save(clean_path)
            line = engine.TextLine(
                text="测试", conf=1.0, box=(35, 30, 85, 44),
                polygon=[[35, 30], [85, 30], [85, 44], [35, 44]],
            )
            block = engine.TextBlock(
                lines=[line], box=(35, 30, 85, 44), kind="dialogue",
                rewrite_text="测试", original_text="测试", confidence=1.0,
                orientation="horizontal", fill=(0, 0, 0),
                background_cleanup="flat_inpaint",
            )

            count = engine.apply_reviewed_background_cleanup(
                original_path, clean_path, [block]
            )

            cleaned = Image.open(clean_path).convert("RGB")
            self.assertEqual(count, 1)
            self.assertGreater(min(cleaned.getpixel((60, 37))), 220)
            self.assertEqual(cleaned.getpixel((5, 5)), (240, 240, 240))

    def test_page_cleanup_backend_routes_image2_without_running_lama(self):
        engine = importlib.import_module("text_engine_pipeline")
        line = engine.TextLine("测试", 1.0, (1, 1, 10, 10), [])
        image2_block = engine.TextBlock(
            lines=[line], box=(1, 1, 10, 10), kind="dialogue",
            rewrite_text="测试", original_text="测试", confidence=1.0,
            orientation="horizontal", fill=(0, 0, 0),
            background_cleanup="gpt_image_2",
        )
        flat_block = engine.TextBlock(
            lines=[line], box=(20, 1, 30, 10), kind="dialogue",
            rewrite_text="测试", original_text="测试", confidence=1.0,
            orientation="horizontal", fill=(0, 0, 0),
            background_cleanup="flat_inpaint",
        )
        self.assertEqual(
            engine.page_cleanup_backend([image2_block, flat_block]),
            "gpt_image_2",
        )

    def test_page_cleanup_backend_rejects_mixed_lama_and_image2(self):
        engine = importlib.import_module("text_engine_pipeline")
        line = engine.TextLine("测试", 1.0, (1, 1, 10, 10), [])
        blocks = [
            engine.TextBlock(
                lines=[line], box=(1, 1, 10, 10), kind="dialogue",
                rewrite_text="测试", original_text="测试", confidence=1.0,
                orientation="horizontal", fill=(0, 0, 0),
                background_cleanup=cleanup,
            )
            for cleanup in ("lama", "gpt_image_2")
        ]
        with self.assertRaisesRegex(ValueError, "cannot mix"):
            engine.page_cleanup_backend(blocks)


if __name__ == "__main__":
    unittest.main()
