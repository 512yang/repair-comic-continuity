import copy
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import candidate_preflight  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_release_gate():
    path = SCRIPTS / "release_gate.py"
    if not path.is_file():
        raise AssertionError("scripts/release_gate.py has not been implemented")
    spec = importlib.util.spec_from_file_location("release_gate_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.references = self.root / "人物参考图"
        self.inputs = self.root / "输入"
        self.outputs = self.root / "输出"
        self.evidence = self.root / "evidence"
        for directory in (self.references, self.inputs, self.outputs, self.evidence):
            directory.mkdir()
        self.novel = self.root / "小说.txt"
        self.novel.write_text("第一章\n测试剧情。", encoding="utf-8")
        self.reference = self.references / "hero.png"
        self._draw_pattern(self.reference, (80, 100, 140))
        self.fixture = self._make_release_fixture()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _draw_pattern(path: Path, color: tuple[int, int, int]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (256, 256), (236, 231, 218))
        draw = ImageDraw.Draw(image)
        draw.rectangle((4, 4, 251, 251), outline=(25, 25, 25), width=4)
        draw.ellipse((54, 34, 202, 176), fill=color, outline=(20, 20, 20), width=3)
        draw.line((15, 220, 240, 190), fill=(20, 20, 20), width=5)
        image.save(path)
        return path

    def _artifact(self, path: Path) -> dict[str, str]:
        return {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": sha256(path),
        }

    def _review_artifact(
        self, path: Path, report: dict, kind: str
    ) -> dict[str, str]:
        return {
            "path": str(path.resolve()),
            "sha256": sha256(path),
            "kind": kind,
            "candidate_sha256": report["hashes"]["candidate"],
            "source_sha256": report["hashes"]["original"],
            "preflight_id": report["preflight_id"],
            "created_at": "2026-07-15T09:30:00+00:00",
        }

    def _make_release_fixture(self) -> dict:
        relative = "chapter/189.PNG"
        original = self._draw_pattern(self.inputs / relative, (150, 105, 75))
        candidate = self.root / "work" / "candidates" / relative
        output = self.outputs / relative
        candidate.parent.mkdir(parents=True)
        output.parent.mkdir(parents=True)
        shutil.copyfile(original, candidate)
        shutil.copyfile(original, output)

        report = candidate_preflight.run_candidate_preflight(
            candidate, original, page_class="unchanged"
        )
        preflight_path = self.evidence / "preflight.json"
        write_json(preflight_path, report)

        comparison = self.evidence / "comparison.png"
        with Image.open(original) as left, Image.open(candidate) as right:
            board = Image.new("RGBA", (512, 256), (255, 255, 255, 255))
            board.paste(left.convert("RGBA"), (0, 0))
            board.paste(right.convert("RGBA"), (256, 0))
            board.save(comparison)
        review = candidate_preflight.record_independent_review(
            report,
            generator="worker-1",
            reviewer="reviewer-2",
            decision="accepted",
            reviewed_at="2026-07-15T10:00:00+00:00",
            candidate_created_at="2026-07-15T09:00:00+00:00",
            review_artifacts=[
                self._review_artifact(
                    original, report, "full_resolution_original"
                ),
                self._review_artifact(
                    candidate, report, "full_resolution_candidate"
                ),
                self._review_artifact(
                    comparison, report, "full_resolution_comparison"
                ),
            ],
            blind=True,
            inspected_panels=["panel-1"],
            inspected_entities=["hero"],
            check_matrix={
                name: True for name in candidate_preflight.REVIEW_MATRIX_CHECKS
            },
        )
        review_path = self.evidence / "review.json"
        write_json(review_path, review)

        observation = self.evidence / "appearance-189.png"
        shutil.copyfile(original, observation)
        matrix = {
            "version": 1,
            "status": "confirmed",
            "cluster_id": "cluster-1",
            "cluster_pages": [relative],
            "characters": [
                {
                    "entity_id": "hero",
                    "reference": self._artifact(self.reference),
                    "baseline": {
                        "face_shape": "wide",
                        "body_build": "strong",
                        "skin_tone": "warm",
                        "hair": "black",
                        "facial_hair": "none",
                        "clothing": "red robe",
                    },
                    "status": "passed",
                    "observations": [
                        {
                            "page": relative,
                            "present": True,
                            "full_resolution": True,
                            "face_shape": "match",
                            "body_build": "match",
                            "skin_tone": "match",
                            "hair": "match",
                            "facial_hair": "match",
                            "clothing": "match",
                            "lighting_explanation": "same neutral daylight",
                            "evidence": self._artifact(observation),
                        }
                    ],
                }
            ],
        }
        matrix_path = self.evidence / "appearance-matrix.json"
        write_json(matrix_path, matrix)

        source_text_audit = {
            "version": 1,
            "status": "confirmed",
            "page": self._artifact(original),
            "novel": self._artifact(self.novel),
            "machine_detector_id": "rapidocr-v1",
            "visual_reviewer_id": "reviewer-2",
            "source_has_ordinary_text": False,
            "textless_review": {
                "reviewer_id": "reviewer-3",
                "full_resolution": True,
                "reason": "fixture page contains no ordinary text",
            },
            "machine_blocks": [],
            "visual_blocks": [],
            "blocks": [],
        }
        source_text_audit_path = self.evidence / "source-text-audit.json"
        write_json(source_text_audit_path, source_text_audit)

        source_hash = sha256(original)
        manifest = {
            "schema_version": "1.1",
            "sealed_inputs": [
                {"relative_path": relative, "sha256": source_hash}
            ],
            "pages": [
                {
                    "relative_path": relative,
                    "page_class": "unchanged",
                    "input_sha256": source_hash,
                    "appearance_matrix": self._artifact(matrix_path),
                    "source_text_audit": self._artifact(source_text_audit_path),
                    "preflight": self._artifact(preflight_path),
                    "review": self._artifact(review_path),
                    "candidate": self._artifact(candidate),
                    "output_sha256": sha256(output),
                }
            ],
        }
        release_path = self.evidence / "release-bindings.json"
        write_json(release_path, manifest)
        return {
            "relative": relative,
            "original": original,
            "candidate": candidate,
            "output": output,
            "matrix": matrix_path,
            "source_text_audit": source_text_audit_path,
            "preflight": preflight_path,
            "review": review_path,
            "manifest": release_path,
        }

    def _validate(self):
        module = load_release_gate()
        with mock.patch.object(
            module,
            "validate_project",
            return_value={"ok": True, "errors": [], "counts": {}},
        ):
            return module.validate_release_project(
                self.root, self.evidence, self.fixture["manifest"]
            )

    def _manifest(self) -> dict:
        return json.loads(self.fixture["manifest"].read_text(encoding="utf-8"))

    def test_accepts_complete_hash_bound_release(self):
        result = self._validate()
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(result["counts"], {"input": 1, "output": 1, "pages": 1})

    def test_missing_each_required_page_binding_is_rejected(self):
        required = {
            "relative_path",
            "page_class",
            "input_sha256",
            "appearance_matrix",
            "source_text_audit",
            "preflight",
            "review",
            "candidate",
            "output_sha256",
        }
        for key in required:
            with self.subTest(key=key):
                manifest = self._manifest()
                manifest["pages"][0].pop(key)
                write_json(self.fixture["manifest"], manifest)
                result = self._validate()
                self.assertFalse(result["ok"])
                self.assertIn("PAGE_BINDING_INVALID", result["errors"])
                write_json(self.fixture["manifest"], self._make_manifest_snapshot())

    def _make_manifest_snapshot(self) -> dict:
        relative = self.fixture["relative"]
        return {
            "schema_version": "1.1",
            "sealed_inputs": [
                {"relative_path": relative, "sha256": sha256(self.fixture["original"])}
            ],
            "pages": [
                {
                    "relative_path": relative,
                    "page_class": "unchanged",
                    "input_sha256": sha256(self.fixture["original"]),
                    "appearance_matrix": self._artifact(self.fixture["matrix"]),
                    "source_text_audit": self._artifact(self.fixture["source_text_audit"]),
                    "preflight": self._artifact(self.fixture["preflight"]),
                    "review": self._artifact(self.fixture["review"]),
                    "candidate": self._artifact(self.fixture["candidate"]),
                    "output_sha256": sha256(self.fixture["output"]),
                }
            ],
        }

    def test_rehashed_tampered_deep_artifacts_are_rejected(self):
        cases = (
            ("matrix", "APPEARANCE_MATRIX_INVALID", lambda doc: doc.update(status="evidence_blocked")),
            ("source_text_audit", "SOURCE_TEXT_AUDIT_INVALID", lambda doc: doc.update(visual_reviewer_id="rapidocr-v1")),
            ("preflight", "PREFLIGHT_INVALID", lambda doc: doc["metrics"].update(grayscale_variance=999)),
            ("review", "REVIEW_INVALID", lambda doc: doc.update(reviewer="reviewer-3")),
        )
        for name, expected, mutate in cases:
            with self.subTest(artifact=name):
                path = self.fixture[name]
                original_bytes = path.read_bytes()
                document = json.loads(original_bytes.decode("utf-8"))
                mutate(document)
                write_json(path, document)
                manifest = self._make_manifest_snapshot()
                write_json(self.fixture["manifest"], manifest)
                result = self._validate()
                self.assertFalse(result["ok"])
                self.assertIn(expected, result["errors"])
                path.write_bytes(original_bytes)
                write_json(self.fixture["manifest"], self._make_manifest_snapshot())

    def test_unchanged_output_must_equal_sealed_input_even_after_rehash(self):
        self._draw_pattern(self.fixture["output"], (220, 20, 20))
        manifest = self._manifest()
        manifest["pages"][0]["output_sha256"] = sha256(self.fixture["output"])
        write_json(self.fixture["manifest"], manifest)

        result = self._validate()

        self.assertFalse(result["ok"])
        self.assertIn("UNCHANGED_SOURCE_MISMATCH", result["errors"])

    def test_current_candidate_hash_is_required(self):
        self._draw_pattern(self.fixture["candidate"], (20, 220, 20))
        result = self._validate()
        self.assertFalse(result["ok"])
        self.assertIn("CANDIDATE_HASH_MISMATCH", result["errors"])

    def test_output_relative_path_set_and_count_must_exactly_match_inputs(self):
        extra = self.outputs / "extra.jpg"
        self._draw_pattern(extra, (20, 20, 220))
        result = self._validate()
        self.assertFalse(result["ok"])
        self.assertIn("OUTPUT_NAME_SET_MISMATCH", result["errors"])

    def test_existing_final_validator_failure_is_a_hard_blocker(self):
        module = load_release_gate()
        with mock.patch.object(
            module,
            "validate_project",
            return_value={"ok": False, "errors": ["upstream failed"], "counts": {}},
        ):
            result = module.validate_release_project(
                self.root, self.evidence, self.fixture["manifest"]
            )
        self.assertFalse(result["ok"])
        self.assertIn("FINAL_VALIDATION_FAILED", result["errors"])


if __name__ == "__main__":
    unittest.main()
