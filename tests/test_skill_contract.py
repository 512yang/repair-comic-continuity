import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    path = SKILL_ROOT / relative_path
    if not path.is_file():
        raise AssertionError(f"required Skill resource is missing: {relative_path}")
    return path.read_text(encoding="utf-8")


class SceneClusterSkillContractTests(unittest.TestCase):
    def test_public_skill_docs_are_utf8_without_replacement_or_typical_mojibake(self):
        documents = {
            "SKILL.md": read("SKILL.md"),
            **{
                path.name: path.read_text(encoding="utf-8")
                for path in sorted((SKILL_ROOT / "references").glob("*.md"))
            },
        }
        mojibake = re.compile(r"\ufffd|â|Ã|鈥")
        for name, document in documents.items():
            with self.subTest(document=name):
                self.assertIsNone(mojibake.search(document))

    def test_skill_names_codex_coordinator_as_orchestrator_without_monolithic_cli(self):
        skill = read("SKILL.md")
        self.assertIn("Codex coordinator is the orchestrator", skill)
        self.assertIn(
            "library APIs are intentionally composed by the coordinator; "
            "there is no monolithic image-generation CLI",
            skill,
        )

    def test_migration_apply_requires_confirmed_alignment_and_independent_review(self):
        migration_docs = "\n".join(
            (read("SKILL.md"), read("references/scene-cluster-pipeline.md"))
        )
        self.assertIn("migration is dry-run only by default", migration_docs)
        self.assertIn("every novel alignment is `confirmed`", migration_docs)
        self.assertIn("independent accepted migration review", migration_docs)

    def test_page_class_action_mapping_and_undersized_block_are_documented(self):
        documents = "\n".join(
            (
                read("SKILL.md"),
                read("references/scene-cluster-pipeline.md"),
                read("references/qa-checklist.md"),
            )
        )
        self.assertIn(
            "`page_class=full_page_redraw` maps to `action=full_page_regeneration`",
            documents,
        )
        self.assertIn("undersized: true", documents)
        self.assertIn("must remain blocked", documents)
        self.assertIn("boundary_exception=true", documents)
        self.assertIn("project_total_below_min", documents)
        self.assertIn("evidence_resolution", documents)

    def test_skill_routes_to_both_new_references_and_uses_dynamic_page_count(self):
        skill = read("SKILL.md")
        self.assertIn("references/scene-cluster-pipeline.md", skill)
        self.assertIn("references/failure-learning.md", skill)
        self.assertIn("scene_cluster_v1", skill)
        self.assertRegex(skill, r"动态页数|dynamic page count|输入页数 `?N`?")
        self.assertNotIn("Expect 98 pages unless", skill)
        self.assertNotIn("Name the 98 final images", skill)

    def test_skill_defines_four_page_classes_and_exact_n_output(self):
        skill = read("SKILL.md")
        for page_class in (
            "unchanged",
            "text_only",
            "full_page_regeneration",
            "evidence_blocked",
        ):
            self.assertIn(page_class, skill)
        self.assertRegex(skill, r"输入.*N.*输出.*N|exactly `?N`?.*output")

    def test_skill_states_parallel_safety_and_quality_gates(self):
        skill = read("SKILL.md")
        required_terms = (
            "coordinator",
            "3 workers",
            "lease",
            "candidate",
            "single-writer",
            "content cache",
            "circuit breaker",
            "canary",
            "structured prompt",
            "preflight",
            "generated_by != reviewed_by",
        )
        for term in required_terms:
            with self.subTest(term=term):
                self.assertIn(term, skill)

    def test_scene_pipeline_reference_covers_cluster_queue_and_promotion_contracts(self):
        reference = read("references/scene-cluster-pipeline.md")
        for term in (
            "8–20",
            "±2",
            "reference role",
            "canary",
            "queued",
            "leased",
            "completed",
            "failed",
            "atomic",
            "speaker graph",
            "page_density_budget",
            "preflight",
            "independent review",
            "single-writer",
            "bijection",
        ):
            with self.subTest(term=term):
                self.assertIn(term, reference)

    def test_failure_learning_reference_covers_taxonomy_promotion_and_rollback(self):
        reference = read("references/failure-learning.md")
        for code in (
            "style_drift",
            "identity_drift",
            "costume_prop_drift",
            "composition_drift",
            "anatomy_error",
            "scene_drift",
            "text_leak",
            "over_rendering",
        ):
            with self.subTest(code=code):
                self.assertIn(code, reference)
        for term in (
            "before",
            "after",
            "outcome",
            "page → cluster → project → skill_candidate",
            "two clusters",
            "revoke",
            "circuit breaker",
            "regression",
        ):
            with self.subTest(term=term):
                self.assertIn(term, reference)

    def test_continuity_reference_defines_state_machines_and_speaker_graph(self):
        reference = read("references/continuity-rules.md")
        for term in (
            "present",
            "absent",
            "unknown",
            "speaker graph",
            "art text",
            "extra",
        ):
            with self.subTest(term=term):
                self.assertIn(term, reference)

    def test_qa_reference_checks_scene_pipeline_completion(self):
        checklist = read("references/qa-checklist.md")
        for term in (
            "canary",
            "reference contamination",
            "lease",
            "generated_by != reviewed_by",
            "failure_rule_ids",
            "speaker graph",
            "page_density_budget",
            "exactly N",
            "zero unresolved tasks",
        ):
            with self.subTest(term=term):
                self.assertIn(term, checklist)

    def test_v3_evidence_registry_and_identity_bindings_are_documented(self):
        skill = read("SKILL.md")
        reference = read("references/scene-cluster-pipeline.md")
        self.assertIn("schema_version: 3.0", skill)
        for filename in (
            "scene_clusters.json",
            "style_reference_packs.json",
            "task_queue.json",
            "failure_learning.json",
            "scene_cluster_qa.json",
        ):
            with self.subTest(filename=filename):
                self.assertIn(filename, skill)
                self.assertIn(filename, reference)
        for term in (
            "registry_hash",
            "stable_pages",
            "approved_hashes",
            "completed_by == generated_by",
            "reviewed_by == page_qa.reviewer",
        ):
            with self.subTest(term=term):
                self.assertIn(term, reference)

    def test_text_source_binding_and_mode_split_are_documented(self):
        skill = read("SKILL.md")
        for term in (
            "source_novel_text",
            "raw UTF-8 SHA-256",
            "exact source slice",
            "must not enter the compiled prompt",
            "block_replace",
            "page_reset",
        ):
            with self.subTest(term=term):
                self.assertIn(term, skill)

    def test_skill_has_only_supported_frontmatter_fields(self):
        skill = read("SKILL.md")
        match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", skill, re.DOTALL)
        self.assertIsNotNone(match)
        keys = {
            line.split(":", 1)[0].strip()
            for line in match.group("frontmatter").splitlines()
            if ":" in line
        }
        self.assertEqual({"name", "description"}, keys)


if __name__ == "__main__":
    unittest.main()
