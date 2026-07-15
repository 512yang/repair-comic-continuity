import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    path = SKILL_ROOT / relative_path
    if not path.is_file():
        raise AssertionError(f"required Skill resource is missing: {relative_path}")
    return path.read_text(encoding="utf-8")


class ContinuityV5SkillContractTests(unittest.TestCase):
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

    def test_ui_metadata_uses_unified_v5_entrypoint_without_shortcut(self):
        metadata = read("agents/openai.yaml")
        self.assertIn("漫画连续性修复 V5", metadata)
        self.assertIn("continuity_v5_unified", metadata)
        self.assertNotIn('default_prompt: "使用 $repair-comic-continuity 按 continuity_v4', metadata)
        self.assertIn("先审核再修复", metadata)

    def test_production_entrypoint_requires_isolated_run_and_benchmark_gate(self):
        skill = read("SKILL.md")
        for phrase in (
            "scripts/prepare_run_workspace.py",
            "scripts/validate_detection_benchmark.py",
            "isolated run root",
            "zero missed confirmed defects",
            "correct-page protection",
            "scripts/validate_source_text_audit.py",
            "every adjacent Chinese repeat",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_signed_release_certificate_skips_only_redundant_generic_review(self):
        skill = read("SKILL.md")
        for phrase in (
            "scripts/validate_release_certificate.py",
            "do not ask the user to recreate the packaged generic golden dataset",
            "automatic project canary",
            "certificate invalid",
            "auditor disagreement",
            "evidence_blocked",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)
        self.assertIn("never bypasses project-specific audit gates", skill)

    def test_confirmed_source_defects_can_reach_redraw_without_weakening_release(self):
        skill = read("SKILL.md")
        for phrase in (
            "defects_confirmed",
            "two independent full-resolution reviewers",
            "eligible for `full_page_redraw` classification",
            "release still requires a `confirmed` appearance matrix",
            "every per-page source text audit",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_production_promotion_and_release_are_machine_gated(self):
        skill = read("SKILL.md")
        for phrase in (
            "scripts/promote_outputs.py",
            "scripts/release_gate.py",
            "unchanged pages are copied only from sealed input",
            "provisional until the release gate passes",
            "never trust a self-reported passed or accepted string",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_source_text_audit_cannot_skip_malformed_glyph_shapes(self):
        documents = read("SKILL.md") + "\n" + read("references/qa-checklist.md")
        for phrase in (
            "source glyph board",
            "every ordinary-text block",
            "explicit non-OCR-only shape decision for every occurrence of 强 and 遇",
            "page cannot be classified",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, documents)

    def test_appearance_gate_includes_identity_geometry_not_only_color_and_costume(self):
        documents = read("SKILL.md") + "\n" + read("references/continuity-rules.md")
        for phrase in ("face shape", "body build"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, documents)

    def test_human_visual_scope_never_weakens_full_page_text_repair(self):
        documents = read("SKILL.md") + "\n" + read("references/scene-cluster-pipeline.md")
        for phrase in (
            "human_visual_auto_text",
            "human_visual_selection.json",
            "manual list controls visual redraw scope only",
            "every input page still receives",
            "page_reset_preserve_style",
            "unselected pages cannot enter `full_page_redraw`",
            "exact input/output bijection",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, documents)


if __name__ == "__main__":
    unittest.main()
