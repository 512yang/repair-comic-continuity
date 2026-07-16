import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from failure_learning import new_failure_store  # noqa: E402


def revision_module():
    try:
        import validate_human_revision_feedback
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "validate_human_revision_feedback.py must exist"
        ) from exc
    return validate_human_revision_feedback


class HumanRevisionFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "输入"
        self.input_dir.mkdir()
        self.source = self.input_dir / "0003.jpg"
        self.source.write_bytes(b"sealed-source-page")
        self.candidate = self.root / "candidates" / "attempt-1" / "0003.jpg"
        self.candidate.parent.mkdir(parents=True)
        self.candidate.write_bytes(b"first-output-candidate")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, origin="missed_detection", suffix="skin"):
        return {
            "feedback_id": f"0003-attempt-2-{suffix}",
            "origin": origin,
            "attempt": 2,
            "page": {
                "path": "0003.jpg",
                "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            },
            "candidate": {
                "path": "candidates/attempt-1/0003.jpg",
                "sha256": hashlib.sha256(self.candidate.read_bytes()).hexdigest(),
            },
            "parent_annotation_ids": [],
            "regions": [{
                "region_id": "face",
                "bbox_norm": [0.2, 0.1, 0.5, 0.5],
                "description": "skin tone",
            }],
            "user_note": "同一男人肤色仍然不一致",
        }

    def manifest(self, records=None):
        return {
            "version": 1,
            "status": "confirmed",
            "confirmed_by": "user-reviewer",
            "confirmed_at": "2026-07-16T18:00:00+08:00",
            "attempt": 2,
            "feedback": records if records is not None else [self.record()],
            "learning_policy": "evidence_gated",
        }

    def validate(self, document):
        return revision_module().validate_human_revision_feedback(
            document,
            self.input_dir,
            ["0003.jpg"],
            self.root,
        )

    def test_accepts_all_feedback_origins_and_binds_live_hashes(self):
        origins = [
            "missed_detection",
            "unresolved",
            "introduced_error",
            "damaged_correct_content",
            "text_result_error",
        ]
        records = [
            self.record(origin, f"issue-{index}")
            for index, origin in enumerate(origins)
        ]
        result = self.validate(self.manifest(records))
        self.assertEqual(result["attempt"], 2)
        self.assertEqual(result["feedback_count"], 5)
        self.assertEqual(
            [row["origin"] for row in result["feedback"]], origins
        )
        self.assertEqual(result["feedback"][0]["candidate"]["sha256"],
                         hashlib.sha256(self.candidate.read_bytes()).hexdigest())

    def test_rejects_stale_hash_duplicate_id_attempt_zero_and_unknown_page(self):
        stale = self.manifest()
        stale["feedback"][0]["candidate"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "candidate sha256 mismatch"):
            self.validate(stale)

        duplicate = self.manifest([self.record(), self.record()])
        with self.assertRaisesRegex(ValueError, "duplicate feedback_id"):
            self.validate(duplicate)

        zero = self.manifest()
        zero["attempt"] = 0
        zero["feedback"][0]["attempt"] = 0
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.validate(zero)

        unknown = self.manifest()
        unknown["feedback"][0]["page"]["path"] = "0099.jpg"
        with self.assertRaisesRegex(ValueError, "unknown input page"):
            self.validate(unknown)

    def test_refuses_to_overwrite_a_prior_attempt(self):
        normalized = self.validate(self.manifest())
        path = self.root / "evidence" / "human_revision_feedback" / "attempt-2.json"
        revision_module().write_revision_feedback_manifest(path, normalized)
        first = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            revision_module().write_revision_feedback_manifest(path, normalized)
        self.assertEqual(path.read_bytes(), first)

    def test_revision_receipt_blocks_task_release_until_feedback_is_ingested(self):
        import closed_loop_controller

        state = closed_loop_controller.new_closed_loop_state(
            run_id="run-revision-2",
            created_at="2026-07-16T18:00:00+08:00",
            annotation_count=0,
            revision_feedback_count=1,
        )
        for stage in ("workspace_prepared", "inventory_sealed", "audit_passed"):
            closed_loop_controller.advance_stage(
                state,
                stage,
                artifacts=[{"path": f"evidence/{stage}.json", "sha256": "a" * 64}],
                actor="coordinator",
                timestamp="2026-07-16T18:01:00+08:00",
            )
        with self.assertRaisesRegex(ValueError, "revision feedback ingestion"):
            closed_loop_controller.advance_stage(
                state,
                "tasks_released",
                artifacts=[{"path": "evidence/tasks.json", "sha256": "b" * 64}],
                actor="coordinator",
                timestamp="2026-07-16T18:02:00+08:00",
            )
        closed_loop_controller.advance_stage(
            state,
            "revision_feedback_ingested",
            artifacts=[{
                "path": "evidence/human_revision_feedback/attempt-2.json",
                "sha256": "c" * 64,
            }],
            actor="coordinator",
            timestamp="2026-07-16T18:02:00+08:00",
        )
        receipt = closed_loop_controller.advance_stage(
            state,
            "tasks_released",
            artifacts=[{"path": "evidence/tasks.json", "sha256": "b" * 64}],
            actor="coordinator",
            timestamp="2026-07-16T18:03:00+08:00",
        )
        self.assertEqual(receipt["stage"], "tasks_released")

    def test_learning_intake_records_observed_failures_only(self):
        from human_issue_learning import ingest_human_revision_feedback

        validated = self.validate(self.manifest())
        store = new_failure_store()
        failures = ingest_human_revision_feedback(
            store,
            validated,
            {"0003.jpg": "cluster-water"},
            "d" * 64,
        )
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["status"], "observed")
        self.assertFalse(failures[0]["effective"])
        self.assertEqual(
            failures[0]["before_candidate"]["path"],
            "candidates/attempt-1/0003.jpg",
        )
        self.assertIn("missed_detection", failures[0]["diagnosis"])


if __name__ == "__main__":
    unittest.main()
