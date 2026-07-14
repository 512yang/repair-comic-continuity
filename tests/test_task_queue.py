import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from task_queue import (  # noqa: E402
    add_task,
    claim_task,
    complete_task,
    fail_task,
    is_lane_paused,
    load_queue,
    make_cache_key,
    new_queue,
    queue_metrics,
    queue_registry_hash,
    record_cache_access,
    recover_expired,
    reopen_lane,
    save_queue,
)
import task_queue as task_queue_module  # noqa: E402
from pipeline_contracts import canonical_hash  # noqa: E402


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def concurrent_save_worker(path, ready, start, results):
    stale = load_queue(path)
    ready.put("ready")
    if not start.wait(timeout=15):
        results.put(("error", "start timeout"))
        return
    try:
        revision = save_queue(path, stale)
    except ValueError as exc:
        results.put(("error", str(exc)))
    else:
        results.put(("ok", revision))


def add(queue, page_id, payload_hash, *, task_type="redraw", cluster_id="c1", prompt="p1"):
    return add_task(
        queue,
        page_id,
        task_type,
        payload_hash,
        cluster_id=cluster_id,
        prompt_reference_hash=prompt,
        now=NOW,
    )


class TaskQueueTests(unittest.TestCase):
    def test_new_queue_enforces_worker_cap_range_and_defaults_to_three(self):
        self.assertEqual(new_queue()["max_active_workers"], 3)
        for value in (1, 2, 3):
            with self.subTest(value=value):
                self.assertEqual(new_queue(max_active_workers=value)["max_active_workers"], value)
        for value in (0, 4, True, "3"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    new_queue(max_active_workers=value)

    def test_claim_enforces_unique_worker_cap_and_one_active_lease_per_worker(self):
        queue = new_queue(max_active_workers=2)
        for page_id in ("1", "2", "3"):
            add(queue, page_id, f"payload-{page_id}", task_type="text")

        self.assertIsNotNone(claim_task(queue, "worker-a", NOW))
        self.assertIsNone(claim_task(queue, "worker-a", NOW))
        self.assertIsNotNone(claim_task(queue, "worker-b", NOW))
        self.assertIsNone(claim_task(queue, "worker-c", NOW))

    def test_expired_lease_frees_worker_slot_before_claim_cap_check(self):
        queue = new_queue(max_active_workers=1)
        first = add(queue, "1", "first", task_type="text")
        second = add(queue, "2", "second", task_type="text")
        claim_task(queue, "worker-a", NOW, lease_seconds=1)

        claimed = claim_task(queue, "worker-b", NOW + timedelta(seconds=1))

        self.assertEqual(claimed["task_id"], first["task_id"])
        self.assertEqual(claimed["lease_owner"], "worker-b")
        self.assertEqual(second["state"], "queued")

    def test_expensive_visual_waits_for_completed_and_independently_approved_canary(self):
        queue = new_queue()
        canary = add_task(
            queue,
            "1",
            "full_page_redraw",
            "canary-payload",
            cluster_id="scene-1",
            prompt_reference_hash="refs-1",
            now=NOW,
            is_canary=True,
        )
        expensive = add_task(
            queue,
            "2",
            "full_page_redraw",
            "page-payload",
            cluster_id="scene-1",
            prompt_reference_hash="refs-1",
            now=NOW,
            canary_task_id=canary["task_id"],
        )

        claimed = claim_task(queue, "visual-worker", NOW)
        self.assertEqual(claimed["task_id"], canary["task_id"])
        complete_task(
            queue,
            canary["task_id"],
            "visual-worker",
            "canary.png",
            "canary-sha",
            NOW,
        )
        self.assertIsNone(claim_task(queue, "visual-worker-2", NOW))

        task_queue_module.approve_canary(
            queue,
            canary["task_id"],
            "review-1",
            "coordinator",
            NOW,
        )
        claimed = claim_task(queue, "visual-worker-2", NOW)
        self.assertEqual(claimed["task_id"], expensive["task_id"])

    def test_canary_approval_requires_completion_review_identity_and_timezone(self):
        queue = new_queue()
        canary = add_task(
            queue,
            "1",
            "visual",
            "canary-payload",
            cluster_id="scene-1",
            now=NOW,
            is_canary=True,
        )
        for review_id, reviewed_by, reviewed_at in (
            ("review-1", "coordinator", NOW),
            ("", "coordinator", NOW),
            ("review-1", "", NOW),
            ("review-1", "coordinator", NOW.replace(tzinfo=None)),
        ):
            with self.subTest(
                review_id=review_id,
                reviewed_by=reviewed_by,
                reviewed_at=reviewed_at,
            ):
                with self.assertRaises(ValueError):
                    task_queue_module.approve_canary(
                        queue,
                        canary["task_id"],
                        review_id,
                        reviewed_by,
                        reviewed_at,
                    )

        claim_task(queue, "canary-generator", NOW)
        complete_task(
            queue,
            canary["task_id"],
            "canary-generator",
            "canary.png",
            "canary-sha",
            NOW,
        )
        before_self_review = copy.deepcopy(queue)
        with self.assertRaisesRegex(ValueError, "independent"):
            task_queue_module.approve_canary(
                queue,
                canary["task_id"],
                "review-self",
                "CANARY-GENERATOR",
                NOW,
            )
        self.assertEqual(queue, before_self_review)

    def test_expensive_task_rejects_missing_or_cross_cluster_canary(self):
        queue = new_queue()
        canary = add_task(
            queue,
            "1",
            "visual",
            "canary-payload",
            cluster_id="scene-1",
            now=NOW,
            is_canary=True,
        )
        with self.assertRaises(ValueError):
            add_task(
                queue,
                "2",
                "visual",
                "missing-canary",
                cluster_id="scene-1",
                now=NOW,
            )
        with self.assertRaises(ValueError):
            add_task(
                queue,
                "2",
                "full_page_redraw",
                "cross-cluster",
                cluster_id="scene-2",
                now=NOW,
                canary_task_id=canary["task_id"],
            )

    def test_text_and_unchanged_tasks_bypass_unapproved_canary_gate(self):
        queue = new_queue()
        canary = add_task(
            queue,
            "1",
            "visual",
            "canary-payload",
            cluster_id="scene-1",
            now=NOW,
            is_canary=True,
        )
        add_task(
            queue,
            "2",
            "full_page_redraw",
            "blocked-payload",
            cluster_id="scene-1",
            now=NOW,
            canary_task_id=canary["task_id"],
        )
        text_task = add(queue, "3", "text-payload", task_type="text", cluster_id="scene-1")
        unchanged_task = add(
            queue,
            "4",
            "unchanged-payload",
            task_type="unchanged",
            cluster_id="scene-1",
        )
        queue["tasks"] = [queue["tasks"][1], text_task, unchanged_task, canary]

        self.assertEqual(claim_task(queue, "text-worker", NOW)["task_id"], text_task["task_id"])
        complete_task(queue, text_task["task_id"], "text-worker", "text.png", "sha-t", NOW)
        self.assertEqual(
            claim_task(queue, "unchanged-worker", NOW)["task_id"],
            unchanged_task["task_id"],
        )

    def test_canary_gate_hash_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            canary = add_task(
                queue,
                "1",
                "visual",
                "canary-payload",
                cluster_id="scene-1",
                now=NOW,
                is_canary=True,
            )
            claim_task(queue, "visual-worker", NOW)
            complete_task(
                queue,
                canary["task_id"],
                "visual-worker",
                "canary.png",
                "canary-sha",
                NOW,
            )
            task_queue_module.approve_canary(
                queue,
                canary["task_id"],
                "review-1",
                "coordinator",
                NOW,
            )
            self.assertEqual(queue["registry_hash"], queue_registry_hash(queue))

            save_queue(path, queue)
            loaded = load_queue(path)

            self.assertEqual(loaded["registry_hash"], queue_registry_hash(loaded))
            self.assertEqual(
                loaded["canary_gates"][canary["task_id"]],
                {
                    "task_id": canary["task_id"],
                    "cluster_id": "scene-1",
                    "approved": True,
                    "review_id": "review-1",
                    "reviewed_by": "coordinator",
                    "reviewed_at": NOW.isoformat(),
                },
            )

    def test_new_queue_has_required_schema_and_idempotent_add(self):
        queue = new_queue()
        self.assertEqual(
            set(queue),
            {
                "schema_version",
                "revision",
                "tasks",
                "paused_lanes",
                "metrics",
                "cache",
                "max_active_workers",
                "canary_gates",
            },
        )
        first = add(queue, "252（1）.jpg", "payload-a")
        second = add(queue, "252(1).png", "payload-a")
        self.assertIs(first, second)
        self.assertEqual(len(queue["tasks"]), 1)
        self.assertEqual(first["page_id"], "252(1)")
        self.assertEqual(first["state"], "queued")
        self.assertEqual(first["attempt_count"], 0)
        self.assertIsNone(first["candidate_path"])
        self.assertIsNone(first["failure_code"])
        self.assertEqual(first["task_id"], first["idempotency_key"])

    def test_task_keeps_structured_timestamp_candidate_and_failure_records(self):
        queue = new_queue()
        task = add(queue, "1", "a")
        self.assertEqual(task["timestamps"]["created_at"], NOW.isoformat())
        self.assertEqual(task["candidate"], {"path": None, "hash": None})
        self.assertEqual(
            task["failure"],
            {
                "code": None,
                "record_id": None,
                "cluster_id": None,
                "prompt_reference_hash": None,
                "timestamp": None,
            },
        )

    def test_two_workers_cannot_claim_tasks_for_the_same_page(self):
        queue = new_queue()
        first = add(queue, "1.jpg", "a", task_type="redraw")
        add(queue, "1.jpg", "b", task_type="text")
        self.assertEqual(claim_task(queue, "worker-a", NOW)["task_id"], first["task_id"])
        self.assertIsNone(claim_task(queue, "worker-b", NOW))

    def test_claim_filters_task_types_and_skips_only_paused_lane(self):
        queue = new_queue()
        add(queue, "1", "a", task_type="redraw", cluster_id="paused")
        wanted = add(queue, "2", "b", task_type="text", cluster_id="open")
        queue["paused_lanes"]["paused::redraw"] = {"reason": "test"}
        claimed = claim_task(queue, "worker", NOW, task_types={"text"})
        self.assertEqual(claimed["task_id"], wanted["task_id"])

    def test_claim_validates_worker_time_and_lease(self):
        queue = new_queue()
        add(queue, "1", "a")
        for worker, now, seconds in (("", NOW, 1), ("w", NOW.replace(tzinfo=None), 1), ("w", NOW, 0)):
            with self.subTest(worker=worker, now=now, seconds=seconds):
                with self.assertRaises(ValueError):
                    claim_task(queue, worker, now, lease_seconds=seconds)

    def test_expired_lease_returns_to_queue_at_boundary(self):
        queue = new_queue()
        task = add(queue, "1", "a")
        claim_task(queue, "worker-a", NOW, lease_seconds=10)
        self.assertEqual(recover_expired(queue, NOW + timedelta(seconds=9)), 0)
        self.assertEqual(recover_expired(queue, NOW + timedelta(seconds=10)), 1)
        self.assertEqual(task["state"], "queued")
        self.assertIsNone(task["lease_owner"])
        self.assertIsNone(task["lease_expires_at"])
        self.assertIsNone(task["leased_at"])
        self.assertIsNone(task["timestamps"]["leased_at"])
        claim_task(queue, "worker-b", NOW + timedelta(seconds=10))
        self.assertEqual(task["attempt_count"], 2)

    def test_claim_automatically_recovers_expired_tasks_in_original_order(self):
        queue = new_queue()
        expired = add(queue, "1", "first", task_type="redraw")
        later_same_page = add(queue, "1", "second", task_type="text")
        claim_task(queue, "worker-a", NOW, lease_seconds=10)

        claimed = claim_task(queue, "worker-b", NOW + timedelta(seconds=10))

        self.assertEqual(claimed["task_id"], expired["task_id"])
        self.assertEqual(claimed["lease_owner"], "worker-b")
        self.assertEqual(claimed["attempt_count"], 2)
        self.assertEqual(later_same_page["state"], "queued")

    def test_late_owner_cannot_complete_or_fail_an_expired_lease(self):
        for transition in ("complete", "fail"):
            with self.subTest(transition=transition):
                queue = new_queue()
                task = add(queue, "1", transition)
                claim_task(queue, "owner", NOW, lease_seconds=10)
                late = NOW + timedelta(seconds=10)
                with self.assertRaisesRegex(ValueError, "expired"):
                    if transition == "complete":
                        complete_task(
                            queue,
                            task["task_id"],
                            "owner",
                            "candidate.png",
                            "sha",
                            late,
                        )
                    else:
                        fail_task(
                            queue,
                            task["task_id"],
                            "owner",
                            "artifact",
                            "fr-late",
                            late,
                        )

    def test_complete_requires_owner_and_records_candidate_metadata(self):
        queue = new_queue()
        task = add(queue, "1", "a")
        claim_task(queue, "owner", NOW)
        with self.assertRaises(ValueError):
            complete_task(queue, task["task_id"], "intruder", "candidate.png", "sha", NOW)
        completed = complete_task(
            queue, task["task_id"], "owner", "candidate.png", "sha", NOW,
            cluster_id="c1", prompt_reference_hash="p1",
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["completed_by"], "owner")
        self.assertEqual(completed["candidate_path"], "candidate.png")
        self.assertEqual(completed["candidate_hash"], "sha")
        self.assertEqual(completed["candidate"], {"path": "candidate.png", "hash": "sha"})
        self.assertEqual(completed["timestamps"]["completed_at"], NOW.isoformat())
        with self.assertRaises(ValueError):
            fail_task(queue, task["task_id"], "owner", "bad", "fr-1", NOW)

    def test_fail_records_metadata_and_enforces_owner(self):
        queue = new_queue()
        task = add(queue, "1", "a")
        claim_task(queue, "owner", NOW)
        with self.assertRaises(ValueError):
            fail_task(queue, task["task_id"], "intruder", "artifact", "fr-1", NOW)
        failed = fail_task(
            queue, task["task_id"], "owner", "artifact", "fr-1", NOW,
            cluster_id="c1", prompt_reference_hash="p1",
        )
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["failure_code"], "artifact")
        self.assertEqual(failed["failure_record_id"], "fr-1")
        self.assertEqual(
            failed["failure"],
            {
                "code": "artifact",
                "record_id": "fr-1",
                "cluster_id": "c1",
                "prompt_reference_hash": "p1",
                "timestamp": NOW.isoformat(),
            },
        )

    def test_three_lane_failures_pause_only_that_lane_and_reopen_requires_review(self):
        queue = new_queue()
        for index in range(3):
            task = add(queue, str(index + 1), f"a{index}", cluster_id="c1", prompt=f"p{index}")
            claim_task(queue, "worker", NOW + timedelta(seconds=index))
            fail_task(queue, task["task_id"], "worker", "artifact", f"fr-{index}", NOW + timedelta(seconds=index))
        other = add(queue, "9", "other", cluster_id="c2", prompt="other")
        self.assertTrue(is_lane_paused(queue, "c1", "redraw"))
        self.assertFalse(is_lane_paused(queue, "c2", "redraw"))
        self.assertEqual(claim_task(queue, "other-worker", NOW)["task_id"], other["task_id"])
        with self.assertRaises(ValueError):
            reopen_lane(queue, "c1", "redraw", "")
        reopened_at = NOW + timedelta(seconds=3)
        reopen_lane(queue, "c1", "redraw", "review-1", now=reopened_at)
        self.assertFalse(is_lane_paused(queue, "c1", "redraw"))
        self.assertEqual(
            queue["metrics"]["lane_reopen_history"],
            [
                {
                    "lane": "c1::redraw",
                    "coordinator_review_id": "review-1",
                    "reopened_at": reopened_at.isoformat(),
                }
            ],
        )

    def test_three_candidate_failures_with_same_prompt_pause_current_lane(self):
        queue = new_queue()
        for index, cluster in enumerate(("c1", "c2", "c3")):
            task = add(queue, str(index + 1), f"a{index}", cluster_id=cluster, prompt="shared")
            claim_task(queue, "worker", NOW + timedelta(seconds=index))
            fail_task(queue, task["task_id"], "worker", f"code-{index}", f"fr-{index}", NOW + timedelta(seconds=index))
        self.assertTrue(is_lane_paused(queue, "c3", "redraw"))
        self.assertFalse(is_lane_paused(queue, "c1", "redraw"))
        reopen_lane(
            queue,
            "c3",
            "redraw",
            "review-prompt",
            now=NOW + timedelta(seconds=3),
        )
        next_task = add(queue, "4", "a4", cluster_id="c4", prompt="shared")
        claim_task(queue, "worker", NOW + timedelta(seconds=4))
        fail_task(
            queue,
            next_task["task_id"],
            "worker",
            "code-4",
            "fr-4",
            NOW + timedelta(seconds=4),
        )
        self.assertFalse(is_lane_paused(queue, "c4", "redraw"))

    def test_success_clears_lane_streak_but_does_not_reopen_paused_lane(self):
        queue = new_queue()
        for index in range(2):
            task = add(queue, str(index + 1), f"f{index}", prompt=f"pf{index}")
            claim_task(queue, "w", NOW)
            fail_task(queue, task["task_id"], "w", "same", f"fr{index}", NOW)
        successful = add(queue, "3", "success", prompt="success")
        claim_task(queue, "w", NOW)
        complete_task(queue, successful["task_id"], "w", "c.png", "sha", NOW)
        for index in range(2):
            task = add(queue, str(index + 4), f"g{index}", prompt=f"pg{index}")
            claim_task(queue, "w", NOW)
            fail_task(queue, task["task_id"], "w", "same", f"gr{index}", NOW)
        self.assertFalse(is_lane_paused(queue, "c1", "redraw"))
        reset = add(queue, "8", "reset", prompt="reset")
        claim_task(queue, "w", NOW)
        queue["paused_lanes"]["c1::redraw"] = {"reason": "manual"}
        complete_task(queue, reset["task_id"], "w", "c2.png", "sha2", NOW)
        self.assertTrue(is_lane_paused(queue, "c1", "redraw"))

    def test_cache_key_access_and_dynamic_metrics(self):
        queue = new_queue()
        key = make_cache_key("prompt", "refs", "config")
        self.assertEqual(key, make_cache_key("prompt", "refs", "config"))
        record_cache_access(queue, key, True)
        record_cache_access(queue, key, False)
        completed = add(queue, "1", "done")
        claim_task(queue, "w", NOW)
        complete_task(queue, completed["task_id"], "w", "c.png", "sha", NOW)
        retried = add(queue, "2", "retry", prompt="other")
        claim_task(queue, "w", NOW, lease_seconds=1)
        recover_expired(queue, NOW + timedelta(seconds=1))
        claim_task(queue, "w2", NOW + timedelta(seconds=1))
        fail_task(queue, retried["task_id"], "w2", "artifact", "fr", NOW + timedelta(seconds=1))
        metrics = queue_metrics(queue, NOW + timedelta(seconds=1))
        self.assertEqual(metrics["depth"], {"queued": 0, "leased": 0, "completed": 1, "failed": 1})
        self.assertEqual(metrics["retry_count"], 1)
        self.assertEqual(metrics["first_pass_rate"], 0.5)
        self.assertEqual(metrics["cache_hits"], 1)
        self.assertEqual(metrics["cache_misses"], 1)
        self.assertEqual(metrics["cache_hit_rate"], 0.5)
        self.assertEqual(metrics["failure_codes"], {"artifact": 1})
        self.assertEqual(metrics["retry_counts_by_failure_code"], {"artifact": 1})

    def test_loaded_queue_cache_hit_and_miss_refresh_hash_before_save_reload(self):
        for hit in (True, False):
            with self.subTest(hit=hit), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "queue.json"
                queue = new_queue()
                save_queue(path, queue)
                loaded = load_queue(path)
                key = make_cache_key("prompt", "refs", "config")

                record_cache_access(loaded, key, hit)
                self.assertEqual(loaded["registry_hash"], queue_registry_hash(loaded))
                save_queue(path, loaded)
                reloaded = load_queue(path)

                expected_hits = 1 if hit else 0
                expected_misses = 0 if hit else 1
                self.assertEqual(reloaded["cache"][key]["hits"], expected_hits)
                self.assertEqual(reloaded["cache"][key]["misses"], expected_misses)
                self.assertEqual(reloaded["metrics"]["cache_hits"], expected_hits)
                self.assertEqual(reloaded["metrics"]["cache_misses"], expected_misses)
                self.assertEqual(
                    reloaded["registry_hash"], queue_registry_hash(reloaded)
                )

    def test_lease_expiry_metric_and_repeated_failure_rate(self):
        queue = new_queue()
        expired = add(queue, "9", "leased", cluster_id="c9")
        claim_task(queue, "expiry-worker", NOW, lease_seconds=1)
        for index in range(3):
            task = add(queue, str(index + 1), f"a{index}", cluster_id="c1", prompt=f"p{index}")
            claim_task(queue, "w", NOW)
            fail_task(queue, task["task_id"], "w", "same", f"fr{index}", NOW)
        metrics = queue_metrics(queue, NOW + timedelta(seconds=1))
        self.assertEqual(expired["state"], "leased")
        self.assertEqual(metrics["lease_expiry_count"], 1)
        self.assertAlmostEqual(metrics["repeated_failure_rate"], 2 / 3)

    def test_save_load_revision_conflict_and_corrupt_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            self.assertEqual(save_queue(path, queue, expected_revision=0), 1)
            self.assertEqual(queue["revision"], 1)
            loaded = load_queue(path)
            self.assertEqual(loaded["revision"], 1)
            stale = new_queue()
            with self.assertRaises(ValueError):
                save_queue(path, stale, expected_revision=0)
            self.assertFalse(path.with_name(path.name + ".tmp").exists())
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_queue(path)

    def test_save_reload_binds_registry_hash_to_final_revision_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            self.assertTrue(hasattr(task_queue_module, "queue_registry_hash"))
            self.assertEqual(save_queue(path, queue, expected_revision=0), 1)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            payload = {
                key: value
                for key, value in persisted.items()
                if key != "registry_hash" and not key.startswith("_")
            }
            self.assertEqual(persisted["registry_hash"], canonical_hash(payload))
            self.assertEqual(queue["registry_hash"], persisted["registry_hash"])
            self.assertEqual(load_queue(path), persisted)

            persisted["revision"] += 1
            path.write_text(json.dumps(persisted), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "registry_hash"):
                load_queue(path)

    def test_loaded_queue_refreshes_registry_hash_through_add_and_save(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            save_queue(path, queue)
            loaded = load_queue(path)
            previous_hash = loaded["registry_hash"]

            task = add(loaded, "7", "payload-7")
            self.assertNotEqual(loaded["registry_hash"], previous_hash)
            self.assertEqual(loaded["registry_hash"], queue_registry_hash(loaded))
            save_queue(path, loaded)
            reloaded = load_queue(path)
            self.assertEqual(reloaded["tasks"][0]["task_id"], task["task_id"])
            self.assertEqual(reloaded["registry_hash"], queue_registry_hash(reloaded))

    def test_loaded_queue_refreshes_hash_through_claim_complete_and_preserves_illegal_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            task = add(queue, "8", "payload-8")
            save_queue(path, queue)
            loaded = load_queue(path)
            before_illegal = copy.deepcopy(loaded)
            with self.assertRaises(ValueError):
                complete_task(
                    loaded, task["task_id"], "worker", "bad.png", "bad", NOW
                )
            self.assertEqual(loaded, before_illegal)

            claim_task(loaded, "worker", NOW)
            self.assertEqual(loaded["registry_hash"], queue_registry_hash(loaded))
            complete_task(
                loaded, task["task_id"], "worker", "good.png", "good", NOW
            )
            self.assertEqual(loaded["registry_hash"], queue_registry_hash(loaded))
            save_queue(path, loaded)
            reloaded = load_queue(path)
            self.assertEqual(reloaded["tasks"][0]["state"], "completed")
            self.assertEqual(reloaded["tasks"][0]["candidate_hash"], "good")

    def test_save_without_explicit_expected_revision_rejects_stale_queue(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            current = new_queue()
            self.assertEqual(save_queue(path, current), 1)
            stale = load_queue(path)
            self.assertEqual(save_queue(path, current), 2)

            with self.assertRaisesRegex(ValueError, "revision conflict"):
                save_queue(path, stale)

            self.assertEqual(stale["revision"], 1)
            self.assertEqual(load_queue(path)["revision"], 2)

    def test_cross_process_double_write_allows_only_one_revision_winner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            self.assertEqual(save_queue(path, queue), 1)
            context = get_context("spawn")
            ready = context.Queue()
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=concurrent_save_worker,
                    args=(str(path), ready, start, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            self.assertEqual([ready.get(timeout=15) for _ in processes], ["ready", "ready"])
            start.set()
            for process in processes:
                process.join(timeout=20)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            outcomes = [results.get(timeout=5) for _ in processes]
            self.assertEqual(sum(kind == "ok" for kind, _ in outcomes), 1)
            errors = [message for kind, message in outcomes if kind == "error"]
            self.assertEqual(len(errors), 1)
            self.assertIn("revision conflict", errors[0])
            self.assertEqual(load_queue(path)["revision"], 2)
            self.assertTrue(path.with_name(path.name + ".lock").exists())

    def test_load_and_save_reject_invalid_deep_queue_structures(self):
        base = new_queue()
        add(base, "1", "base")
        invalid_queues = []

        missing_field = copy.deepcopy(base)
        del missing_field["tasks"][0]["payload_hash"]
        invalid_queues.append(("missing task field", missing_field))

        leased_without_owner = copy.deepcopy(base)
        leased_without_owner["tasks"][0].update(
            {
                "state": "leased",
                "lease_owner": None,
                "lease_expires_at": (NOW + timedelta(seconds=10)).isoformat(),
            }
        )
        invalid_queues.append(("leased without owner", leased_without_owner))

        queued_with_lease = copy.deepcopy(base)
        queued_with_lease["tasks"][0].update(
            {
                "lease_owner": "stale-owner",
                "lease_expires_at": (NOW + timedelta(seconds=10)).isoformat(),
            }
        )
        invalid_queues.append(("queued with stale lease", queued_with_lease))

        completed_without_candidate = copy.deepcopy(base)
        completed_without_candidate["tasks"][0].update(
            {"state": "completed", "candidate": {"path": "", "hash": "sha"}}
        )
        invalid_queues.append(("completed without candidate", completed_without_candidate))

        failed_without_record = copy.deepcopy(base)
        failed_without_record["tasks"][0].update(
            {
                "state": "failed",
                "failure": {
                    "code": "artifact",
                    "record_id": "",
                    "cluster_id": "c1",
                    "prompt_reference_hash": "p1",
                    "timestamp": NOW.isoformat(),
                },
            }
        )
        invalid_queues.append(("failed without record id", failed_without_record))

        naive_timestamp = copy.deepcopy(base)
        naive_timestamp["tasks"][0]["created_at"] = "2026-07-13T08:00:00"
        invalid_queues.append(("naive timestamp", naive_timestamp))

        bad_reopen_time = copy.deepcopy(base)
        bad_reopen_time["metrics"]["lane_reopen_history"] = [
            {
                "lane": "c1::redraw",
                "coordinator_review_id": "review-1",
                "reopened_at": "2026-07-13T08:00:00",
            }
        ]
        invalid_queues.append(("naive reopen timestamp", bad_reopen_time))

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            for label, invalid in invalid_queues:
                with self.subTest(label=label, operation="load"):
                    path.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_queue(path)
                path.unlink()
                with self.subTest(label=label, operation="save"):
                    with self.assertRaises(ValueError):
                        save_queue(path, invalid)
            path.write_text(json.dumps([]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_queue(path)

    def test_deep_validation_rejects_forged_and_duplicate_task_ids(self):
        base = new_queue()
        first = add(base, "1", "one")
        second = add(base, "2", "two")
        forged = copy.deepcopy(base)
        forged["tasks"][0]["task_id"] = "f" * 64
        forged["tasks"][0]["idempotency_key"] = "f" * 64
        duplicated = copy.deepcopy(base)
        duplicated["tasks"][1]["task_id"] = first["task_id"]
        duplicated["tasks"][1]["idempotency_key"] = first["task_id"]

        with tempfile.TemporaryDirectory() as temp_dir:
            for label, invalid in (("forged", forged), ("duplicate", duplicated)):
                with self.subTest(label=label):
                    with self.assertRaises(ValueError):
                        save_queue(Path(temp_dir) / f"{label}.json", invalid)

        self.assertNotEqual(first["task_id"], second["task_id"])

    def test_idempotency_key_is_optional_but_must_match_when_present(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "queue.json"
            queue = new_queue()
            task = add(queue, "1", "one")
            del task["idempotency_key"]
            self.assertEqual(save_queue(path, queue), 1)
            self.assertNotIn("idempotency_key", load_queue(path)["tasks"][0])

    def test_state_matrix_rejects_cross_state_metadata(self):
        queued = new_queue()
        add(queued, "1", "queued")

        leased = new_queue()
        add(leased, "2", "leased")
        claim_task(leased, "worker", NOW)

        completed = new_queue()
        completed_task = add(completed, "3", "completed")
        claim_task(completed, "worker", NOW)
        complete_task(completed, completed_task["task_id"], "worker", "c.png", "sha", NOW)

        failed = new_queue()
        failed_task = add(failed, "4", "failed")
        claim_task(failed, "worker", NOW)
        fail_task(failed, failed_task["task_id"], "worker", "artifact", "fr-4", NOW)

        queued_with_leased_at = copy.deepcopy(queued)
        queued_with_leased_at["tasks"][0]["leased_at"] = NOW.isoformat()
        queued_with_leased_at["tasks"][0]["timestamps"]["leased_at"] = NOW.isoformat()

        queued_with_candidate = copy.deepcopy(queued)
        queued_with_candidate["tasks"][0]["candidate_path"] = "stale.png"
        queued_with_candidate["tasks"][0]["candidate_hash"] = "stale-sha"
        queued_with_candidate["tasks"][0]["candidate"] = {
            "path": "stale.png",
            "hash": "stale-sha",
        }

        leased_with_failure = copy.deepcopy(leased)
        leased_with_failure["tasks"][0]["failure_code"] = "stale"
        leased_with_failure["tasks"][0]["failure_record_id"] = "fr-stale"
        leased_with_failure["tasks"][0]["failure"] = {
            "code": "stale",
            "record_id": "fr-stale",
            "cluster_id": "c1",
            "prompt_reference_hash": "p1",
            "timestamp": NOW.isoformat(),
        }

        completed_with_failure = copy.deepcopy(completed)
        completed_with_failure["tasks"][0]["failed_at"] = NOW.isoformat()
        completed_with_failure["tasks"][0]["timestamps"]["failed_at"] = NOW.isoformat()
        completed_with_failure["tasks"][0]["failure_code"] = "stale"
        completed_with_failure["tasks"][0]["failure_record_id"] = "fr-stale"
        completed_with_failure["tasks"][0]["failure"] = {
            "code": "stale",
            "record_id": "fr-stale",
            "cluster_id": "c1",
            "prompt_reference_hash": "p1",
            "timestamp": NOW.isoformat(),
        }

        failed_with_candidate = copy.deepcopy(failed)
        failed_with_candidate["tasks"][0]["completed_at"] = NOW.isoformat()
        failed_with_candidate["tasks"][0]["timestamps"]["completed_at"] = NOW.isoformat()
        failed_with_candidate["tasks"][0]["candidate_path"] = "stale.png"
        failed_with_candidate["tasks"][0]["candidate_hash"] = "stale-sha"
        failed_with_candidate["tasks"][0]["candidate"] = {
            "path": "stale.png",
            "hash": "stale-sha",
        }

        mismatched_failure_time = copy.deepcopy(failed)
        mismatched_failure_time["tasks"][0]["failure"]["timestamp"] = (
            NOW + timedelta(seconds=1)
        ).isoformat()

        invalid = (
            queued_with_leased_at,
            queued_with_candidate,
            leased_with_failure,
            completed_with_failure,
            failed_with_candidate,
            mismatched_failure_time,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for index, queue in enumerate(invalid):
                with self.subTest(index=index):
                    with self.assertRaises(ValueError):
                        save_queue(Path(temp_dir) / f"invalid-{index}.json", queue)

    def test_metrics_and_cache_reject_invalid_counts_and_structures(self):
        invalid = []
        for value in (-1, "1", True):
            queue = new_queue()
            queue["metrics"]["cache_hits"] = value
            invalid.append(queue)

        negative_retry = new_queue()
        negative_retry["metrics"]["retry_counts_by_failure_code"] = {"artifact": -1}
        invalid.append(negative_retry)

        bad_prompt_count = new_queue()
        bad_prompt_count["metrics"]["prompt_failure_streaks"] = {"prompt": "3"}
        invalid.append(bad_prompt_count)

        bad_lane_count = new_queue()
        bad_lane_count["metrics"]["lane_failure_streaks"] = {
            "c1::redraw": {"failure_code": "artifact", "count": True}
        }
        invalid.append(bad_lane_count)

        bad_cache_entry = new_queue()
        bad_cache_entry["cache"] = {"key": {"hits": -1, "misses": 0}}
        invalid.append(bad_cache_entry)

        bad_cache_type = new_queue()
        bad_cache_type["cache"] = {"key": "not-an-object"}
        invalid.append(bad_cache_type)

        mismatched_cache_summary = new_queue()
        mismatched_cache_summary["cache"] = {"key": {"hits": 1, "misses": 0}}
        invalid.append(mismatched_cache_summary)

        with tempfile.TemporaryDirectory() as temp_dir:
            for index, queue in enumerate(invalid):
                with self.subTest(index=index):
                    with self.assertRaises(ValueError):
                        save_queue(Path(temp_dir) / f"bad-metrics-{index}.json", queue)


if __name__ == "__main__":
    unittest.main()
