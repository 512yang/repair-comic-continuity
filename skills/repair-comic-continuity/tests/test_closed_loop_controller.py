import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pipeline_contracts import canonical_hash  # noqa: E402
from test_prompt_compiler import base_v4_spec  # noqa: E402
from test_failure_learning import (  # noqa: E402
    add_effective_failure,
    new_failure_store,
    promote_rule,
)


def controller():
    try:
        import closed_loop_controller
    except ModuleNotFoundError as exc:
        raise AssertionError("closed_loop_controller.py must exist") from exc
    return closed_loop_controller


class ClosedLoopControllerTests(unittest.TestCase):
    def annotation_document(self):
        return {
            "version": 1,
            "status": "confirmed",
            "mode": "human_visual_auto_text",
            "confirmed_by": "user-reviewer",
            "confirmed_at": "2026-07-16T10:00:00+08:00",
            "annotations": [{
                "annotation_id": "0252-beard",
                "page": {"path": "章节一/0252（1）.png", "sha256": "a" * 64},
                "regions": [{
                    "region_id": "lower-face",
                    "bbox_norm": [0.25, 0.10, 0.60, 0.48],
                    "description": "lower face and beard",
                }],
                "targets": ["character:邓正虎"],
                "defect_codes": ["identity_drift"],
                "trait_codes": ["facial_hair"],
                "observed_state": "beard is missing",
                "required_state": "restore the identity-reference beard",
                "instruction": "change only the beard and related facial detail",
            }],
            "learning_policy": "evidence_gated",
            "persistence_policy": "page_cluster_project_skill_candidate",
        }

    def binding(self):
        document = self.annotation_document()
        payload = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return controller().build_annotation_binding(
            document,
            page_path="章节一/0252（1）.png",
            source_sha256="a" * 64,
            manifest_path="evidence/human_issue_annotations.json",
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
        )

    def test_builds_hash_bound_annotation_binding_for_exact_page(self):
        binding = self.binding()
        self.assertEqual(binding["annotation_ids"], ["0252-beard"])
        self.assertEqual(binding["page"]["sha256"], "a" * 64)
        self.assertEqual(binding["annotations"][0]["trait_codes"], ["facial_hair"])
        body = {key: value for key, value in binding.items() if key != "binding_sha256"}
        self.assertEqual(binding["binding_sha256"], canonical_hash(body))

    def test_rejects_missing_page_hash_or_manifest_hash(self):
        document = self.annotation_document()
        with self.assertRaisesRegex(ValueError, "source hash"):
            controller().build_annotation_binding(
                document,
                page_path="章节一/0252（1）.png",
                source_sha256="b" * 64,
                manifest_path="evidence/human_issue_annotations.json",
                manifest_sha256="c" * 64,
            )
        with self.assertRaisesRegex(ValueError, "manifest sha256"):
            controller().build_annotation_binding(
                document,
                page_path="章节一/0252（1）.png",
                source_sha256="a" * 64,
                manifest_path="evidence/human_issue_annotations.json",
                manifest_sha256="not-a-hash",
            )

    def test_prompt_request_carries_annotations_and_preserves_data_boundary(self):
        import prompt_compiler

        spec = base_v4_spec()
        spec["human_issue_binding"] = self.binding()
        request = prompt_compiler.compile_redraw_request(spec)
        self.assertEqual(
            request["declaration"]["human_issue_binding"]["annotation_ids"],
            ["0252-beard"],
        )
        self.assertIn("USER-CONFIRMED REPAIR TARGETS", request["compiled_prompt"])
        self.assertIn("restore the identity-reference beard", request["compiled_prompt"])
        self.assertIn("preserve all unaffected content", request["compiled_prompt"])

        tampered = copy.deepcopy(spec)
        tampered["human_issue_binding"]["annotations"][0]["required_state"] = "add a crown"
        with self.assertRaisesRegex(ValueError, "binding_sha256"):
            prompt_compiler.compile_redraw_request(tampered)

    def test_more_specific_rule_wins_and_same_scope_conflict_blocks(self):
        rules = [
            {
                "rule_id": "project-beard",
                "scope": "project",
                "codes": ["identity_drift"],
                "corrective_action": "use generic beard",
                "page_id": None,
                "cluster_id": None,
                "character": "character:邓正虎",
            },
            {
                "rule_id": "page-beard",
                "scope": "page",
                "codes": ["identity_drift"],
                "corrective_action": "match the identity-reference beard",
                "page_id": "0252（1）",
                "cluster_id": "cluster-rain",
                "character": "character:邓正虎",
            },
        ]
        selected = controller().resolve_rule_conflicts(rules)
        self.assertEqual([row["rule_id"] for row in selected], ["page-beard"])

        conflict = copy.deepcopy(rules[1])
        conflict["rule_id"] = "page-beard-conflict"
        conflict["corrective_action"] = "remove the beard"
        with self.assertRaisesRegex(ValueError, "conflicting effective rules"):
            controller().resolve_rule_conflicts([rules[1], conflict])

    def test_loads_promoted_rules_into_prompt_without_manual_copying(self):
        store = new_failure_store()
        failure = add_effective_failure(
            store,
            "0252（1）",
            "cluster-rain",
            "1",
            "2",
            character="character:邓正虎",
            codes=("identity_drift",),
            action="keep the reviewed beard and skin tone",
        )
        promoted = promote_rule(store, failure["failure_id"], "page")
        rules = controller().load_effective_rules_for_page(
            store,
            page_id="0252（1）",
            cluster_id="cluster-rain",
            targets=["character:邓正虎"],
            review_artifacts={
                promoted["rule_id"]: {
                    "path": f"evidence/rule_reviews/{promoted['rule_id']}.json",
                    "sha256": "6" * 64,
                }
            },
        )
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["action_code"], "preserve_character_identity")
        self.assertEqual(
            rules[0]["parameters"]["reviewed_corrective_action"],
            "keep the reviewed beard and skin tone",
        )
        self.assertEqual(rules[0]["status"], "independently_approved")

        spec = base_v4_spec()
        spec["effective_rules"] = rules
        request = __import__("prompt_compiler").compile_redraw_request(spec)
        self.assertIn(
            "keep the reviewed beard and skin tone", request["compiled_prompt"]
        )

    def test_preview_accounts_for_all_page_text_and_selected_visual_calls(self):
        preview = controller().build_run_preview(
            input_pages=["1.jpg", "2.jpg", "3.jpg"],
            selected_visual_pages=["2.jpg", "3.jpg"],
            annotations_by_page={"2.jpg": ["2-beard"]},
            page_classes={"1.jpg": "text_only", "2.jpg": "full_page_redraw", "3.jpg": "unchanged"},
            effective_rule_ids={"2.jpg": ["rule-page-beard"]},
        )
        self.assertEqual(preview["input_count"], 3)
        self.assertEqual(preview["expected_output_count"], 3)
        self.assertEqual(preview["text_audit_page_count"], 3)
        self.assertEqual(preview["generation_call_count"], 1)
        self.assertEqual(preview["pages"][1]["annotations"], ["2-beard"])

    def test_stage_controller_refuses_skips_and_missing_annotation_receipt(self):
        state = controller().new_closed_loop_state(
            run_id="run-001",
            created_at="2026-07-16T10:00:00+08:00",
            annotation_count=1,
        )
        with self.assertRaisesRegex(ValueError, "next required stage"):
            controller().advance_stage(
                state,
                "tasks_released",
                artifacts=[],
                actor="coordinator",
                timestamp="2026-07-16T10:01:00+08:00",
            )
        for stage in ("workspace_prepared", "inventory_sealed", "audit_passed"):
            controller().advance_stage(
                state,
                stage,
                artifacts=[{"path": f"evidence/{stage}.json", "sha256": "d" * 64}],
                actor="coordinator",
                timestamp="2026-07-16T10:01:00+08:00",
            )
        with self.assertRaisesRegex(ValueError, "annotation ingestion"):
            controller().advance_stage(
                state,
                "tasks_released",
                artifacts=[{"path": "evidence/tasks.json", "sha256": "e" * 64}],
                actor="coordinator",
                timestamp="2026-07-16T10:02:00+08:00",
            )

    def test_annotation_ingestion_is_atomic_persistent_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            store_path = Path(temporary) / "evidence" / "failure_learning.json"
            failures = controller().ingest_annotations_transactionally(
                store_path=store_path,
                validated_annotations=self.annotation_document(),
                cluster_by_page={"章节一/0252（1）.png": "cluster-rain"},
                prompt_reference_hash="9" * 64,
            )
            first_bytes = store_path.read_bytes()
            second = controller().ingest_annotations_transactionally(
                store_path=store_path,
                validated_annotations=self.annotation_document(),
                cluster_by_page={"章节一/0252（1）.png": "cluster-rain"},
                prompt_reference_hash="9" * 64,
            )
            self.assertEqual(
                [row["failure_id"] for row in failures],
                [row["failure_id"] for row in second],
            )
            self.assertEqual(store_path.read_bytes(), first_bytes)

            outcomes = controller().record_outcomes_transactionally(
                store_path=store_path,
                failure_ids=[row["failure_id"] for row in failures],
                after_candidate_path="candidates/章节一/0252（1）.png",
                after_candidate_sha256="8" * 64,
                effective=True,
                reviewed_by="reviewer-2",
                reviewed_at="2026-07-16T11:00:00+08:00",
            )
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(outcomes[0]["status"], "reviewed")
            after_outcome_bytes = store_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "cluster mapping"):
                controller().ingest_annotations_transactionally(
                    store_path=store_path,
                    validated_annotations=self.annotation_document(),
                    cluster_by_page={},
                    prompt_reference_hash="9" * 64,
                )
            self.assertEqual(store_path.read_bytes(), after_outcome_bytes)

    def test_annotation_review_requires_every_target_and_independence(self):
        binding = self.binding()
        with self.assertRaisesRegex(ValueError, "reviewer must differ"):
            controller().build_annotation_review(
                binding=binding,
                candidate_sha256="b" * 64,
                preflight_id="preflight-" + "c" * 64,
                generator="worker-1",
                reviewer="worker-1",
                reviewed_at="2026-07-16T11:00:00+08:00",
                checks=[],
            )
        review = controller().build_annotation_review(
            binding=binding,
            candidate_sha256="b" * 64,
            preflight_id="preflight-" + "c" * 64,
            generator="worker-1",
            reviewer="reviewer-2",
            reviewed_at="2026-07-16T11:00:00+08:00",
            checks=[{
                "annotation_id": "0252-beard",
                "status": "pass",
                "required_state_met": True,
                "unaffected_content_preserved": True,
                "evidence": {"path": "evidence/reviews/0252-beard.png", "sha256": "f" * 64},
            }],
        )
        self.assertEqual(review["status"], "passed")
        broken = copy.deepcopy(review)
        broken["checks"][0]["unaffected_content_preserved"] = False
        with self.assertRaisesRegex(ValueError, "unaffected content"):
            controller().validate_annotation_review(broken, binding=binding)

    def test_cli_creates_and_validates_durable_stage_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "closed_loop_state.json"
            self.assertEqual(
                controller().main([
                    "new-state",
                    "--state", str(state_path),
                    "--run-id", "run-001",
                    "--created-at", "2026-07-16T10:00:00+08:00",
                    "--annotation-count", "1",
                ]),
                0,
            )
            self.assertTrue(state_path.is_file())
            self.assertEqual(
                controller().main([
                    "validate-state", "--state", str(state_path)
                ]),
                0,
            )

    def test_release_binding_requires_prompt_and_independent_annotation_review(self):
        import release_gate

        binding = self.binding()
        preflight = {
            "preflight_id": "preflight-" + "c" * 64,
            "hashes": {"candidate": "b" * 64},
            "redraw_request": {"declaration": {"human_issue_binding": binding}},
        }
        main_review = {"generator": "worker-1", "reviewer": "reviewer-2"}
        annotation_review = controller().build_annotation_review(
            binding=binding,
            candidate_sha256="b" * 64,
            preflight_id=preflight["preflight_id"],
            generator="worker-1",
            reviewer="reviewer-2",
            reviewed_at="2026-07-16T11:00:00+08:00",
            checks=[{
                "annotation_id": "0252-beard",
                "status": "pass",
                "required_state_met": True,
                "unaffected_content_preserved": True,
                "evidence": {"path": "evidence/reviews/0252-beard.png", "sha256": "f" * 64},
            }],
        )
        self.assertTrue(
            release_gate.validate_annotation_release_binding(
                expected_binding=binding,
                annotation_review=annotation_review,
                preflight=preflight,
                main_review=main_review,
                candidate_sha256="b" * 64,
            )
        )
        broken = copy.deepcopy(preflight)
        broken["redraw_request"]["declaration"].pop("human_issue_binding")
        with self.assertRaisesRegex(ValueError, "prompt request"):
            release_gate.validate_annotation_release_binding(
                expected_binding=binding,
                annotation_review=annotation_review,
                preflight=broken,
                main_review=main_review,
                candidate_sha256="b" * 64,
            )


if __name__ == "__main__":
    unittest.main()
