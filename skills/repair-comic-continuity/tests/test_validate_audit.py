import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from entity_timeline import build_timeline  # noqa: E402
from pipeline_contracts import canonical_hash  # noqa: E402
from validate_audit import validate_audit_project  # noqa: E402


def write_registry(path, value):
    value = dict(value)
    value.pop("registry_hash", None)
    value["registry_hash"] = canonical_hash(value)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class AuditOnlyValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("人物参考图", "输入", "输出", "evidence"):
            (self.root / name).mkdir()
        (self.root / "小说.txt").write_text("第一章\n测试正文", encoding="utf-8")
        Image.new("RGB", (32, 48), "white").save(self.root / "人物参考图" / "hero.png")
        Image.new("RGB", (32, 48), "gray").save(self.root / "输入" / "189.jpg")
        self.evidence = self.root / "evidence"
        (self.evidence / "novel_alignment.json").write_text(
            json.dumps({"status": "confirmed", "pages": [{"output_name": "189.jpg", "status": "confirmed"}]}),
            encoding="utf-8",
        )
        write_registry(
            self.evidence / "scene_clusters.json",
            {"schema_version": "1.0", "status": "passed", "clusters": [{"cluster_id": "c1", "member_pages": ["189.jpg"]}]},
        )
        write_registry(
            self.evidence / "style_reference_packs.json",
            {"schema_version": "1.0", "status": "passed", "stable_pages": ["189.jpg"], "reference_packs": [{"reference_pack_id": "p1", "references": [{"role": "identity_only", "subject": "hero"}]}]},
        )
        timeline = build_timeline(
            [{"entity_type": "character", "entity_id": "hero", "page": "189.jpg", "state": {"hair": "black"}}],
            [],
        )
        timeline["status"] = "passed"
        write_registry(self.evidence / "entity_state_timeline.json", timeline)
        matrix = {
            "version": 1,
            "status": "confirmed",
            "cluster_id": "c1",
            "cluster_pages": ["189.jpg"],
            "characters": [{
                "entity_id": "hero",
                "reference": self.artifact(self.root / "人物参考图" / "hero.png"),
                "baseline": {
                    "skin_tone": "gray fixture",
                    "hair": "black",
                    "facial_hair": "none",
                    "clothing": "fixture robe",
                    "face_shape": "fixture face",
                    "body_build": "fixture build",
                },
                "status": "passed",
                "observations": [{
                    "page": "189.jpg",
                    "present": True,
                    "full_resolution": True,
                    "skin_tone": "match",
                    "hair": "match",
                    "facial_hair": "match",
                    "clothing": "match",
                    "face_shape": "match",
                    "body_build": "match",
                    "lighting_explanation": "uniform fixture lighting",
                    "evidence": self.artifact(self.root / "输入" / "189.jpg"),
                }],
            }],
        }
        (self.evidence / "character_appearance_matrix.json").write_text(
            json.dumps(matrix, ensure_ascii=False), encoding="utf-8"
        )
        crop_dir = self.evidence / "source_text_crops"
        crop_dir.mkdir()
        crop = crop_dir / "189-b1.png"
        with Image.open(self.root / "输入" / "189.jpg") as page_image:
            page_image.crop((0, 0, 32, 48)).save(crop)
        source_audit_dir = self.evidence / "source_text_audit"
        source_audit_dir.mkdir()
        block_ref = {
            "block_id": "189-b1",
            "bbox": [0, 0, 32, 48],
            "transcription": "测试正文",
        }
        source_audit = {
            "version": 1,
            "status": "confirmed",
            "page": self.artifact(self.root / "输入" / "189.jpg"),
            "novel": self.artifact(self.root / "小说.txt"),
            "machine_detector_id": "fixture-machine",
            "visual_reviewer_id": "fixture-independent-reviewer",
            "source_has_ordinary_text": True,
            "textless_review": None,
            "machine_blocks": [block_ref],
            "visual_blocks": [block_ref],
            "blocks": [{
                **block_ref,
                "crop": self.artifact(crop),
                "decision": "passed",
                "reason": "full-resolution fixture inspected",
                "novel_alignment": {
                    "status": "confirmed",
                    "start": 5,
                    "end": 9,
                    "excerpt": "测试正文",
                    "semantic_decision": "faithful",
                    "reason": "exact fixture excerpt",
                },
                "repeat_reviews": [],
                "glyph_checks": [],
            }],
        }
        (source_audit_dir / "189.jpg.json").write_text(
            json.dumps(source_audit, ensure_ascii=False), encoding="utf-8"
        )
        write_registry(
            self.evidence / "page_audit.json",
            {
                "schema_version": "1.0",
                "status": "audit_passed",
                "pages": [{
                    "page": "189.jpg",
                    "decision": "unchanged",
                    "audits": [{
                        "perspective": "continuity",
                        "artifact": {"kind": "full_resolution_page", "path": "输入/189.jpg"},
                    }],
                }],
            },
        )
        write_registry(
            self.evidence / "task_queue.json",
            {"schema_version": "1.0", "status": "audit_only", "tasks": []},
        )

    def tearDown(self):
        self.temp.cleanup()

    def tree_bytes(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def artifact(self, path):
        return {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def write_human_selection(self, selected_pages):
        path = self.evidence / "human_visual_selection.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "confirmed",
                    "mode": "human_visual_auto_text",
                    "selected_pages": [
                        {
                            "path": page,
                            "sha256": self.artifact(
                                self.root / "输入" / page
                            )["sha256"],
                        }
                        for page in selected_pages
                    ],
                    "visual_policy": "selected_pages_only",
                    "text_policy": "all_input_pages_page_reset_preserve_style",
                    "output_policy": "exact_input_bijection",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def write_unselected_scope_audit(self, decision="text_only"):
        selection = self.evidence / "human_visual_selection.json"
        write_registry(
            self.evidence / "page_audit.json",
            {
                "schema_version": "1.0",
                "status": "audit_passed",
                "pages": [{
                    "page": "189.jpg",
                    "decision": decision,
                    "audits": [{
                        "perspective": "human_visual_scope",
                        "artifact": {
                            "kind": "user_selected_page_list",
                            **self.artifact(selection),
                        },
                    }],
                }],
            },
        )

    def write_human_annotations(self):
        path = self.evidence / "human_issue_annotations.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "confirmed",
                    "mode": "human_visual_auto_text",
                    "confirmed_by": "user",
                    "confirmed_at": "2026-07-16T10:00:00+08:00",
                    "annotations": [{
                        "annotation_id": "189-beard",
                        "page": {
                            "path": "189.jpg",
                            "sha256": self.artifact(
                                self.root / "输入" / "189.jpg"
                            )["sha256"],
                        },
                        "regions": [{
                            "region_id": "lower-face",
                            "bbox_norm": [0.25, 0.10, 0.60, 0.48],
                            "description": "lower face and beard",
                        }],
                        "targets": ["character:hero"],
                        "defect_codes": ["identity_drift"],
                        "observed_state": "beard is missing",
                        "required_state": "restore reference beard",
                        "instruction": "change only the annotated detail",
                    }],
                    "learning_policy": "evidence_gated",
                    "persistence_policy": "page_cluster_project_skill_candidate",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def append_human_annotation_audit(self, artifact_override=None):
        annotations = self.evidence / "human_issue_annotations.json"
        path = self.evidence / "page_audit.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        artifact = {
            "kind": "user_confirmed_issue_annotations",
            **self.artifact(annotations),
        }
        if artifact_override:
            artifact.update(artifact_override)
        document["pages"][0]["audits"].append({
            "perspective": "human_issue_annotation",
            "artifact": artifact,
        })
        write_registry(path, document)

    def test_audit_only_accepts_complete_audits_without_candidates(self):
        before = self.tree_bytes()
        result = validate_audit_project(self.root, self.evidence)
        self.assertEqual(result["status"], "audit_passed")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["promoted_output_count"], 0)
        self.assertEqual(before, self.tree_bytes())

    def test_audit_only_rejects_any_candidate_or_final_image(self):
        candidate = self.root / "work" / "candidates" / "189.jpg"
        candidate.parent.mkdir(parents=True)
        Image.new("RGB", (32, 48), "red").save(candidate)
        with self.assertRaisesRegex(ValueError, "audit-only run contains candidate"):
            validate_audit_project(self.root, self.evidence)
        candidate.unlink()
        Image.new("RGB", (32, 48), "red").save(self.root / "输出" / "189.jpg")
        with self.assertRaisesRegex(ValueError, "audit-only run contains final output"):
            validate_audit_project(self.root, self.evidence)

    def test_audit_only_requires_second_review_when_routed(self):
        path = self.evidence / "page_audit.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["decision"] = "second_review_required"
        write_registry(path, document)
        with self.assertRaisesRegex(ValueError, "second review"):
            validate_audit_project(self.root, self.evidence)

    def test_audit_only_requires_appearance_and_source_text_gates(self):
        matrix = self.evidence / "character_appearance_matrix.json"
        matrix_bytes = matrix.read_bytes()
        matrix.unlink()
        with self.assertRaisesRegex(ValueError, "appearance matrix"):
            validate_audit_project(self.root, self.evidence)
        matrix.write_bytes(matrix_bytes)

        source_audit = self.evidence / "source_text_audit" / "189.jpg.json"
        source_audit.unlink()
        with self.assertRaisesRegex(ValueError, "source text audit"):
            validate_audit_project(self.root, self.evidence)

    def test_human_mode_selected_page_keeps_full_resolution_visual_review(self):
        self.write_human_selection(["189.jpg"])
        result = validate_audit_project(self.root, self.evidence)
        self.assertEqual(result["audit_mode"], "human_visual_auto_text")
        self.assertEqual(result["selected_visual_page_count"], 1)
        self.assertEqual(result["appearance_matrix_status"], "confirmed")
        self.assertEqual(result["source_text_audit_count"], 1)

    def test_human_mode_empty_selection_skips_visual_matrix_but_keeps_text_audit(self):
        self.write_human_selection([])
        (self.evidence / "character_appearance_matrix.json").unlink()
        self.write_unselected_scope_audit()

        result = validate_audit_project(self.root, self.evidence)
        self.assertEqual(result["audit_mode"], "human_visual_auto_text")
        self.assertEqual(result["selected_visual_page_count"], 0)
        self.assertEqual(result["appearance_matrix_status"], "not_applicable")
        self.assertEqual(result["source_text_audit_count"], 1)

    def test_human_mode_unselected_page_cannot_enter_full_page_redraw(self):
        self.write_human_selection([])
        (self.evidence / "character_appearance_matrix.json").unlink()
        self.write_unselected_scope_audit(decision="full_page_redraw")

        with self.assertRaisesRegex(ValueError, "unselected page cannot enter full_page_redraw"):
            validate_audit_project(self.root, self.evidence)

    def test_human_mode_still_requires_source_text_audit_on_unselected_page(self):
        self.write_human_selection([])
        (self.evidence / "character_appearance_matrix.json").unlink()
        self.write_unselected_scope_audit()
        (self.evidence / "source_text_audit" / "189.jpg.json").unlink()

        with self.assertRaisesRegex(ValueError, "source text audit"):
            validate_audit_project(self.root, self.evidence)

    def test_human_annotations_require_hash_bound_page_audit_and_report_counts(self):
        self.write_human_selection(["189.jpg"])
        self.write_human_annotations()
        with self.assertRaisesRegex(ValueError, "human issue annotation audit"):
            validate_audit_project(self.root, self.evidence)

        self.append_human_annotation_audit()
        result = validate_audit_project(self.root, self.evidence)
        self.assertEqual(result["human_issue_annotation_count"], 1)
        self.assertEqual(result["human_issue_annotated_page_count"], 1)

    def test_human_annotation_audit_rejects_stale_manifest_binding(self):
        self.write_human_selection(["189.jpg"])
        self.write_human_annotations()
        self.append_human_annotation_audit({"sha256": "0" * 64})
        with self.assertRaisesRegex(ValueError, "annotation artifact mismatch"):
            validate_audit_project(self.root, self.evidence)

    def test_human_annotations_require_human_visual_selection_mode(self):
        self.write_human_annotations()
        with self.assertRaisesRegex(ValueError, "requires human visual selection"):
            validate_audit_project(self.root, self.evidence)


if __name__ == "__main__":
    unittest.main()
