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


if __name__ == "__main__":
    unittest.main()
