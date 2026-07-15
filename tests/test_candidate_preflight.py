import copy
import hashlib
import importlib
import os
import shutil
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
from test_prompt_compiler import (  # noqa: E402
    base_v4_spec,
    base_v4_text_spec,
    refresh_text_inventory,
)


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


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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

    def make_task7_text_candidate(self, *, outside_change=False, alpha_outside=False, rgba=False):
        original = self.root / "task7-original.png"
        candidate = self.root / "task7-candidate.png"
        mask_path = self.root / "task7-mask.png"
        base = Image.new("RGB", (256, 256), (235, 230, 215))
        base_draw = ImageDraw.Draw(base)
        base_draw.rectangle((4, 4, 251, 251), outline=(30, 30, 30), width=3)
        base_draw.ellipse((70, 40, 190, 150), fill=(160, 120, 90))
        if alpha_outside or rgba:
            base = base.convert("RGBA")
        base.save(original)
        edited = base.copy()
        ImageDraw.Draw(edited).rectangle((30, 170, 100, 195), fill=(90, 90, 90))
        if outside_change:
            ImageDraw.Draw(edited).point((240, 20), fill=(255, 0, 0))
        if alpha_outside:
            red, green, blue, _ = edited.getpixel((240, 20))
            edited.putpixel((240, 20), (red, green, blue, 0))
        edited.save(candidate)
        mask_image = Image.new("L", base.size, 0)
        ImageDraw.Draw(mask_image).rectangle((20, 160, 219, 229), fill=255)
        mask_image.save(mask_path)
        change_mask = {
            "path": str(mask_path),
            "sha256": sha256(mask_path),
            "mode": "L",
            "width": 256,
            "height": 256,
        }
        spec = base_v4_text_spec()
        page_path = "章节/0003.png"
        source_hash = sha256(original)
        spec["page_id"] = page_path
        spec["canvas_size"] = {"width": 256, "height": 256}
        spec["source_page"] = {
            "path": page_path,
            "sha256": source_hash,
            "width": 256,
            "height": 256,
        }
        spec["page_visual_metadata"].update(
            page_path=page_path,
            source_page_sha256=source_hash,
        )
        spec["cluster"].update(
            member_pages=[page_path],
            visual_targets=[page_path],
            canary_page=page_path,
        )
        for reference in spec["references"]:
            if reference["role"] == "target_composition":
                reference.update(
                    path=page_path,
                    subject=page_path,
                    sha256=source_hash,
                )
        region = spec["source_text_inventory"]["regions"][0]
        region["bbox"] = [20, 160, 220, 230]
        spec["source_text_inventory"].update(source_page_sha256=source_hash)
        spec["source_text_inventory"]["coverage_review"].update(
            source_page_sha256=source_hash,
            inspected_bbox=[0, 0, 256, 256],
        )
        spec["blocks"][0].update(bbox=[20, 160, 220, 230])
        spec["page_density_budget"].update(
            max_page_chars_per_10000_px2=100,
            max_block_chars_per_10000_px2=100,
        )
        refresh_text_inventory(spec)
        request = importlib.import_module("prompt_compiler").compile_text_repair_request(spec)
        return original, candidate, change_mask, spec, request

    def review_artifact(self, path, report, kind, *, created_at="2026-07-14T19:30:00+00:00"):
        return {
            "path": str(path),
            "sha256": sha256(path),
            "kind": kind,
            "candidate_sha256": report["hashes"]["candidate"],
            "source_sha256": report["hashes"]["original"],
            "preflight_id": report["preflight_id"],
            "created_at": created_at,
        }

    def write_comparison(self, original, candidate, path, *, swapped=False, stale=None, blank=False):
        with Image.open(original) as source, Image.open(candidate) as current:
            left = current if swapped else source
            right = source if swapped else current
            if stale is not None:
                right = Image.open(stale)
            try:
                board = Image.new("RGBA", (source.width * 2, source.height), (255, 255, 255, 255))
                if not blank:
                    board.paste(left.convert("RGBA"), (0, 0))
                    board.paste(right.convert("RGBA"), (source.width, 0))
                board.save(path)
            finally:
                if stale is not None:
                    right.close()
        return path

    def make_full_redraw_report(self):
        module = candidate_preflight()
        original, candidate, _, _, _ = self.make_task7_text_candidate()
        topology = self.root / "panel-topology.png"
        Image.new("RGB", (256, 256), "white").save(topology)
        spec = base_v4_spec()
        page_path = "章节/0003.png"
        source_hash = sha256(original)
        spec["page_id"] = page_path
        spec["source_page"] = {"path": page_path, "sha256": source_hash, "width": 256, "height": 256}
        spec["target_metadata"] = copy.deepcopy(spec["source_page"])
        spec["target_dimensions"] = {"width": 256, "height": 256}
        spec["page_visual_metadata"].update(page_path=page_path, source_page_sha256=source_hash)
        spec["cluster"].update(member_pages=[page_path], visual_targets=[page_path], canary_page=page_path)
        for reference in spec["references"]:
            if reference["role"] == "target_composition":
                reference.update(path=page_path, subject=page_path, sha256=source_hash)
        redraw_request = importlib.import_module("prompt_compiler").compile_redraw_request(spec)
        request_path = self.root / "textless-request.json"
        import json

        request_path.write_text(json.dumps(redraw_request, ensure_ascii=False), encoding="utf-8")
        redraw_evidence = {
            "source_page_sha256": sha256(original),
            "candidate_sha256": sha256(candidate),
            "candidate_stage": "textless",
            "source_has_ordinary_text": False,
            "panel_topology": {
                "path": str(topology),
                "sha256": sha256(topology),
                "kind": "panel_topology",
                "source_page_sha256": sha256(original),
                "candidate_sha256": sha256(candidate),
            },
            "target_composition": {
                "path": str(original),
                "sha256": sha256(original),
                "kind": "target_composition",
                "source_page_sha256": sha256(original),
            },
            "textless_request": {
                "path": str(request_path),
                "sha256": sha256(request_path),
                "kind": "textless_redraw_request",
                "source_page_sha256": sha256(original),
            },
        }
        report = module.run_candidate_preflight(
            candidate,
            original,
            page_class="full_page_redraw",
            candidate_stage="textless",
            text_policy="textless",
            ocr_metadata=VALID_OCR,
            redraw_evidence=redraw_evidence,
            redraw_spec=spec,
            redraw_request=redraw_request,
        )
        return original, candidate, report

    def task7_machine_evidence(self, candidate, request):
        declaration = request["declaration"]
        ocr_blocks = []
        rendered_blocks = []
        with Image.open(candidate) as image:
            with image.convert("RGBA") as rgba:
                for block in declaration["blocks"]:
                    bbox = block["bbox"]
                    with rgba.crop(tuple(bbox)) as crop:
                        crop_hash = hashlib.sha256(crop.tobytes()).hexdigest()
                    replacement = block["replacement_text"]
                    ocr_blocks.append(
                        {
                            "block_id": block["block_id"],
                            "recognized_text": replacement,
                            "status": "passed",
                        }
                    )
                    rendered_blocks.append(
                        {
                            "block_id": block["block_id"],
                            "rendered_text": replacement,
                            "rendered_text_sha256": hashlib.sha256(
                                replacement.encode("utf-8")
                            ).hexdigest(),
                            "bbox": bbox,
                            "crop_sha256": crop_hash,
                        }
                    )
        return ocr_blocks, {
            "candidate_sha256": sha256(candidate),
            "declaration_hash": request["declaration_hash"],
            "blocks": rendered_blocks,
        }

    def task7_machine_kwargs(self, candidate, request):
        ocr_blocks, render_manifest = self.task7_machine_evidence(candidate, request)
        return {"ocr_blocks": ocr_blocks, "render_manifest": render_manifest}

    def test_text_machine_gate_rejects_ocr_and_render_crop_mismatch(self):
        module = candidate_preflight()
        original, candidate, mask, spec, request = self.make_task7_text_candidate(rgba=True)
        ocr_blocks, render_manifest = self.task7_machine_evidence(candidate, request)
        bad_ocr = copy.deepcopy(ocr_blocks)
        bad_ocr[0]["recognized_text"] = "错字"
        with self.assertRaisesRegex(ValueError, "OCR|ocr"):
            module.run_candidate_preflight(
                candidate,
                original,
                page_class="text_only",
                text_policy="deterministic_text",
                change_mask=mask,
                text_spec=spec,
                text_request=request,
                ocr_blocks=bad_ocr,
                render_manifest=render_manifest,
            )
        bad_render = copy.deepcopy(render_manifest)
        bad_render["blocks"][0]["crop_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "crop|render"):
            module.run_candidate_preflight(
                candidate,
                original,
                page_class="text_only",
                text_policy="deterministic_text",
                change_mask=mask,
                text_spec=spec,
                text_request=request,
                ocr_blocks=ocr_blocks,
                render_manifest=bad_render,
            )

    def test_samefile_hardlink_and_unmodified_text_candidate_are_rejected(self):
        module = candidate_preflight()
        original, candidate, mask, spec, request = self.make_task7_text_candidate()
        ocr_blocks, _ = self.task7_machine_evidence(original, request)
        _, unmodified_manifest = self.task7_machine_evidence(original, request)
        for name, path in (("same", original), ("hardlink", self.root / "hardlink.png")):
            if name == "hardlink":
                os.link(original, path)
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "same file|hardlink|changed pixels"):
                    module.run_candidate_preflight(
                        path,
                        original,
                        page_class="text_only",
                        text_policy="deterministic_text",
                        change_mask=mask,
                        text_spec=spec,
                        text_request=request,
                        ocr_blocks=ocr_blocks,
                        render_manifest=unmodified_manifest,
                    )

    def test_animated_image_and_one_character_stable_id_are_rejected(self):
        module = candidate_preflight()
        animated = self.root / "animated.webp"
        frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
        frames[0].save(animated, save_all=True, append_images=frames[1:], duration=50, loop=0)
        duplicate = self.root / "animated-copy.webp"
        shutil.copyfile(animated, duplicate)
        with self.assertRaisesRegex(ValueError, "animated|frame"):
            module.run_candidate_preflight(duplicate, animated, page_class="unchanged")
        report = module.run_candidate_preflight(self.candidate, self.original, page_class="unchanged")
        with self.assertRaisesRegex(ValueError, "3-64|stable id"):
            module.record_independent_review(
                report,
                "a",
                "reviewer-2",
                "rejected",
                "2026-07-14T20:00:00+00:00",
                candidate_created_at="2026-07-14T19:00:00+00:00",
                review_artifacts=[],
                blind=True,
                inspected_panels=[],
                inspected_entities=[],
                check_matrix={name: False for name in module.REVIEW_MATRIX_CHECKS},
            )

    def test_batch_rejects_report_bound_to_wrong_source_inventory(self):
        module = candidate_preflight()
        report = module.run_candidate_preflight(self.candidate, self.original, page_class="unchanged")
        input_root = self.root / "input"
        input_root.mkdir()
        expected = input_root / "page.jpg"
        draw_pattern(expected)
        with self.assertRaisesRegex(ValueError, "source|inventory"):
            module.validate_candidate_batch(
                ["page.jpg"],
                [{"source_page": "page.jpg", "output_name": "page.jpg"}],
                [report],
                candidate_root=self.root,
                actual_outputs=["page.jpg"],
                input_root=input_root,
                inventory_rows=[
                    {
                        "input_name": "page.jpg",
                        "sha256": sha256(expected),
                        "width": 896,
                        "height": 1200,
                    }
                ],
            )

    def test_complete_task7_request_is_required_instead_of_self_signed_subset(self):
        module = candidate_preflight()
        original, candidate, mask, spec, request = self.make_task7_text_candidate()

        report = module.run_candidate_preflight(
            candidate,
            original,
            page_class="text_only",
            text_policy="deterministic_text",
            change_mask=mask,
            text_spec=spec,
            text_request=request,
            **self.task7_machine_kwargs(candidate, request),
        )
        self.assertEqual(report["status"], "pass")
        self.assertEqual(
            report["text_request_binding"]["declaration_hash"],
            request["declaration_hash"],
        )

        comparison = self.root / "task7-comparison.png"
        with Image.open(original) as left, Image.open(candidate) as right:
            board = Image.new("RGB", (512, 256), "white")
            board.paste(left.convert("RGB"), (0, 0))
            board.paste(right.convert("RGB"), (256, 0))
            board.save(comparison)
        glyph_board = self.root / "task7-glyph.png"
        Image.new("RGB", (256, 256), "white").save(glyph_board)
        glyph_artifact = self.review_artifact(
            glyph_board, report, "full_resolution_glyph"
        )
        review = module.record_independent_review(
            report,
            "text-worker-1",
            "reviewer-2",
            "accepted",
            "2026-07-14T20:00:00+00:00",
            candidate_created_at="2026-07-14T19:00:00+00:00",
            review_artifacts=[
                self.review_artifact(original, report, "full_resolution_original"),
                self.review_artifact(candidate, report, "full_resolution_candidate"),
                self.review_artifact(comparison, report, "full_resolution_comparison"),
            ],
            blind=True,
            inspected_panels=["panel-1"],
            inspected_entities=["hero"],
            check_matrix={name: True for name in module.REVIEW_MATRIX_CHECKS},
            glyph_review={
                "artifact": glyph_artifact,
                "reviewer_id": "glyph-reviewer-3",
                "result": "passed",
                "rendered_blocks": [{"block_id": "dialogue-1", "inspected": True}],
                "regression_vocabulary": [
                    {"character": "\u5f3a", "shape_inspected": True},
                    {"character": "\u9047", "shape_inspected": True},
                ],
            },
        )
        self.assertTrue(module.candidate_is_finally_eligible(report, review))

        forged = copy.deepcopy(request)
        forged["declaration"]["current_target"]["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "Task 7|current_target|request"):
            module.run_candidate_preflight(
                candidate,
                original,
                page_class="text_only",
                text_policy="deterministic_text",
                change_mask=mask,
                text_spec=spec,
                text_request=forged,
                **self.task7_machine_kwargs(candidate, request),
            )

    def test_review_artifact_kind_cannot_disguise_a_text_file(self):
        module = candidate_preflight()
        report = module.run_candidate_preflight(
            self.candidate, self.original, page_class="unchanged"
        )
        fake = self.root / "board.txt"
        fake.write_text("not an image", encoding="utf-8")
        artifact = self.review_artifact(fake, report, "full_resolution")
        matrix = {name: "passed" for name in module.REVIEW_MATRIX_CHECKS}

        with self.assertRaisesRegex(ValueError, "image|artifact"):
            module.record_independent_review(
                report,
                "worker-1",
                "reviewer-2",
                "accepted",
                "2026-07-14T20:00:00+00:00",
                candidate_created_at="2026-07-14T19:00:00+00:00",
                review_artifacts=[artifact],
                blind=True,
                inspected_panels=["panel-1"],
                inspected_entities=[],
                check_matrix=matrix,
            )

    def test_rgba_alpha_change_outside_mask_is_not_lost_in_grayscale(self):
        module = candidate_preflight()
        original, candidate, mask, spec, request = self.make_task7_text_candidate(
            alpha_outside=True
        )

        report = module.run_candidate_preflight(
            candidate,
            original,
            page_class="text_only",
            text_policy="deterministic_text",
            change_mask=mask,
            text_spec=spec,
            text_request=request,
            **self.task7_machine_kwargs(candidate, request),
        )
        self.assertEqual(report["checks"]["outside_mask_preserved"]["status"], "fail")

    def test_one_pixel_antialias_boundary_is_limited_and_two_pixels_outside_fails(self):
        module = candidate_preflight()
        original, candidate, mask, spec, request = self.make_task7_text_candidate(rgba=True)
        with Image.open(candidate) as source:
            boundary = source.convert("RGBA")
        red, green, blue, _ = boundary.getpixel((220, 180))
        boundary.putpixel((220, 180), (red, green, blue, 251))
        boundary_path = self.root / "boundary.png"
        boundary.save(boundary_path)
        allowed = module.run_candidate_preflight(
            boundary_path,
            original,
            page_class="text_only",
            text_policy="deterministic_text",
            change_mask=mask,
            text_spec=spec,
            text_request=request,
            **self.task7_machine_kwargs(boundary_path, request),
        )
        self.assertEqual(allowed["checks"]["outside_mask_preserved"]["status"], "pass")

        red, green, blue, _ = boundary.getpixel((221, 180))
        boundary.putpixel((221, 180), (red, green, blue, 251))
        outside_path = self.root / "two-pixels-outside.png"
        boundary.save(outside_path)
        rejected = module.run_candidate_preflight(
            outside_path,
            original,
            page_class="text_only",
            text_policy="deterministic_text",
            change_mask=mask,
            text_spec=spec,
            text_request=request,
            **self.task7_machine_kwargs(outside_path, request),
        )
        self.assertEqual(rejected["checks"]["outside_mask_preserved"]["status"], "fail")

    def test_blind_review_matrix_rejects_old_incomplete_check_set(self):
        module = candidate_preflight()
        report = module.run_candidate_preflight(
            self.candidate, self.original, page_class="unchanged"
        )
        comparison = self.root / "comparison.png"
        self.write_comparison(self.original, self.candidate, comparison)
        artifacts = [
            self.review_artifact(self.original, report, "full_resolution_original"),
            self.review_artifact(self.candidate, report, "full_resolution_candidate"),
            self.review_artifact(comparison, report, "full_resolution_comparison"),
        ]
        old_matrix = {name: "passed" for name in module.REVIEW_MATRIX_CHECKS}

        with self.assertRaisesRegex(ValueError, "check_matrix"):
            module.record_independent_review(
                report,
                "worker-1",
                "reviewer-2",
                "accepted",
                "2026-07-14T20:00:00+00:00",
                candidate_created_at="2026-07-14T19:00:00+00:00",
                review_artifacts=artifacts,
                blind=True,
                inspected_panels=["panel-1"],
                inspected_entities=[],
                check_matrix=old_matrix,
            )

    def test_full_page_redraw_accepts_only_real_full_size_three_board_review(self):
        module = candidate_preflight()
        original, candidate, report = self.make_full_redraw_report()
        comparison = self.root / "redraw-comparison.png"
        self.write_comparison(original, candidate, comparison)
        review = module.record_independent_review(
            report,
            "redraw-worker-1",
            "reviewer-2",
            "accepted",
            "2026-07-14T20:00:00+00:00",
            candidate_created_at="2026-07-14T19:00:00+00:00",
            review_artifacts=[
                self.review_artifact(original, report, "full_resolution_original"),
                self.review_artifact(candidate, report, "full_resolution_candidate"),
                self.review_artifact(comparison, report, "full_resolution_comparison"),
            ],
            blind=True,
            inspected_panels=["panel-1"],
            inspected_entities=["hero"],
            check_matrix={name: True for name in module.REVIEW_MATRIX_CHECKS},
        )
        self.assertTrue(module.candidate_is_finally_eligible(report, review))

    def test_comparison_board_rejects_blank_stale_and_swapped_halves(self):
        module = candidate_preflight()
        original, candidate, report = self.make_full_redraw_report()
        stale = self.root / "stale.png"
        Image.new("RGB", (256, 256), (1, 2, 3)).save(stale)
        variants = {
            "blank": self.write_comparison(
                original, candidate, self.root / "blank-board.png", blank=True
            ),
            "stale": self.write_comparison(
                original,
                candidate,
                self.root / "stale-board.png",
                stale=stale,
            ),
            "swapped": self.write_comparison(
                original,
                candidate,
                self.root / "swapped-board.png",
                swapped=True,
            ),
        }
        for name, comparison in variants.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "comparison"):
                    module.record_independent_review(
                        report,
                        "redraw-worker-1",
                        "reviewer-2",
                        "accepted",
                        "2026-07-14T20:00:00+00:00",
                        candidate_created_at="2026-07-14T19:00:00+00:00",
                        review_artifacts=[
                            self.review_artifact(original, report, "full_resolution_original"),
                            self.review_artifact(candidate, report, "full_resolution_candidate"),
                            self.review_artifact(comparison, report, "full_resolution_comparison"),
                        ],
                        blind=True,
                        inspected_panels=["panel-1"],
                        inspected_entities=["hero"],
                        check_matrix={name: True for name in module.REVIEW_MATRIX_CHECKS},
                    )

    def test_text_only_candidate_rejects_pixels_changed_outside_mask(self):
        module = candidate_preflight()
        original, candidate, change_mask, spec, request = self.make_task7_text_candidate(
            outside_change=True
        )

        report = module.run_candidate_preflight(
            candidate,
            original,
            page_class="text_only",
            text_policy="deterministic_text",
            change_mask=change_mask,
            text_spec=spec,
            text_request=request,
            **self.task7_machine_kwargs(candidate, request),
        )

        self.assertEqual(
            report["checks"]["outside_mask_preserved"]["status"], "fail"
        )

    def test_review_requires_full_resolution_artifacts_and_candidate_time_order(self):
        module = candidate_preflight()
        report = module.run_candidate_preflight(
            self.candidate,
            self.original,
            page_class="unchanged",
        )

        with self.assertRaisesRegex(ValueError, "review artifact"):
            module.record_independent_review(
                report,
                generator="worker-1",
                reviewer="reviewer-2",
                decision="accepted",
                reviewed_at="2026-07-14T20:00:00+00:00",
                candidate_created_at="2026-07-14T19:00:00+00:00",
                review_artifacts=[],
                blind=True,
            )

    def test_text_candidate_requires_visual_glyph_review(self):
        module = candidate_preflight()
        original, candidate, change_mask, spec, request = self.make_task7_text_candidate()
        report = module.run_candidate_preflight(
            candidate,
            original,
            page_class="text_only",
            text_policy="deterministic_text",
            change_mask=change_mask,
            text_spec=spec,
            text_request=request,
            **self.task7_machine_kwargs(candidate, request),
        )
        board = self.root / "glyph-board.png"
        Image.new("RGB", (256, 256), "white").save(board)

        with self.assertRaisesRegex(ValueError, "glyph review"):
            module.record_independent_review(
                report,
                generator="text-worker-1",
                reviewer="glyph-reviewer-2",
                decision="accepted",
                reviewed_at="2026-07-14T20:00:00+00:00",
                candidate_created_at="2026-07-14T19:00:00+00:00",
                review_artifacts=[
                    {
                        "path": str(board),
                        "sha256": sha256(board),
                        "kind": "full_resolution",
                        "candidate_sha256": report["hashes"]["candidate"],
                        "preflight_id": report["preflight_id"],
                    }
                ],
                blind=True,
                glyph_review=None,
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

    def test_preflight_statistics_do_not_return_long_lived_pil_images(self):
        report = self.run_preflight()
        import json

        json.dumps(report)
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
