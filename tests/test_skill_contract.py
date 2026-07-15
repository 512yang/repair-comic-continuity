import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    path = SKILL_ROOT / relative_path
    if not path.is_file():
        raise AssertionError(f"required Skill resource is missing: {relative_path}")
    return path.read_text(encoding="utf-8")


class ContinuityV4SkillContractTests(unittest.TestCase):
    def test_required_v4_phrases_are_public(self):
        required_phrases = {
            "SKILL.md": [
                "continuity_v4",
                "same relative path, filename, and extension",
                "continuity_first_full_page",
                "contact sheets are orientation aids only",
                "generator cannot approve its own candidate",
                "validation is read-only",
            ],
            "references/continuity-rules.md": [
                "beard",
                "moustache",
                "sideburn",
                "Non-critical action variation is not a defect",
                "entity_state_timeline.json",
            ],
            "references/qa-checklist.md": [
                "full-resolution artifact",
                "blind review",
                "exact relative-path set",
                "malformed glyph",
            ],
        }
        for filename, phrases in required_phrases.items():
            document = read(filename)
            for phrase in phrases:
                with self.subTest(filename=filename, phrase=phrase):
                    self.assertIn(phrase, document)

    def test_public_text_is_utf8_without_replacement_or_known_mojibake(self):
        paths = [SKILL_ROOT / "SKILL.md"]
        paths.extend(sorted((SKILL_ROOT / "references").glob("*.md")))
        paths.extend(sorted((SKILL_ROOT / "agents").glob("*.yaml")))
        mojibake = re.compile(r"\ufffd|锛|鈥|卤|杈|婕|鏂囧瓧|鍙傝€")
        for path in paths:
            document = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(SKILL_ROOT).as_posix()):
                self.assertIsNone(mojibake.search(document))

    def test_skill_is_concise_and_frontmatter_is_supported(self):
        skill = read("SKILL.md")
        self.assertLess(len(skill.splitlines()), 500)
        match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", skill, re.DOTALL)
        self.assertIsNotNone(match)
        keys = {
            line.split(":", 1)[0].strip()
            for line in match.group("frontmatter").splitlines()
            if ":" in line
        }
        self.assertEqual({"name", "description"}, keys)

    def test_exact_names_replace_legacy_sequential_renaming(self):
        skill = read("SKILL.md")
        self.assertIn("identical relative path and extension", skill)
        self.assertNotIn("0001.jpg", skill)
        self.assertNotIn("scene_cluster_v1", skill)
        self.assertNotIn("schema_version: 3.0", skill)

    def test_four_classes_and_textless_redraw_order_are_explicit(self):
        skill = read("SKILL.md")
        for page_class in ("unchanged", "text_only", "full_page_redraw", "evidence_blocked"):
            self.assertIn(page_class, skill)
        self.assertLess(skill.index("full-page textless candidate"), skill.index("Restore text only after"))
        self.assertIn("remove all ordinary text", skill)
        self.assertIn("deterministic rendering", skill)

    def test_parallel_cost_controls_and_review_independence_are_explicit(self):
        documents = "\n".join(
            (read("SKILL.md"), read("references/scene-cluster-pipeline.md"), read("references/failure-learning.md"))
        )
        for phrase in (
            "at most 3 workers",
            "canary",
            "second failure",
            "pause that lane",
            "clean-control",
            "independent-review",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, documents)

    def test_audit_only_and_final_validation_never_generate_or_rebuild(self):
        skill = read("SKILL.md")
        self.assertIn("scripts/validate_audit.py", skill)
        self.assertIn("rejects candidate images", skill)
        self.assertIn("must not rebuild evidence", skill)
        self.assertIn("V3 migration output is diagnostic only", skill)

    def test_ui_metadata_describes_v4_without_shortcut(self):
        metadata = read("agents/openai.yaml")
        self.assertIn("漫画连续性修复 V4", metadata)
        self.assertIn("continuity_v4", metadata)
        self.assertIn("先审核再修复", metadata)


if __name__ == "__main__":
    unittest.main()
