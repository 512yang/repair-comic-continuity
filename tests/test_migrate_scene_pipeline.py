from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from migrate_scene_pipeline import (  # noqa: E402
    APPLY_CONFIRM_TOKEN,
    PROPOSAL_FILENAMES,
    _validate_bundle_documents,
    apply_project,
    create_migration_review,
    migrate_project,
    recover_interrupted_migration,
    validate_migration_bundle,
    validate_migration_review,
)
from pipeline_contracts import canonical_hash  # noqa: E402
from scene_clusters import cluster_size_controls  # noqa: E402
from validate_output import validate_project  # noqa: E402


FIXED_NOW = "2026-07-14T01:02:03+00:00"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return {
        item.relative_to(path).as_posix(): _sha(item)
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _accepted_review_file(proposal: Path, report: dict) -> Path:
    review = create_migration_review(
        report,
        reviewed_by="independent-reviewer",
        reviewed_at=FIXED_NOW,
        notes="test approval",
    )
    path = proposal / "migration_review.json"
    _write_json(path, review)
    return path


def _make_image(path: Path, colour: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 60), colour).save(path)


def _make_project(
    root: Path,
    count: int = 3,
    *,
    unresolved: set[int] | None = None,
    provisional: set[int] | None = None,
    actions: list[str] | None = None,
    single_scene: bool = False,
) -> Path:
    unresolved = unresolved or set()
    provisional = provisional or set()
    actions = actions or ["unchanged"] * count
    (root / "人物参考图").mkdir(parents=True)
    (root / "输入").mkdir()
    (root / "输出").mkdir()
    evidence = root / "evidence"
    evidence.mkdir()
    (root / "novel.txt").write_text("第一章\n测试小说正文。", encoding="utf-8")
    _make_image(root / "人物参考图" / "hero.png", (200, 30, 30))

    inventory_pages = []
    manifest_pages = []
    alignment_pages = []
    repair_pages = []
    for index in range(1, count + 1):
        source = root / "输入" / f"{index}.jpg"
        _make_image(source, (index % 255, 80, 120))
        output_name = f"{index:04d}.jpg"
        inventory_pages.append(
            {
                "index": index,
                "input_name": source.name,
                "input_sha256": _sha(source),
            }
        )
        manifest_pages.append(
            {
                "index": index,
                "input_name": source.name,
                "input_sha256": _sha(source),
                "output_name": output_name,
                "state": "inventoried",
            }
        )
        status = (
            "unresolved"
            if index in unresolved
            else "provisional"
            if index in provisional
            else "confirmed"
        )
        alignment_pages.append(
            {
                "index": index,
                "output_name": output_name,
                "status": status,
                "chapter": "第一章",
                "location": (
                    "山门"
                    if single_scene or index <= max(1, count // 2)
                    else "大殿"
                ),
                "story_time": "清晨",
                "scene_summary": "入门" if index <= max(1, count // 2) else "议事",
                "start_offset": index,
                "end_offset": index + 1,
            }
        )
        repair_pages.append(
            {
                "index": index,
                "output_name": output_name,
                "action": actions[index - 1],
            }
        )

    novel_hash = _sha(root / "novel.txt")
    _write_json(
        evidence / "inventory.json",
        {
            "input_count": count,
            "novel": {"path": str(root / "novel.txt"), "sha256": novel_hash},
            "pages": inventory_pages,
        },
    )
    _write_json(
        evidence / "comic_run_manifest.json",
        {"expected_count": count, "novel_sha256": novel_hash, "pages": manifest_pages},
    )
    _write_json(
        evidence / "novel_alignment.json",
        {"novel_sha256": novel_hash, "pages": alignment_pages},
    )
    _write_json(evidence / "repair_log.json", {"pages": repair_pages})
    _write_json(
        evidence / "continuity_bible.json",
        {"status": "inventoried", "locks": []},
    )
    _write_json(evidence / "ocr_snapshot.json", {"status": "snapshot", "pages": []})
    return root


class MigrationDryRunTests(unittest.TestCase):
    def test_output_audit_counts_supported_extensions_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            _make_image(root / "输出" / "a" / "1.jpeg", (1, 2, 3))
            _make_image(root / "输出" / "b" / "2.png", (4, 5, 6))
            _make_image(root / "输出" / "b" / "3.webp", (7, 8, 9))

            report = migrate_project(root, now=FIXED_NOW)["report"]

            self.assertEqual(3, report["output_count_before"])
            self.assertEqual(3, report["output_count_after"])
            self.assertEqual(
                {"输出/a/1.jpeg", "输出/b/2.png", "输出/b/3.webp"},
                {record["path"] for record in report["output_snapshot_before"]},
            )

    def test_recursive_same_basename_pages_keep_unique_relative_identities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", count=2, single_scene=True)
            (root / "输入" / "a").mkdir()
            (root / "输入" / "b").mkdir()
            (root / "输入" / "1.jpg").replace(root / "输入" / "a" / "1.jpg")
            (root / "输入" / "2.jpg").unlink()
            _make_image(root / "输入" / "b" / "1.jpg", (2, 80, 120))

            result = migrate_project(root, now=FIXED_NOW)
            manifest = result["artifacts"]["comic_run_manifest.json"]
            tasks = result["artifacts"]["task_queue.json"]["tasks"]

            self.assertEqual(
                ["a/1.jpg", "b/1.jpg"],
                [page["output_name"] for page in manifest["pages"]],
            )
            self.assertEqual(2, len({task["page_id"] for task in tasks}))
            self.assertEqual([], _validate_bundle_documents(result["artifacts"]))

    def test_bundle_validation_reports_unsafe_relative_paths_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            artifacts = migrate_project(root, now=FIXED_NOW)["artifacts"]
            artifacts["comic_run_manifest.json"]["pages"][0]["input_name"] = "../escape.jpg"
            artifacts["comic_run_manifest.json"]["pages"][0]["output_name"] = "../escape.jpg"

            errors = _validate_bundle_documents(artifacts)

            self.assertTrue(
                any("manifest input/output bijection invalid" in error for error in errors),
                errors,
            )

    def test_bundle_schema_error_uses_current_neutral_wording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            artifacts = migrate_project(root, now=FIXED_NOW)["artifacts"]
            artifacts["comic_run_manifest.json"]["schema_version"] = "wrong"

            errors = _validate_bundle_documents(artifacts)

            self.assertTrue(any("schema declaration invalid" in error for error in errors))
            self.assertFalse(any("v3 schema" in error for error in errors), errors)

    def test_dry_run_builds_one_queued_task_per_page_without_standard_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(
                Path(tmp) / "project",
                actions=["unchanged", "text_only", "full_page_redraw"],
                single_scene=True,
            )
            evidence = root / "evidence"
            before_sources = _tree_hashes(root / "输入")
            before_output = _tree_hashes(root / "输出")

            result = migrate_project(root, now=FIXED_NOW)

            self.assertTrue(result["report"]["proposal_only"])
            self.assertEqual(result["report"]["input_count"], 3)
            self.assertEqual(result["report"]["output_count_before"], 0)
            self.assertEqual(result["report"]["input_count_before"], 3)
            self.assertEqual(result["report"]["input_count_after"], 3)
            self.assertEqual(result["report"]["output_count_after"], 0)
            self.assertEqual(
                result["report"]["source_hashes_before"],
                result["report"]["source_hashes_after"],
            )
            self.assertEqual(
                result["report"]["output_snapshot_before"],
                result["report"]["output_snapshot_after"],
            )
            self.assertEqual(result["report"]["images_touched"], 0)
            self.assertTrue(result["report"]["output_unchanged"])
            self.assertTrue(result["report"]["exact_bijection_planned"])
            self.assertFalse(result["report"]["ready_for_generation"])
            tasks = result["artifacts"]["task_queue.json"]["tasks"]
            self.assertEqual(len(tasks), 3)
            self.assertEqual({task["state"] for task in tasks}, {"queued"})
            self.assertEqual({task["completed_by"] for task in tasks}, {None})
            self.assertEqual(
                [task["task_type"] for task in tasks],
                ["continuity_check", "text_repair", "full_page_redraw"],
            )
            manifest_pages = result["artifacts"]["comic_run_manifest.json"]["pages"]
            repair_pages = result["artifacts"]["repair_log.json"]["pages"]
            expected_classes = ["unchanged", "text_only", "full_page_redraw"]
            self.assertEqual([page["page_class"] for page in manifest_pages], expected_classes)
            self.assertEqual([page["page_class"] for page in repair_pages], expected_classes)
            self.assertEqual(
                [page["action"] for page in repair_pages],
                ["unchanged", "text_only", "full_page_regeneration"],
            )
            self.assertEqual(_validate_bundle_documents(result["artifacts"]), [])
            for filename in PROPOSAL_FILENAMES:
                if filename not in {"comic_run_manifest.json", "repair_log.json"}:
                    self.assertFalse((evidence / filename).exists())
            self.assertEqual(_tree_hashes(root / "输入"), before_sources)
            self.assertEqual(_tree_hashes(root / "输出"), before_output)

    def test_bundle_rejects_page_class_or_action_mapping_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(
                Path(tmp) / "project",
                actions=["unchanged", "text_only", "full_page_redraw"],
                single_scene=True,
            )
            artifacts = migrate_project(root, now=FIXED_NOW)["artifacts"]

            artifacts["repair_log.json"]["pages"][2]["action"] = "full_page_redraw"
            errors = _validate_bundle_documents(artifacts)
            self.assertTrue(any("page class/action" in error for error in errors), errors)

            artifacts["repair_log.json"]["pages"][2]["action"] = "full_page_regeneration"
            artifacts["comic_run_manifest.json"]["pages"][2]["page_class"] = "text_only"
            errors = _validate_bundle_documents(artifacts)
            self.assertTrue(any("page class" in error for error in errors), errors)

    def test_nonboundary_undersized_clusters_are_blocked_and_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", count=13)

            with mock.patch(
                "migrate_scene_pipeline.cluster_size_controls",
                wraps=cluster_size_controls,
            ) as shared_controls:
                artifacts = migrate_project(root, now=FIXED_NOW)["artifacts"]
            self.assertEqual(shared_controls.call_count, 2)
            clusters = artifacts["scene_clusters.json"]["clusters"]
            tasks = artifacts["task_queue.json"]["tasks"]
            manifest_pages = artifacts["comic_run_manifest.json"]["pages"]
            repair_pages = artifacts["repair_log.json"]["pages"]

            self.assertEqual([len(c["member_pages"]) for c in clusters], [6, 7])
            self.assertTrue(all(c["undersized"] for c in clusters))
            self.assertTrue(all(c["blocked"] for c in clusters))
            self.assertTrue(all(c["boundary_exception"] is False for c in clusters))
            self.assertTrue(all(c["undersized_reason"] == "scene_fragment_below_min" for c in clusters))
            self.assertEqual({task["task_type"] for task in tasks}, {"evidence_resolution"})
            self.assertEqual({page["page_class"] for page in manifest_pages}, {"evidence_blocked"})
            self.assertEqual({page["action"] for page in repair_pages}, {"evidence_blocked"})
            self.assertEqual(_validate_bundle_documents(artifacts), [])

            clusters[0]["blocked"] = False
            clusters[0]["blocker_codes"] = []
            scene_registry = artifacts["scene_clusters.json"]
            scene_registry["registry_hash"] = canonical_hash(
                {key: value for key, value in scene_registry.items() if key != "registry_hash"}
            )
            errors = _validate_bundle_documents(artifacts)
            self.assertTrue(any("undersized" in error for error in errors), errors)

    def test_project_below_min_single_cluster_is_a_boundary_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", count=3, single_scene=True)

            artifacts = migrate_project(root, now=FIXED_NOW)["artifacts"]
            cluster = artifacts["scene_clusters.json"]["clusters"][0]

            self.assertEqual(cluster["member_pages"], ["1.jpg", "2.jpg", "3.jpg"])
            self.assertTrue(cluster["undersized"])
            self.assertTrue(cluster["boundary_exception"])
            self.assertEqual(cluster["undersized_reason"], "project_total_below_min")
            self.assertFalse(cluster["blocked"])

    def test_artifact_dir_writes_only_proposals_and_preserves_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            artifact_dir = root / "evidence" / "skill_validation_test"
            evidence_before = _tree_hashes(root / "evidence")
            input_before = _tree_hashes(root / "输入")
            output_before = _tree_hashes(root / "输出")

            migrate_project(root, artifact_dir=artifact_dir, now=FIXED_NOW)

            self.assertEqual(
                {path.name for path in artifact_dir.iterdir()}, set(PROPOSAL_FILENAMES)
            )
            for path in artifact_dir.iterdir():
                self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["proposal_only"])
            self.assertEqual(_tree_hashes(root / "输入"), input_before)
            self.assertEqual(_tree_hashes(root / "输出"), output_before)
            self.assertEqual(
                {key: value for key, value in _tree_hashes(root / "evidence").items() if not key.startswith("skill_validation_test/")},
                evidence_before,
            )
            saved_report = json.loads(
                (artifact_dir / "pipeline_migration_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                saved_report["source_hashes_before"],
                saved_report["source_hashes_after"],
            )
            self.assertEqual(
                saved_report["output_snapshot_before"],
                saved_report["output_snapshot_after"],
            )
            self.assertEqual(saved_report["input_count_before"], 3)
            self.assertEqual(saved_report["input_count_after"], 3)
            self.assertEqual(saved_report["output_count_before"], 0)
            self.assertEqual(saved_report["output_count_after"], 0)

    def test_unresolved_alignment_stays_unresolved_and_creates_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", unresolved={2})

            result = migrate_project(root, now=FIXED_NOW)

            clusters = result["artifacts"]["scene_clusters.json"]["clusters"]
            blocked = [cluster for cluster in clusters if "2.jpg" in cluster["member_pages"]]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0]["alignment_state"], "unresolved")
            self.assertTrue(blocked[0]["blocked"])
            self.assertTrue(blocked[0]["undersized"])
            self.assertIn("2", result["report"]["unresolved_alignment_pages"])
            self.assertTrue(result["report"]["blockers"])
            task = next(
                task
                for task in result["artifacts"]["task_queue.json"]["tasks"]
                if task["page_id"] == "2"
            )
            self.assertEqual(task["task_type"], "evidence_resolution")

    def test_provisional_alignment_is_visible_but_never_migration_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", provisional={2})
            proposal = root / "evidence" / "dry_run_provisional"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            report = result["report"]
            blocker = next(
                item
                for item in report["blockers"]
                if item["code"] == "UNCONFIRMED_ALIGNMENT_PAGES"
            )
            self.assertIn("2", blocker["pages"])
            self.assertFalse(report["ready_for_migration"])
            cluster = next(
                item
                for item in result["artifacts"]["scene_clusters.json"]["clusters"]
                if "2.jpg" in item["member_pages"]
            )
            self.assertEqual(cluster["alignment_state"], "provisional")
            before = _tree_hashes(root / "evidence")
            review = create_migration_review(
                proposal,
                reviewed_by="independent-reviewer",
                reviewed_at=FIXED_NOW,
                notes="provisional gate check",
            )

            with self.assertRaisesRegex(ValueError, "blocker"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=report["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=proposal / "migration_review.json",
                )

            after = _tree_hashes(root / "evidence")
            self.assertEqual(
                {
                    key: value
                    for key, value in after.items()
                    if key != "dry_run_provisional/migration_review.json"
                },
                before,
            )
            self.assertEqual(review["decision"], "accepted")

    def test_reference_packs_are_content_addressed_and_bound_to_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", count=13)

            result = migrate_project(root, now=FIXED_NOW)

            cluster_doc = result["artifacts"]["scene_clusters.json"]
            pack_doc = result["artifacts"]["style_reference_packs.json"]
            packs = {
                pack["reference_pack_id"]: pack
                for pack in pack_doc["reference_packs"]
            }
            self.assertEqual(len(result["artifacts"]["task_queue.json"]["tasks"]), 13)
            for cluster in cluster_doc["clusters"]:
                self.assertEqual(cluster["reference_pack_state"], "bound")
                pack = packs[cluster["reference_pack_id"]]
                self.assertEqual(pack["cluster_id"], cluster["cluster_id"])
                for reference in pack["references"]:
                    self.assertRegex(reference["sha256"], r"^[0-9a-f]{64}$")
                    if reference["path"].startswith("人物参考图/"):
                        self.assertEqual(reference["role"], "identity_only")
                    else:
                        self.assertIn(reference["role"], {"primary_style", "adjacent_style"})
                cluster_tasks = [
                    task
                    for task in result["artifacts"]["task_queue.json"]["tasks"]
                    if task["cluster_id"] == cluster["cluster_id"]
                ]
                self.assertTrue(cluster_tasks)
                self.assertEqual(
                    {task["prompt_reference_hash"] for task in cluster_tasks},
                    {pack["reference_binding_hash"]},
                )

    def test_fixed_time_makes_thirteen_page_proposal_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", count=13)

            first = migrate_project(root, now=FIXED_NOW)
            second = migrate_project(root, now=FIXED_NOW)

            self.assertEqual(first, second)
            self.assertEqual(first["report"]["task_count"], 13)
            self.assertEqual(len(first["report"]["planned_outputs"]), 13)

    def test_proposals_have_registry_hashes_exact_output_members_and_safe_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project", count=13)

            result = migrate_project(root, now=FIXED_NOW)
            artifacts = result["artifacts"]

            self.assertEqual(
                set(PROPOSAL_FILENAMES),
                {
                    "scene_clusters.json",
                    "style_reference_packs.json",
                    "task_queue.json",
                    "failure_learning.json",
                    "scene_cluster_qa.json",
                    "comic_run_manifest.json",
                    "repair_log.json",
                    "pipeline_metrics.json",
                    "pipeline_migration_report.json",
                },
            )
            clusters = artifacts["scene_clusters.json"]["clusters"]
            members = [member for cluster in clusters for member in cluster["member_pages"]]
            self.assertEqual(members, [f"{index}.jpg" for index in range(1, 14)])
            self.assertTrue(all("source_pages" in cluster for cluster in clusters))
            for filename in (
                "scene_clusters.json",
                "style_reference_packs.json",
                "task_queue.json",
                "failure_learning.json",
                "scene_cluster_qa.json",
            ):
                document = artifacts[filename]
                payload = {key: value for key, value in document.items() if key != "registry_hash"}
                self.assertEqual(document["registry_hash"], canonical_hash(payload))
            packs = artifacts["style_reference_packs.json"]
            self.assertEqual(
                set(("reference_packs", "stable_pages", "approved_hashes"))
                - set(packs),
                set(),
            )
            self.assertNotIn("packs", packs)
            qa_rows = artifacts["scene_cluster_qa.json"]["clusters"]
            self.assertEqual(len(qa_rows), len(clusters))
            self.assertEqual({row["status"] for row in qa_rows}, {"pending"})
            manifest = artifacts["comic_run_manifest.json"]
            repair = artifacts["repair_log.json"]
            self.assertEqual(manifest["pipeline_mode"], "continuity_v4")
            self.assertEqual(repair["schema_version"], "4.0")
            self.assertEqual(len(manifest["evidence_files"]), 15)
            for run_row, repair_row in zip(manifest["pages"], repair["pages"]):
                for field in ("cluster_id", "reference_pack_id", "task_id"):
                    self.assertTrue(run_row[field])
                    self.assertEqual(run_row[field], repair_row[field])
                self.assertEqual(run_row["failure_rule_ids"], [])
                self.assertIsNone(run_row["generated_by"])
                self.assertIsNone(run_row["reviewed_by"])

    def test_migration_id_binds_source_output_image_and_proposal_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            proposal = root / "evidence" / "dry_run_contract"

            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            report = result["report"]

            self.assertRegex(report["migration_id"], r"^[0-9a-f]{64}$")
            self.assertTrue(report["ready_for_migration"])
            self.assertEqual(report["blockers"], [])
            self.assertEqual(report["locked_sources_before"], report["locked_sources_after"])
            self.assertTrue(report["locked_sources_unchanged"])
            self.assertEqual(report["image_snapshot_before"], report["image_snapshot_after"])
            self.assertEqual(report["images_touched"], 0)
            saved = json.loads(
                (proposal / "pipeline_migration_report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["migration_id"], report["migration_id"])

    def test_alignment_exact_name_wins_index_fallback_and_scene_id_is_only_fourth_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            alignment_path = root / "evidence" / "novel_alignment.json"
            document = json.loads(alignment_path.read_text(encoding="utf-8"))
            document["pages"][0].update(
                index=99,
                output_name="1.jpg",
                scene_id="exact-scene",
                scene_summary="must-not-be-scene-key",
            )
            document["pages"].append(
                {
                    "index": 1,
                    "output_name": "9999.jpg",
                    "status": "confirmed",
                    "chapter": "wrong",
                    "location": "wrong",
                    "story_time": "wrong",
                    "scene_id": "fallback-scene",
                }
            )
            _write_json(alignment_path, document)

            result = migrate_project(root, now=FIXED_NOW)

            first_cluster = next(
                cluster
                for cluster in result["artifacts"]["scene_clusters.json"]["clusters"]
                if "1.jpg" in cluster["member_pages"]
            )
            self.assertEqual(first_cluster["scene_key"][3], "exact-scene")
            self.assertNotIn("must-not-be-scene-key", first_cluster["scene_key"])

    def test_alignment_duplicate_exact_name_or_index_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "exact")
            path = root / "evidence" / "novel_alignment.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pages"].append(dict(document["pages"][0]))
            _write_json(path, document)
            with self.assertRaisesRegex(ValueError, "duplicate.*name"):
                migrate_project(root, now=FIXED_NOW)

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "index")
            path = root / "evidence" / "novel_alignment.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            duplicate = dict(document["pages"][0])
            duplicate["output_name"] = "9999.jpg"
            document["pages"].append(duplicate)
            _write_json(path, document)
            with self.assertRaisesRegex(ValueError, "duplicate.*index"):
                migrate_project(root, now=FIXED_NOW)


class MigrationSafetyTests(unittest.TestCase):
    def test_independent_migration_review_is_content_addressed_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            proposal = root / "evidence" / "dry_run_review"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            report = result["report"]
            self.assertTrue(report["prepared_by"].strip())
            review = create_migration_review(
                proposal,
                reviewed_by="independent-reviewer",
                reviewed_at="2026-07-14T09:02:03+08:00",
                notes="independent approval",
            )
            self.assertNotEqual(review["prepared_by"], review["reviewed_by"])
            self.assertRegex(review["review_id"], r"^[0-9a-f]{64}$")
            validation = validate_migration_review(
                proposal / "migration_review.json", report
            )
            self.assertTrue(validation["ok"], validation["errors"])

            tampered = dict(review)
            tampered["notes"] = "tampered"
            tampered_path = proposal / "tampered_review.json"
            _write_json(tampered_path, tampered)
            validation = validate_migration_review(tampered_path, report)
            self.assertFalse(validation["ok"])
            self.assertTrue(any("review_id" in item for item in validation["errors"]))

            with self.assertRaisesRegex(ValueError, "independent"):
                create_migration_review(
                    report,
                    reviewed_by=report["prepared_by"],
                    reviewed_at=FIXED_NOW,
                    notes="self review",
                )
            with self.assertRaisesRegex(ValueError, "timezone"):
                create_migration_review(
                    report,
                    reviewed_by="independent-reviewer",
                    reviewed_at="2026-07-14T01:02:03",
                    notes="naive time",
                )
            with self.assertRaisesRegex(ValueError, "decision"):
                create_migration_review(
                    report,
                    reviewed_by="independent-reviewer",
                    reviewed_at=FIXED_NOW,
                    decision="pending",
                    notes="invalid decision",
                )

    def test_apply_requires_valid_accepted_independent_review_before_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            evidence = root / "evidence"
            proposal = evidence / "dry_run_review_gate"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            report = result["report"]
            before = _tree_hashes(evidence)

            with self.assertRaisesRegex(ValueError, "review"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=report["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )
            self.assertEqual(_tree_hashes(evidence), before)

            rejected = create_migration_review(
                report,
                reviewed_by="independent-reviewer",
                reviewed_at=FIXED_NOW,
                decision="rejected",
                notes="not approved",
            )
            rejected_path = proposal / "migration_review.json"
            _write_json(rejected_path, rejected)
            with self.assertRaisesRegex(ValueError, "accepted"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=report["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=rejected_path,
                )

            same_person = dict(rejected)
            same_person["decision"] = "accepted"
            same_person["reviewed_by"] = same_person["prepared_by"]
            same_person.pop("review_id")
            same_person["review_id"] = canonical_hash(same_person)
            _write_json(rejected_path, same_person)
            with self.assertRaisesRegex(ValueError, "independent"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=report["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=rejected_path,
                )

            accepted = create_migration_review(
                report,
                reviewed_by="independent-reviewer",
                reviewed_at=FIXED_NOW,
                notes="approved",
            )
            accepted["notes"] = "tampered after signing"
            _write_json(rejected_path, accepted)
            with self.assertRaisesRegex(ValueError, "review_id"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=report["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=rejected_path,
                )
            self.assertFalse((evidence / "scene_clusters.json").exists())

    def test_report_integrity_rejects_any_audit_field_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "proposal")
            proposal = root / "evidence" / "dry_run_report_integrity"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            report_path = proposal / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertRegex(report["report_integrity_hash"], r"^[0-9a-f]{64}$")
            report["input_count_after"] += 1
            _write_json(report_path, report)

            with self.assertRaisesRegex(ValueError, "report integrity"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )
            self.assertFalse((root / "evidence" / "scene_clusters.json").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "applied")
            proposal = root / "evidence" / "dry_run_applied_integrity"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            apply_project(
                root,
                proposal_dir=proposal,
                expected_migration_id=result["report"]["migration_id"],
                confirm_token=APPLY_CONFIRM_TOKEN,
                review_file=review_file,
            )
            report_path = root / "evidence" / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertRegex(report["report_integrity_hash"], r"^[0-9a-f]{64}$")
            report["output_count_after"] += 1
            _write_json(report_path, report)

            validation = validate_migration_bundle(root, root / "evidence")
            self.assertFalse(validation["ok"])
            self.assertTrue(
                any("report integrity" in error for error in validation["errors"])
            )

    def test_keyboard_interrupt_journal_recovers_every_replace_boundary(self) -> None:
        for boundary in range(1, 10):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                root = _make_project(Path(tmp) / "project")
                evidence = root / "evidence"
                proposal = evidence / f"dry_run_interrupt_{boundary}"
                result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
                migration_id = result["report"]["migration_id"]
                review_file = _accepted_review_file(proposal, result["report"])
                before = {
                    filename: (
                        _sha(evidence / filename)
                        if (evidence / filename).is_file()
                        else None
                    )
                    for filename in PROPOSAL_FILENAMES
                }

                with self.assertRaises(KeyboardInterrupt):
                    apply_project(
                        root,
                        proposal_dir=proposal,
                        expected_migration_id=migration_id,
                        confirm_token=APPLY_CONFIRM_TOKEN,
                        review_file=review_file,
                        interrupt_after_replacements=boundary,
                    )

                journal = evidence / ".scene_pipeline_migration_journal.json"
                self.assertTrue(journal.is_file())
                recovered = recover_interrupted_migration(root)
                self.assertTrue(recovered["recovered"])
                self.assertFalse(journal.exists())
                self.assertEqual(list(evidence.glob(".migration-staging-*")), [])
                for filename, digest in before.items():
                    target = evidence / filename
                    if digest is None:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(_sha(target), digest)

                applied = apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=migration_id,
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )
                self.assertFalse(applied["report"]["proposal_only"])

    def test_intent_journal_recovers_interrupts_before_transaction_is_prepared(self) -> None:
        scenarios = (
            {"interrupt_after_backup_dir_creation": True},
            {"interrupt_after_first_backup_copy": True},
            {"interrupt_after_stage_dir_creation": True},
        )
        for options in scenarios:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                root = _make_project(Path(tmp) / "project")
                evidence = root / "evidence"
                proposal = evidence / "dry_run_intent_interrupt"
                result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
                review_file = _accepted_review_file(proposal, result["report"])
                before = {
                    filename: _sha(evidence / filename)
                    if (evidence / filename).is_file()
                    else None
                    for filename in PROPOSAL_FILENAMES
                }

                with self.assertRaises(KeyboardInterrupt):
                    apply_project(
                        root,
                        proposal_dir=proposal,
                        expected_migration_id=result["report"]["migration_id"],
                        confirm_token=APPLY_CONFIRM_TOKEN,
                        review_file=review_file,
                        **options,
                    )

                journal_path = evidence / ".scene_pipeline_migration_journal.json"
                self.assertTrue(journal_path.is_file())
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["state"], "intent")
                recovered = recover_interrupted_migration(root)
                self.assertEqual(recovered["state"], "intent_cleaned")
                self.assertFalse(journal_path.exists())
                self.assertEqual(list(evidence.glob(".migration-staging-*")), [])
                self.assertFalse(
                    (evidence / "migration_backups" / result["report"]["migration_id"]).exists()
                )
                for filename, digest in before.items():
                    target = evidence / filename
                    if digest is None:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(_sha(target), digest)

                applied = apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )
                self.assertFalse(applied["report"]["proposal_only"])

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "changed-target")
            evidence = root / "evidence"
            proposal = evidence / "dry_run_intent_changed_target"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            with self.assertRaises(KeyboardInterrupt):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                    interrupt_after_backup_dir_creation=True,
                )
            manifest = evidence / "comic_run_manifest.json"
            manifest.write_bytes(manifest.read_bytes() + b"\n")

            with self.assertRaisesRegex(ValueError, "intent targets changed"):
                recover_interrupted_migration(root)

            self.assertTrue(
                (evidence / ".scene_pipeline_migration_journal.json").is_file()
            )
            self.assertTrue(
                (evidence / "migration_backups" / result["report"]["migration_id"]).is_dir()
            )

    def test_interrupt_during_journal_progress_update_recovers_from_stale_commit_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            evidence = root / "evidence"
            proposal = evidence / "dry_run_journal_interrupt"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            before = {
                filename: _sha(evidence / filename)
                if (evidence / filename).is_file()
                else None
                for filename in PROPOSAL_FILENAMES
            }

            with self.assertRaises(KeyboardInterrupt):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                    interrupt_during_journal_update_at=3,
                )

            recovered = recover_interrupted_migration(root)
            self.assertTrue(recovered["recovered"])
            for filename, digest in before.items():
                target = evidence / filename
                if digest is None:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(_sha(target), digest)

    def test_journal_progress_write_failure_rolls_back_from_persistent_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            evidence = root / "evidence"
            proposal = evidence / "dry_run_journal_write_failure"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            before = {
                filename: _sha(evidence / filename)
                if (evidence / filename).is_file()
                else None
                for filename in PROPOSAL_FILENAMES
            }
            import migrate_scene_pipeline as migration_module

            original_write = migration_module._write_migration_journal
            calls = 0

            def fail_third_write(path: Path, journal: dict) -> None:
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("injected journal update failure")
                original_write(path, journal)

            with mock.patch.object(
                migration_module,
                "_write_migration_journal",
                side_effect=fail_third_write,
            ):
                with self.assertRaisesRegex(OSError, "journal update failure"):
                    apply_project(
                        root,
                        proposal_dir=proposal,
                        expected_migration_id=result["report"]["migration_id"],
                        confirm_token=APPLY_CONFIRM_TOKEN,
                        review_file=review_file,
                    )

            self.assertFalse((evidence / ".scene_pipeline_migration_journal.json").exists())
            for filename, digest in before.items():
                target = evidence / filename
                if digest is None:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual(_sha(target), digest)

    def test_untrusted_recovery_journal_refuses_without_touching_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            evidence = root / "evidence"
            before = _tree_hashes(evidence)
            _write_json(
                evidence / ".scene_pipeline_migration_journal.json",
                {
                    "state": "committing",
                    "migration_id": "0" * 64,
                    "stage_dir": str(root / "输出"),
                    "backup_dir": str(root / "outside"),
                    "targets": [],
                    "committed": [],
                },
            )

            with self.assertRaisesRegex(ValueError, "journal"):
                recover_interrupted_migration(root)

            after = _tree_hashes(evidence)
            self.assertEqual(
                {key: value for key, value in after.items() if not key.startswith(".scene_pipeline")},
                before,
            )

    @staticmethod
    def _attacker_migration_id(report: dict) -> str:
        return canonical_hash(
            {
                "schema_version": report["schema_version"],
                "locked_sources": report["locked_sources_before"],
                "output_snapshot": report["output_snapshot_before"],
                "image_snapshot": report["image_snapshot_before"],
                "proposal_hashes": report["proposal_hashes"],
                "blockers": report["blockers"],
                "ready_for_migration": report["ready_for_migration"],
                "exact_bijection_planned": report["exact_bijection_planned"],
                "confirmed_report_digest": report["confirmed_report_digest"],
            }
        )

    @staticmethod
    def _reseal_report(report: dict) -> None:
        report.pop("report_integrity_hash", None)
        report["report_integrity_hash"] = canonical_hash(report)

    def test_confirmed_report_digest_binds_audit_fields_even_after_resealing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "reseal-only")
            proposal = root / "evidence" / "dry_run_reseal_only"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            report_path = proposal / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["input_count_after"] += 1
            self._reseal_report(report)
            _write_json(report_path, report)

            with self.assertRaisesRegex(ValueError, "confirmed report digest"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "reseal-and-redigest")
            proposal = root / "evidence" / "dry_run_reseal_and_redigest"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            old_id = result["report"]["migration_id"]
            report_path = proposal / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["output_count_after"] += 1
            body = {
                key: value
                for key, value in report.items()
                if key not in {
                    "migration_id",
                    "report_integrity_hash",
                    "confirmed_report_digest",
                }
            }
            report["confirmed_report_digest"] = canonical_hash(body)
            report["migration_id"] = self._attacker_migration_id(report)
            self._reseal_report(report)
            _write_json(report_path, report)

            with self.assertRaisesRegex(ValueError, "expected migration_id"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=old_id,
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )

    def test_blocker_report_gate_tampering_cannot_bypass_derived_apply_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "keep-id", unresolved={2})
            proposal = root / "evidence" / "dry_run_attack_keep_id"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            migration_id = result["report"]["migration_id"]
            review_file = _accepted_review_file(proposal, result["report"])
            report_path = proposal / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["blockers"] = []
            report["ready_for_migration"] = True
            _write_json(report_path, report)

            with self.assertRaisesRegex(ValueError, "report integrity"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=migration_id,
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )
            self.assertFalse((root / "evidence" / "scene_clusters.json").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "new-id", unresolved={2})
            proposal = root / "evidence" / "dry_run_attack_new_id"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            original_id = result["report"]["migration_id"]
            report_path = proposal / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["blockers"] = []
            report["ready_for_migration"] = True
            report["migration_id"] = self._attacker_migration_id(report)
            _write_json(report_path, report)

            with self.assertRaisesRegex(ValueError, "report integrity"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=original_id,
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )
            self.assertFalse((root / "evidence" / "scene_clusters.json").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "cluster", unresolved={2})
            proposal = root / "evidence" / "dry_run_attack_cluster"
            migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            scene_path = proposal / "scene_clusters.json"
            scene = json.loads(scene_path.read_text(encoding="utf-8"))
            for cluster in scene["clusters"]:
                cluster["blocked"] = False
                cluster["blocker_codes"] = []
                if cluster["alignment_state"] == "unresolved":
                    cluster["alignment_state"] = "confirmed"
            scene.pop("registry_hash")
            scene["registry_hash"] = canonical_hash(scene)
            _write_json(scene_path, scene)
            report_path = proposal / "pipeline_migration_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["proposal_hashes"]["scene_clusters.json"] = canonical_hash(scene)
            report["blockers"] = []
            report["ready_for_migration"] = True
            report["migration_id"] = self._attacker_migration_id(report)
            _write_json(report_path, report)

            with self.assertRaisesRegex(ValueError, "report integrity"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=report["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )
            self.assertFalse((root / "evidence" / "scene_clusters.json").exists())

    def test_utf16_windows_evidence_is_discovered_without_false_corruption_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            inventory = root / "evidence" / "inventory.json"
            payload = json.loads(inventory.read_text(encoding="utf-8"))
            inventory.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-16",
            )

            result = migrate_project(root, now=FIXED_NOW)

            blocker_paths = {
                item.get("path")
                for item in result["report"]["blockers"]
                if item.get("code") == "EVIDENCE_JSON_UNREADABLE"
            }
            self.assertNotIn("evidence/inventory.json", blocker_paths)

    def test_known_rejected_candidates_are_observed_never_effective(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            _make_image(root / "evidence" / "page_candidates" / "2_repaired.jpg", (0, 0, 0))
            _make_image(
                root / "evidence" / "full_page_redraw" / "3" / "candidate_rejected.png",
                (0, 0, 0),
            )

            result = migrate_project(root, now=FIXED_NOW)

            store = result["artifacts"]["failure_learning.json"]
            self.assertEqual(len(store["failures"]), 2)
            self.assertEqual({item["status"] for item in store["failures"]}, {"observed"})
            self.assertEqual({item["effective"] for item in store["failures"]}, {False})
            self.assertEqual(store["rules"], [])

    def test_two_stage_apply_requires_untampered_ready_proposal_and_exact_migration_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            proposal = root / "evidence" / "dry_run_apply"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            migration_id = result["report"]["migration_id"]
            review_file = _accepted_review_file(proposal, result["report"])
            with self.assertRaisesRegex(ValueError, "confirm token"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=migration_id,
                    confirm_token="wrong",
                    now=FIXED_NOW,
                )
            with self.assertRaisesRegex(ValueError, "migration_id"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id="0" * 64,
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    now=FIXED_NOW,
                )

            applied = apply_project(
                root,
                proposal_dir=proposal,
                expected_migration_id=migration_id,
                confirm_token=APPLY_CONFIRM_TOKEN,
                review_file=review_file,
                now=FIXED_NOW,
            )

            self.assertFalse(applied["report"]["proposal_only"])
            self.assertEqual(applied["report"]["source_migration_id"], migration_id)
            self.assertNotEqual(
                applied["report"]["applied_hashes"],
                result["report"]["proposal_hashes"],
            )
            self.assertTrue(validate_migration_bundle(root, root / "evidence")["ok"])
            backup = root / "evidence" / "migration_backups" / migration_id
            self.assertTrue((backup / "comic_run_manifest.json").is_file())
            self.assertTrue((backup / "repair_log.json").is_file())
            self.assertTrue((backup / "backup_manifest.json").is_file())
            with self.assertRaisesRegex(ValueError, "already exists"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=migration_id,
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )

            validation = validate_project(root, root / "evidence", 3)
            joined_errors = "\n".join(validation["errors"])
            for forbidden in (
                "schema_version mismatch",
                "registry_hash mismatch",
                "reference pack mismatch",
                "task_id page mapping mismatch",
                "evidence_files must exactly",
            ):
                self.assertNotIn(forbidden, joined_errors)

    def test_apply_rejects_tamper_source_drift_and_blockers_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "tamper")
            proposal = root / "evidence" / "dry_run_tamper"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            before = _tree_hashes(root / "evidence")
            task_path = proposal / "task_queue.json"
            task = json.loads(task_path.read_text(encoding="utf-8"))
            task["tasks"][0]["task_type"] = "tampered"
            _write_json(task_path, task)
            with self.assertRaisesRegex(ValueError, "proposal hash"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )
            after = _tree_hashes(root / "evidence")
            self.assertEqual(
                {k: v for k, v in before.items() if k != "dry_run_tamper/task_queue.json"},
                {k: v for k, v in after.items() if k != "dry_run_tamper/task_queue.json"},
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "drift")
            proposal = root / "evidence" / "dry_run_drift"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            (root / "novel.txt").write_text("source changed", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source snapshot"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )
            self.assertFalse((root / "evidence" / "scene_clusters.json").exists())

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "blocked", unresolved={2})
            proposal = root / "evidence" / "dry_run_blocked"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            with self.assertRaisesRegex(ValueError, "blocker"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )
            self.assertFalse((root / "evidence" / "scene_clusters.json").exists())

    def test_apply_fault_rolls_back_every_target_and_cleans_staging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            proposal = root / "evidence" / "dry_run_rollback"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)
            review_file = _accepted_review_file(proposal, result["report"])
            evidence = root / "evidence"
            before = _tree_hashes(evidence)

            with self.assertRaisesRegex(RuntimeError, "injected"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                    fault_after_replacements=3,
                )

            self.assertEqual(_tree_hashes(evidence), before)
            self.assertEqual(list(evidence.glob(".migration-staging-*")), [])
            self.assertFalse((evidence / "migration_backups" / result["report"]["migration_id"]).exists())

    def test_artifact_path_preflight_rejects_output_tree_and_unapproved_evidence_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            output_target = root / "输出" / "nested" / "proposal"
            evidence_target = root / "evidence" / "arbitrary" / "proposal"

            with self.assertRaisesRegex(ValueError, "artifact_dir"):
                migrate_project(root, artifact_dir=output_target, now=FIXED_NOW)
            with self.assertRaisesRegex(ValueError, "artifact_dir"):
                migrate_project(root, artifact_dir=evidence_target, now=FIXED_NOW)

            self.assertFalse(output_target.exists())
            self.assertFalse(evidence_target.exists())

    def test_cli_apply_requires_proposal_migration_id_and_confirm_token(self) -> None:
        script = SCRIPTS_DIR / "migrate_scene_pipeline.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "project")
            result = subprocess.run(
                [sys.executable, str(script), "--project", str(root), "--apply"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("proposal", payload["error"])
            self.assertIn("review-file", payload["error"])

    def test_rejects_corrupt_duplicate_and_out_of_range_input_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "corrupt")
            (root / "输入" / "2.jpg").write_bytes(b"not a jpeg")
            with self.assertRaisesRegex(ValueError, "unreadable image"):
                migrate_project(root, now=FIXED_NOW)

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "duplicate")
            _make_image(root / "输入" / "1.jpeg", (1, 1, 1))
            with self.assertRaisesRegex(ValueError, "duplicate page identity"):
                migrate_project(root, now=FIXED_NOW)

        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(Path(tmp) / "range")
            (root / "输入" / "3.jpg").rename(root / "输入" / "10000.jpg")
            with self.assertRaisesRegex(ValueError, "1..9999"):
                migrate_project(root, now=FIXED_NOW)

    def test_cli_dry_run_help_and_missing_project(self) -> None:
        script = SCRIPTS_DIR / "migrate_scene_pipeline.py"
        help_result = subprocess.run(
            [sys.executable, str(script), "--help"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--artifact-dir", help_result.stdout)
        self.assertIn("--apply", help_result.stdout)
        self.assertIn("--review-file", help_result.stdout)

        missing_result = subprocess.run(
            [sys.executable, str(script), "--project", "missing-project", "--dry-run"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(missing_result.returncode, 0)
        payload = json.loads(missing_result.stdout)
        self.assertFalse(payload["ok"])


_REAL_PROJECT_ROOT_TEXT = os.environ.get("REPAIR_COMIC_REAL_PROJECT_ROOT", "").strip()
REAL_PROJECT_ROOT = (
    Path(_REAL_PROJECT_ROOT_TEXT).expanduser().resolve()
    if _REAL_PROJECT_ROOT_TEXT
    else None
)


@unittest.skipUnless(
    REAL_PROJECT_ROOT is not None and REAL_PROJECT_ROOT.is_dir(),
    "set REPAIR_COMIC_REAL_PROJECT_ROOT to enable real-project read-only tests",
)
class RealProjectReadOnlyTests(unittest.TestCase):
    def test_real_project_dry_run_recollects_equal_before_and_after_snapshots(self) -> None:
        self.assertIsNotNone(REAL_PROJECT_ROOT)
        result = migrate_project(
            REAL_PROJECT_ROOT,
            now=FIXED_NOW,
        )
        report = result["report"]

        self.assertGreater(report["input_count_before"], 0)
        self.assertEqual(report["input_count_before"], report["input_count_after"])
        self.assertEqual(report["output_count_before"], report["output_count_after"])
        self.assertEqual(report["source_hashes_before"], report["source_hashes_after"])
        self.assertEqual(
            report["output_snapshot_before"], report["output_snapshot_after"]
        )
        self.assertTrue(report["source_unchanged"])
        self.assertTrue(report["output_unchanged"])
        self.assertIn("evidence/FINAL_QA_REPORT.md", report["locked_sources_before"])
        image_paths = {row["path"] for row in report["image_snapshot_before"]}
        self.assertTrue(any(path.startswith("输入/") for path in image_paths))
        self.assertTrue(any(path.startswith("人物参考图/") for path in image_paths))
        self.assertTrue(any(path.startswith("evidence/page_candidates/") for path in image_paths))

    def test_real_project_blocked_proposal_is_rejected_before_standard_writes(self) -> None:
        self.assertIsNotNone(REAL_PROJECT_ROOT)
        root = REAL_PROJECT_ROOT
        evidence = root / "evidence"
        standard_new = {
            filename: (evidence / filename).exists()
            for filename in PROPOSAL_FILENAMES
            if filename not in {"comic_run_manifest.json", "repair_log.json"}
        }
        manifest_before = _sha(evidence / "comic_run_manifest.json")
        repair_before = _sha(evidence / "repair_log.json")
        with tempfile.TemporaryDirectory() as tmp:
            proposal = Path(tmp) / "real-proposal"
            result = migrate_project(root, artifact_dir=proposal, now=FIXED_NOW)

            with self.assertRaisesRegex(ValueError, "review"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                )

            review_file = _accepted_review_file(proposal, result["report"])

            with self.assertRaisesRegex(ValueError, "blocker"):
                apply_project(
                    root,
                    proposal_dir=proposal,
                    expected_migration_id=result["report"]["migration_id"],
                    confirm_token=APPLY_CONFIRM_TOKEN,
                    review_file=review_file,
                )

        self.assertEqual(_sha(evidence / "comic_run_manifest.json"), manifest_before)
        self.assertEqual(_sha(evidence / "repair_log.json"), repair_before)
        self.assertEqual(
            {
                filename: (evidence / filename).exists()
                for filename in standard_new
            },
            standard_new,
        )


if __name__ == "__main__":
    unittest.main()
