import json
import hashlib
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
import warnings
from unittest import mock
from pathlib import Path

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from project_common import (
    atomic_write_json,
    discover_project,
    ensure_safe_subpath,
    natural_page_key,
    sha256_file,
    sorted_input_pages,
)
from inventory_project import inventory_project
import build_output_manifest as manifest_module
from build_output_manifest import EVIDENCE_FILES, build_manifests
from failure_learning import new_failure_store
from pipeline_contracts import canonical_hash, normalize_page_id
from scene_clusters import bind_reference_pack, build_reference_pack, build_scene_clusters
from task_queue import (
    add_task,
    claim_task,
    complete_task,
    load_queue,
    new_queue,
    save_queue,
)
from validate_output import validate_project
import validate_output as validator_module


EXPECTED_STYLE_REVIEW_CHECKS = (
    "unchanged_regions_preserved",
    "panel_geometry_preserved",
    "line_weight_brush_match",
    "color_texture_match",
    "face_simplification_match",
    "detail_density_match",
    "adjacent_comic_style_match",
    "character_reference_identity_only",
    "no_rendering_upgrade",
)


def with_registry_hash(document):
    document = dict(document)
    document["registry_hash"] = canonical_hash(document)
    return document


class ComicContinuityToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "人物参考图").mkdir()
        (self.root / "输入").mkdir()
        (self.root / "输出").mkdir()
        (self.root / "小说.txt").write_text("第一章\n测试剧情。", encoding="utf-8")
        Image.new("RGB", (32, 48), "white").save(self.root / "人物参考图" / "角色.png")

    def tearDown(self):
        self.temp.cleanup()

    def add_page(self, name, color="white"):
        path = self.root / "输入" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (32, 48), color).save(path)
        return path

    def add_style_artifact(self, evidence, name, mode="comparison"):
        path = evidence / "style_evidence" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "local_mask":
            image = Image.new("L", (32, 48), 0)
            image.paste(255, (8, 8, 24, 32))
            image.save(path)
        elif mode == "full_mask":
            Image.new("L", (32, 48), 255).save(path)
        else:
            Image.new("RGB", (64, 48), "white").save(path)
        return str(path.relative_to(self.root)), sha256_file(path)

    def make_localized_visual_repair(self, evidence, page_index=1):
        repair_path = evidence / "repair_log.json"
        repair = json.loads(repair_path.read_text(encoding="utf-8"))
        page = repair["pages"][page_index]
        output_names = [item["output_name"] for item in repair["pages"]]
        adjacent = [
            name
            for offset, name in enumerate(output_names)
            if offset != page_index and 1 <= abs(offset - page_index) <= 2
        ]
        mask_path, mask_hash = self.add_style_artifact(
            evidence, f"mask-{page_index + 1}.png", "local_mask"
        )
        comparison_path, comparison_hash = self.add_style_artifact(
            evidence, f"comparison-{page_index + 1}.png", "comparison"
        )
        page.update(
            action="edited",
            repair_scope="visual",
            had_ordinary_text=True,
            text_recognition="reliable",
            text_reset_mode="page",
            text_reset_reason="visual_repair_requires_page_reset",
            visual_issue_extent="localized",
            visual_edit_mode="localized_inpaint",
            style_reference_pages=adjacent,
            identity_reference_files=["角色.png"],
            character_reference_role="identity_only",
            visual_mask_path=mask_path,
            visual_mask_sha256=mask_hash,
            changed_region_ratio=(16 * 24) / (32 * 48),
            full_page_regeneration_reason=None,
            style_review={
                "status": "passed",
                "full_size": True,
                "reviewer": "style-reviewer",
                "reviewed_at": "2026-07-13T12:15:00+08:00",
                "comparison_path": comparison_path,
                "comparison_sha256": comparison_hash,
                "full_page_exception_approved": False,
                "checks": {name: True for name in EXPECTED_STYLE_REVIEW_CHECKS},
            },
        )
        atomic_write_json(repair_path, repair)

    def mutate_page(self, evidence, filename, mutate, page_index=0):
        path = evidence / filename
        document = json.loads(path.read_text(encoding="utf-8"))
        mutate(document["pages"][page_index])
        atomic_write_json(path, document)
        return validate_project(self.root, evidence, len(document["pages"]))

    def rewrite_registry(self, evidence, filename, mutate):
        path = evidence / filename
        document = json.loads(path.read_text(encoding="utf-8"))
        document.pop("registry_hash", None)
        mutate(document)
        atomic_write_json(path, with_registry_hash(document))
        return document

    def make_complete_fixture(self, count=1):
        for index in range(1, count + 1):
            self.add_page(f"{188 + index}.jpg", color=(index, index, index))
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=count)
        for index in range(1, count + 1):
            shutil.copy2(
                self.root / "输入" / f"{188 + index}.jpg",
                self.root / "输出" / f"{index:04d}.jpg",
            )
        run_path = evidence / "comic_run_manifest.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        # These completed-fixture tests intentionally target the legacy V3
        # validator. Keep their synthetic conversion outside the production V4
        # initializer, whose output names stay exact and whose batches stay absent.
        legacy_output_names = [f"{index:04d}.jpg" for index in range(1, count + 1)]
        run.update(schema_version="3.0", pipeline_mode="scene_cluster_v1")
        for page, output_name in zip(run["pages"], legacy_output_names):
            page["output_name"] = output_name
        atomic_write_json(run_path, run)

        alignment_path = evidence / "novel_alignment.json"
        alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
        for page, output_name in zip(alignment["pages"], legacy_output_names):
            page["output_name"] = output_name
        atomic_write_json(alignment_path, alignment)

        repair_path = evidence / "repair_log.json"
        repair = json.loads(repair_path.read_text(encoding="utf-8"))
        repair.update(schema_version="3.0", pipeline_mode="scene_cluster_v1")
        batches, page_to_batch = manifest_module._build_batches(legacy_output_names)
        repair["batches"] = batches
        for page, output_name in zip(repair["pages"], legacy_output_names):
            page["output_name"] = output_name
            page["batch_id"] = page_to_batch[output_name]
        atomic_write_json(repair_path, repair)

        run["status"] = "batch_qa_passed"
        lock_ids = [f"lock-{category}" for category in manifest_module.CONTINUITY_CATEGORIES]
        output_names = [page["output_name"] for page in run["pages"]]
        clusters = []
        reference_packs = []
        page_to_cluster = {}
        page_to_pack = {}
        page_to_reference_binding = {}
        reference_file = next((self.root / "人物参考图").iterdir())
        reference_rel = reference_file.relative_to(self.root).as_posix()
        approved_hashes = {reference_rel: sha256_file(reference_file)}
        cluster_blueprints = build_scene_clusters(
            [
                {
                    "page_id": output_name,
                    "chapter": "fixture-chapter",
                    "location": "fixture-location",
                    "story_time": "fixture-time",
                    "scene_id": "fixture-scene",
                    # This V3 complete-fixture models processed visual pages, so it
                    # must declare visual work under the V4 canary contract.
                    "has_visual_task": True,
                }
                for output_name in output_names
            ]
        )
        output_by_page_id = {
            normalize_page_id(output_name): output_name for output_name in output_names
        }
        for cluster_blueprint in cluster_blueprints:
            cluster_blueprint = dict(cluster_blueprint)
            for field in ("member_pages", "context_before", "context_after"):
                cluster_blueprint[field] = [
                    output_by_page_id[page_id]
                    for page_id in cluster_blueprint[field]
                ]
            cluster_blueprint["canary_page"] = output_by_page_id[
                cluster_blueprint["canary_page"]
            ]
            members = cluster_blueprint["member_pages"]
            if len(output_names) < 8 and members == output_names:
                cluster_blueprint["boundary_exception"] = True
                cluster_blueprint["undersized_reason"] = "project_total_below_min"
            else:
                cluster_blueprint["boundary_exception"] = False
                cluster_blueprint["undersized_reason"] = None
            cluster_id = cluster_blueprint["cluster_id"]
            pack = build_reference_pack(
                {"cluster_id": cluster_id},
                [
                    {
                        "path": reference_rel,
                        "role": "identity_only",
                        "sha256": approved_hashes[reference_rel],
                    }
                ],
                stable_pages=[],
            )
            cluster = bind_reference_pack(cluster_blueprint, pack)
            clusters.append(cluster)
            reference_packs.append(pack)
            for output_name in members:
                page_to_cluster[output_name] = cluster_id
                page_to_pack[output_name] = pack["reference_pack_id"]
                page_to_reference_binding[output_name] = pack[
                    "reference_binding_hash"
                ]
        atomic_write_json(
            evidence / "scene_clusters.json",
            with_registry_hash({"schema_version": "1.0", "clusters": clusters}),
        )
        atomic_write_json(
            evidence / "style_reference_packs.json",
            with_registry_hash(
                {
                    "schema_version": "1.0",
                    "stable_pages": [],
                    "approved_hashes": approved_hashes,
                    "reference_packs": reference_packs,
                }
            ),
        )
        fixture_queue = new_queue()
        for page in run["pages"]:
            page["state"] = "batch_qa_passed"
            page["output_sha256"] = sha256_file(self.root / "输出" / page["output_name"])
            page["continuity_lock_ids"] = lock_ids
            task = add_task(
                fixture_queue,
                page_id=page["output_name"],
                task_type="continuity_check",
                payload_hash=page["input_sha256"],
                cluster_id=page_to_cluster[page["output_name"]],
                prompt_reference_hash=page_to_reference_binding[page["output_name"]],
                now="2026-07-13T12:00:00+08:00",
            )
            leased = claim_task(
                fixture_queue,
                worker="fixture-worker",
                now="2026-07-13T12:01:00+08:00",
            )
            self.assertEqual(task["task_id"], leased["task_id"])
            complete_task(
                fixture_queue,
                task_id=task["task_id"],
                worker="fixture-worker",
                candidate_path=page["output_name"],
                candidate_hash=page["output_sha256"],
                now="2026-07-13T12:02:00+08:00",
            )
            page.update(
                cluster_id=page_to_cluster[page["output_name"]],
                reference_pack_id=page_to_pack[page["output_name"]],
                task_id=task["task_id"],
                failure_rule_ids=[],
                generated_by="fixture-worker",
                reviewed_by="page-reviewer",
            )
        atomic_write_json(run_path, run)
        atomic_write_json(evidence / "task_queue.json", with_registry_hash(fixture_queue))
        atomic_write_json(
            evidence / "failure_learning.json",
            with_registry_hash(new_failure_store()),
        )
        bible_path = evidence / "continuity_bible.json"
        bible = json.loads(bible_path.read_text(encoding="utf-8"))
        bible.update(
            {
                "status": "confirmed",
                "reviewer": "continuity-reviewer",
                "reviewed_at": "2026-07-13T12:05:00+08:00",
                "source_priority": ["novel", "reference", "adjacent_page", "established_page", "inference"],
                "coverage": {
                    category: {
                        "status": "confirmed",
                        "rationale": "",
                        "lock_ids": [f"lock-{category}"],
                    }
                    for category in manifest_module.CONTINUITY_CATEGORIES
                },
                "locks": [
                    {
                        "lock_id": f"lock-{category}",
                        "category": category,
                        "subject": "测试主体",
                        "attribute": "连续性",
                        "value": "保持一致",
                        "source_type": "novel",
                        "source_ref": "小说.txt:0",
                        "applies_to_pages": [page["output_name"] for page in run["pages"]],
                        "confidence": 1.0,
                        "created_by": "continuity-reviewer",
                        "created_at": "2026-07-13T12:04:00+08:00",
                        "confirmed": True,
                        "reviewer": "continuity-reviewer",
                        "reviewed_at": "2026-07-13T12:05:00+08:00",
                    }
                    for category in manifest_module.CONTINUITY_CATEGORIES
                ],
            }
        )
        atomic_write_json(bible_path, bible)
        alignment_path = evidence / "novel_alignment.json"
        alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
        alignment["status"] = "confirmed"
        novel_text = (self.root / "小说.txt").read_text(encoding="utf-8")
        novel_hash = sha256_file(self.root / "小说.txt")
        for page in alignment["pages"]:
            page.update(
                {
                    "status": "confirmed",
                    "evidence": "第一章测试剧情",
                    "confidence": 1.0,
                    "novel_sha256": novel_hash,
                    "chapter": "第一章",
                    "start_offset": 0,
                    "end_offset": len(novel_text),
                    "source_excerpt": novel_text,
                    "context_excerpt": novel_text,
                    "located_by": "manual_source_match",
                    "located_at": "2026-07-13T12:00:00+08:00",
                    "scene_summary": "角色在测试场景推进剧情",
                    "involved_characters": ["角色"],
                    "dialogue_owners": [],
                    "props": [],
                    "location": "测试场景",
                    "story_time": "第一章当日",
                }
            )
        atomic_write_json(alignment_path, alignment)
        repair_path = evidence / "repair_log.json"
        repair = json.loads(repair_path.read_text(encoding="utf-8"))
        repair["status"] = "batch_qa_passed"
        for page in repair["pages"]:
            output = self.root / "输出" / page["output_name"]
            snapshot = evidence / "text_snapshots" / f"{page['index']:04d}.txt"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text("文字快照", encoding="utf-8")
            page.update(
                {
                    "cluster_id": run["pages"][page["index"] - 1]["cluster_id"],
                    "reference_pack_id": run["pages"][page["index"] - 1]["reference_pack_id"],
                    "task_id": run["pages"][page["index"] - 1]["task_id"],
                    "failure_rule_ids": [],
                    "generated_by": "fixture-worker",
                    "reviewed_by": "page-reviewer",
                    "continuity_lock_ids": lock_ids,
                    "action": "unchanged_copy",
                    "repair_scope": "unchanged",
                    "visual_issue_extent": "not_applicable",
                    "visual_edit_mode": "not_applicable",
                    "style_reference_pages": [],
                    "identity_reference_files": [],
                    "character_reference_role": "not_applicable",
                    "visual_mask_path": None,
                    "visual_mask_sha256": None,
                    "changed_region_ratio": 0.0,
                    "full_page_regeneration_reason": None,
                    "style_review": {
                        "status": "not_applicable",
                        "full_size": False,
                        "reviewer": None,
                        "reviewed_at": None,
                        "comparison_path": None,
                        "comparison_sha256": None,
                        "full_page_exception_approved": False,
                        "checks": {
                            name: False for name in manifest_module.STYLE_REVIEW_CHECKS
                        },
                    },
                    "had_ordinary_text": False,
                    "text_recognition": "not_applicable",
                    "candidate_path": str(output.relative_to(self.root)),
                    "candidate_sha256": sha256_file(output),
                    "text_reset_mode": "none",
                    "text_reset_reason": "unchanged_page",
                    "used_external_text_resource": False,
                    "external_candidate": None,
                    "text_snapshot_path": str(snapshot.relative_to(self.root)),
                    "text_snapshot_sha256": sha256_file(snapshot),
                    "visual_review": {
                        "status": "passed",
                        "full_size": True,
                        "reviewer": "qa-reviewer",
                        "reviewed_at": "2026-07-13T12:10:00+08:00",
                        "checks": {name: True for name in manifest_module.VISUAL_REVIEW_CHECKS},
                    },
                    "page_qa": {
                        "status": "passed",
                        "pass_id": f"page-pass-{page['index']}",
                        "reviewer": "page-reviewer",
                        "reviewed_at": "2026-07-13T12:20:00+08:00",
                        "checks": {name: True for name in manifest_module.PAGE_QA_CHECKS},
                    },
                }
            )
        for batch in repair["batches"]:
            batch["batch_qa"] = {
                "status": "passed",
                "pass_id": f"batch-pass-{batch['batch_id']}",
                "reviewer": "batch-reviewer",
                "reviewed_at": "2026-07-13T12:30:00+08:00",
                "checks": {name: True for name in manifest_module.BATCH_QA_CHECKS},
            }
        atomic_write_json(repair_path, repair)
        atomic_write_json(
            evidence / "scene_cluster_qa.json",
            with_registry_hash(
                {
                    "schema_version": "1.0",
                    "clusters": [
                        {
                            "cluster_id": cluster["cluster_id"],
                            "member_pages": cluster["member_pages"],
                            "canary_page": cluster["canary_page"],
                            "status": "passed",
                            "pass_id": f"cluster-pass-{cluster['cluster_id']}",
                            "reviewed_by": "cluster-reviewer",
                            "reviewed_at": "2026-07-13T12:35:00+08:00",
                        }
                        for cluster in clusters
                    ],
                }
            ),
        )
        (evidence / "FINAL_QA_REPORT.md").write_text(
            "# FINAL QA REPORT\n\n"
            "status: passed\n"
            "reviewer: final-reviewer\n"
            "reviewed_at: 2026-07-13T12:40:00+08:00\n"
            f"input_count: {count}\n"
            f"output_count: {count}\n"
            f"page_qa_passed: {count}\n"
            f"batch_count: {len(repair['batches'])}\n"
            f"batch_qa_passed: {len(repair['batches'])}\n"
            "blocking_issues: 0\n"
            "unresolved_issues: 0\n",
            encoding="utf-8",
        )
        return evidence

    def test_parenthesized_insert_sorts_after_main_and_before_next_page(self):
        names = ["253.jpg", "252（1）.jpg", "252.jpg", "252(2).jpg"]
        self.assertEqual(
            ["252.jpg", "252（1）.jpg", "252(2).jpg", "253.jpg"],
            sorted(names, key=lambda value: natural_page_key(Path(value))),
        )

    def test_mixed_letter_digit_page_names_sort_deterministically(self):
        names = ["a1.jpg", "1a.jpg", "a10.jpg", "1b.jpg", "a2.jpg"]
        first = sorted(names, key=lambda value: natural_page_key(Path(value)))
        second = sorted(reversed(names), key=lambda value: natural_page_key(Path(value)))
        self.assertEqual(first, second)
        self.assertEqual(set(names), set(first))

    def test_normalized_duplicate_insert_identity_is_rejected(self):
        self.add_page("252（1）.jpg")
        self.add_page("252(1).jpg")
        with self.assertRaisesRegex(ValueError, "duplicate page identity"):
            sorted_input_pages(self.root / "输入")

    def test_nfkc_equivalent_page_identity_is_rejected_without_renaming_source(self):
        fullwidth = self.add_page("Ａ.jpg")
        ascii_page = self.add_page("A.png")
        self.assertEqual("Ａ.jpg", fullwidth.name)
        self.assertEqual("A.png", ascii_page.name)

        with self.assertRaisesRegex(ValueError, "duplicate page identity"):
            sorted_input_pages(self.root / "输入")

    def test_input_inventory_accepts_supported_extensions_recursively(self):
        self.add_page("1.jpg")
        self.add_page("2.jpeg")
        self.add_page("chapter/3.png")
        self.add_page("chapter/4.webp")
        self.assertEqual(
            ["1.jpg", "2.jpeg", "chapter/3.png", "chapter/4.webp"],
            [
                path.relative_to(self.root / "输入").as_posix()
                for path in sorted_input_pages(self.root / "输入")
            ],
        )

    def test_inventory_records_exact_relative_path_extension_dimensions_and_hash(self):
        page = self.add_page("章节一/252（1）.png")

        inventory = inventory_project(self.root, expected_count=1)

        self.assertEqual(
            {
                "index": 1,
                "input_name": "章节一/252（1）.png",
                "relative_path": "章节一/252（1）.png",
                "extension": ".png",
                "width": 32,
                "height": 48,
                "input_sha256": sha256_file(page),
            },
            inventory["pages"][0],
        )

    def test_inventory_records_hash_bound_novel_decoding_without_mutation(self):
        novel = self.root / "小说.txt"
        text = "国标小说正文"
        raw = text.encode("gb18030")
        novel.write_bytes(raw)

        inventory = inventory_project(self.root)

        self.assertEqual(raw, novel.read_bytes())
        self.assertEqual("gb18030", inventory["novel"]["encoding"])
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(), inventory["novel"]["raw_sha256"]
        )
        self.assertEqual(
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            inventory["novel"]["decoded_sha256"],
        )

    def test_discovery_finds_fixed_project_resources(self):
        self.add_page("189.jpg")
        paths = discover_project(self.root)
        self.assertEqual(self.root / "小说.txt", paths.novel)
        self.assertEqual(self.root / "人物参考图", paths.references)
        self.assertEqual(self.root / "输入", paths.input_dir)
        self.assertEqual(self.root / "输出", paths.output_dir)

    def test_discovery_rejects_multiple_novel_txt_files(self):
        (self.root / "另一本.txt").write_text("冲突", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "multiple novel txt"):
            discover_project(self.root)

    def test_inventory_rejects_corrupt_reference_image(self):
        (self.root / "人物参考图" / "坏图.jpg").write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "unreadable reference image"):
            inventory_project(self.root)

    def test_manifest_rejects_corrupt_reference_image(self):
        self.add_page("189.jpg")
        (self.root / "人物参考图" / "坏图.jpg").write_bytes(b"not an image")
        with self.assertRaisesRegex(ValueError, "unreadable reference image"):
            build_manifests(self.root, self.root / "证据", expected_count=1)
        self.assertFalse((self.root / "证据").exists())

    def test_inventory_and_manifest_record_reference_hashes(self):
        self.add_page("189.jpg")
        reference = self.root / "人物参考图" / "角色.png"
        expected_hash = sha256_file(reference)
        inventory = inventory_project(self.root, expected_count=1)
        self.assertEqual(expected_hash, inventory["reference_files"][0]["sha256"])
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)
        bible = json.loads((evidence / "continuity_bible.json").read_text(encoding="utf-8"))
        self.assertEqual(expected_hash, bible["reference_files"][0]["sha256"])

    def test_safe_subpath_rejects_escape(self):
        self.assertEqual((self.root / "证据").resolve(), ensure_safe_subpath(self.root, "证据"))
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            ensure_safe_subpath(self.root, self.root.parent / "outside")

    def test_atomic_write_json_replaces_complete_document(self):
        target = self.root / "证据" / "atomic.json"
        atomic_write_json(target, {"version": 1})
        atomic_write_json(target, {"version": 2, "中文": "保留"})
        self.assertEqual({"version": 2, "中文": "保留"}, json.loads(target.read_text(encoding="utf-8")))
        self.assertEqual([], list(target.parent.glob("tmp*")))

    def test_manifest_preserves_source_names_and_declares_all_evidence_files(self):
        for name in ("189.jpg", "252（1）.jpg", "分卷/269.webp"):
            self.add_page(name)
        evidence = self.root / "证据"
        result = build_manifests(self.root, evidence, expected_count=3)
        self.assertEqual(3, len(result["pages"]))
        self.assertEqual(
            ["189.jpg", "252（1）.jpg", "分卷/269.webp"],
            [page["output_name"] for page in result["pages"]],
        )
        self.assertEqual(
            {
                "comic_run_manifest.json",
                "continuity_bible.json",
                "novel_alignment.json",
                "repair_log.json",
                "FINAL_QA_REPORT.md",
                "scene_clusters.json",
                "style_reference_packs.json",
                "task_queue.json",
                "failure_learning.json",
                "scene_cluster_qa.json",
                "entity_state_timeline.json",
                "page_audit.json",
                "review_events.jsonl",
                "text_geometry.json",
                "regression_summary.json",
            },
            set(EVIDENCE_FILES),
        )
        self.assertTrue(all((evidence / name).exists() for name in EVIDENCE_FILES))

    def test_manifest_initializes_v4_exact_names_and_required_registries(self):
        for name in ("3.webp", "1.jpg", "章节/2.PNG"):
            self.add_page(name)
        evidence = self.root / "证据"

        result = build_manifests(self.root, evidence)
        repair = json.loads((evidence / "repair_log.json").read_text(encoding="utf-8"))

        self.assertEqual(3, result["expected_count"])
        self.assertEqual("continuity_v4", result["pipeline_mode"])
        self.assertEqual("4.0", result["schema_version"])
        self.assertEqual(list(EVIDENCE_FILES), result["evidence_files"])
        self.assertEqual(
            ["1.jpg", "3.webp", "章节/2.PNG"],
            [page["output_name"] for page in result["pages"]],
        )
        self.assertEqual(
            [page["input_name"] for page in result["pages"]],
            [page["output_name"] for page in result["pages"]],
        )
        self.assertEqual(
            {
                "entity_state_timeline.json",
                "page_audit.json",
                "review_events.jsonl",
                "text_geometry.json",
                "regression_summary.json",
            },
            {name for name in EVIDENCE_FILES if name in {
                "entity_state_timeline.json",
                "page_audit.json",
                "review_events.jsonl",
                "text_geometry.json",
                "regression_summary.json",
            }},
        )
        self.assertTrue(all((evidence / name).is_file() for name in EVIDENCE_FILES))
        self.assertEqual(b"", (evidence / "review_events.jsonl").read_bytes())
        neutral_fields = {
            "cluster_id": None,
            "reference_pack_id": None,
            "task_id": None,
            "failure_rule_ids": [],
            "page_class": None,
            "generated_by": None,
            "reviewed_by": None,
        }
        for page in result["pages"]:
            for key, value in neutral_fields.items():
                self.assertEqual(value, page[key])
        self.assertEqual("continuity_v4", repair["pipeline_mode"])
        self.assertEqual("4.0", repair["schema_version"])
        self.assertNotIn("batches", repair)
        for page in repair["pages"]:
            for key, value in neutral_fields.items():
                self.assertEqual(value, page[key])

    def test_manifest_derives_three_hundred_exact_output_names(self):
        for number in range(1, 301):
            self.add_page(f"{number}.jpg", color=(number % 255, 0, 0))
        result = build_manifests(self.root, self.root / "证据")
        self.assertEqual(300, result["expected_count"])
        self.assertEqual("1.jpg", result["pages"][0]["output_name"])
        self.assertEqual("300.jpg", result["pages"][-1]["output_name"])

    def test_manifest_rejects_provided_count_that_differs_from_inputs(self):
        for number in range(1, 4):
            self.add_page(f"{number}.jpg")
        with self.assertRaisesRegex(ValueError, "expected 2, found 3"):
            build_manifests(self.root, self.root / "证据", expected_count=2)

    def test_manifest_cli_allows_omitting_expected_count(self):
        for number in range(1, 4):
            self.add_page(f"{number}.jpg")
        evidence = self.root / "证据"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "build_output_manifest.py"),
                "--root",
                str(self.root),
                "--evidence-dir",
                str(evidence),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        manifest = json.loads((evidence / "comic_run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(3, manifest["expected_count"])

    def test_manifest_does_not_initialize_legacy_batches(self):
        for number in range(1, 14):
            self.add_page(f"{number}.jpg")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=13)
        repair = json.loads((evidence / "repair_log.json").read_text(encoding="utf-8"))
        self.assertNotIn("batches", repair)

    def test_batch_sizes_are_deterministic_for_three_fifteen_and_three_hundred(self):
        self.assertEqual([3], manifest_module.expected_batch_sizes(3))
        self.assertEqual([8, 7], manifest_module.expected_batch_sizes(15))
        self.assertEqual([12] * 25, manifest_module.expected_batch_sizes(300))

    def test_batch_size_contract_rejects_out_of_range_counts(self):
        for count in (0, -1, 10000, True):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    manifest_module.expected_batch_sizes(count)

    def test_manifest_does_not_overwrite_existing_evidence_without_force(self):
        self.add_page("189.jpg")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)
        before = (evidence / "comic_run_manifest.json").read_bytes()
        with self.assertRaisesRegex(FileExistsError, "evidence already exists"):
            build_manifests(self.root, evidence, expected_count=1)
        self.assertEqual(before, (evidence / "comic_run_manifest.json").read_bytes())

    def test_force_manifest_rolls_back_all_files_when_third_replace_fails(self):
        self.add_page("189.jpg")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)
        before = {name: (evidence / name).read_bytes() for name in EVIDENCE_FILES}
        real_replace = os.replace
        final_replacements = 0

        def fail_third_final_replace(source, destination):
            nonlocal final_replacements
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path.parent == evidence
                and source_path.parent.name.startswith(".manifest-staging-")
            ):
                final_replacements += 1
                if final_replacements == 3:
                    raise OSError("injected third replacement failure")
            return real_replace(source, destination)

        with mock.patch.object(manifest_module.os, "replace", side_effect=fail_third_final_replace):
            with self.assertRaisesRegex(OSError, "third replacement failure"):
                build_manifests(self.root, evidence, expected_count=1, force=True)
        self.assertEqual(before, {name: (evidence / name).read_bytes() for name in EVIDENCE_FILES})
        self.assertEqual([], list(evidence.glob(".manifest-staging-*")))
        self.assertEqual([], list(evidence.glob(".manifest-backup-*")))

    def test_force_manifest_cleans_staging_when_backup_directory_creation_fails(self):
        self.add_page("189.jpg")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)
        before = {name: (evidence / name).read_bytes() for name in EVIDENCE_FILES}
        real_mkdtemp = tempfile.mkdtemp

        def fail_backup_creation(*args, **kwargs):
            if kwargs.get("prefix") == ".manifest-backup-":
                raise OSError("injected backup directory failure")
            return real_mkdtemp(*args, **kwargs)

        with mock.patch.object(
            manifest_module.tempfile, "mkdtemp", side_effect=fail_backup_creation
        ):
            with self.assertRaisesRegex(OSError, "backup directory failure"):
                build_manifests(self.root, evidence, expected_count=1, force=True)

        self.assertEqual(before, {name: (evidence / name).read_bytes() for name in EVIDENCE_FILES})
        self.assertEqual([], list(evidence.glob(".manifest-staging-*")))
        self.assertEqual([], list(evidence.glob(".manifest-backup-*")))

    def test_force_manifest_successfully_replaces_all_v4_pending_evidence(self):
        self.add_page("nested/189.webp")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)

        build_manifests(self.root, evidence, expected_count=1, force=True)

        self.assertEqual(set(EVIDENCE_FILES), {path.name for path in evidence.iterdir()})
        self.assertEqual(b"", (evidence / "review_events.jsonl").read_bytes())
        self.assertIn("status: pending", (evidence / "FINAL_QA_REPORT.md").read_text(encoding="utf-8"))
        for filename in EVIDENCE_FILES[5:]:
            if not filename.endswith(".json"):
                continue
            document = json.loads((evidence / filename).read_text(encoding="utf-8"))
            registry_hash = document.pop("registry_hash")
            self.assertEqual(canonical_hash(document), registry_hash, filename)
            self.assertEqual("pending", document.get("status"), filename)

    def test_force_manifest_preserves_full_backup_when_rollback_is_incomplete(self):
        self.add_page("189.jpg")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)
        before = {name: (evidence / name).read_bytes() for name in EVIDENCE_FILES}
        real_replace = os.replace
        commit_replacements = 0
        rollback_failed = False

        def fail_commit_and_rollback(source, destination):
            nonlocal commit_replacements, rollback_failed
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path.parent == evidence
                and source_path.parent.name.startswith(".manifest-staging-")
            ):
                commit_replacements += 1
                if commit_replacements == 2:
                    raise OSError("injected second commit failure")
            if (
                destination_path.parent == evidence
                and source_path.parent.name.startswith(".manifest-backup-")
                and source_path.name.startswith(".restore-")
                and not rollback_failed
            ):
                rollback_failed = True
                raise OSError("injected first rollback failure")
            return real_replace(source, destination)

        with mock.patch.object(manifest_module.os, "replace", side_effect=fail_commit_and_rollback):
            with self.assertRaisesRegex(OSError, "rollback incomplete") as raised:
                build_manifests(self.root, evidence, expected_count=1, force=True)
        backups = list(evidence.glob(".manifest-backup-*"))
        stagings = list(evidence.glob(".manifest-staging-*"))
        self.assertEqual(1, len(backups))
        self.assertEqual(1, len(stagings))
        self.assertIn(str(backups[0].resolve()), str(raised.exception))
        self.assertEqual(before, {name: (backups[0] / name).read_bytes() for name in EVIDENCE_FILES})

    def test_manifest_does_not_change_original_material_hashes(self):
        page = self.add_page("189.jpg")
        materials = [self.root / "小说.txt", self.root / "人物参考图" / "角色.png", page]
        before = {path: sha256_file(path) for path in materials}
        build_manifests(self.root, self.root / "证据", expected_count=1)
        self.assertEqual(before, {path: sha256_file(path) for path in materials})

    def test_manifest_does_not_create_or_modify_output_tree(self):
        self.add_page("nested/189.webp")
        existing = self.root / "输出" / "keep" / "existing.png"
        existing.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), "blue").save(existing)

        def output_snapshot():
            return {
                path.relative_to(self.root / "输出").as_posix(): sha256_file(path)
                for path in (self.root / "输出").rglob("*")
                if path.is_file()
            }

        before = output_snapshot()
        build_manifests(self.root, self.root / "证据", expected_count=1)
        self.assertEqual(before, output_snapshot())

    def test_all_clis_have_help_and_fail_nonzero_for_missing_project(self):
        for script in ("inventory_project.py", "build_output_manifest.py", "validate_output.py"):
            help_result = subprocess.run(
                [sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True
            )
            self.assertEqual(0, help_result.returncode, script)
            self.assertIn("Python 3.11", help_result.stdout)
        commands = (
            [sys.executable, str(SCRIPTS / "inventory_project.py"), "--root", str(self.root / "missing")],
            [sys.executable, str(SCRIPTS / "build_output_manifest.py"), "--root", str(self.root / "missing"), "--evidence-dir", "证据", "--expected-count", "1"],
            [sys.executable, str(SCRIPTS / "validate_output.py"), "--root", str(self.root / "missing"), "--evidence-dir", "证据", "--expected-count", "1"],
        )
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_validator_rejects_unfinished_then_accepts_complete_fixture(self):
        self.add_page("189.jpg")
        evidence = self.root / "证据"
        build_manifests(self.root, evidence, expected_count=1)
        failed = validate_project(self.root, evidence, expected_count=1)
        self.assertFalse(failed["ok"])
        self.assertTrue(any("output" in item or "state" in item for item in failed["errors"]))

        shutil.rmtree(evidence)
        evidence = self.make_complete_fixture()
        passed = validate_project(self.root, evidence, expected_count=1)
        self.assertTrue(passed["ok"], passed["errors"])

    def test_validator_derives_expected_count_when_omitted(self):
        evidence = self.make_complete_fixture(count=3)
        result = validate_project(self.root, evidence)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(3, result["counts"]["expected"])

    def test_validator_rejects_provided_count_that_differs_from_input(self):
        evidence = self.make_complete_fixture(count=2)
        with self.assertRaisesRegex(ValueError, "expected 1, found 2"):
            validate_project(self.root, evidence, expected_count=1)

    def test_validator_rejects_invalid_explicit_counts(self):
        evidence = self.make_complete_fixture(count=1)
        for count in (0, -1, 10000, True):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    validate_project(self.root, evidence, expected_count=count)

    def test_validator_rejects_invalid_count_before_project_discovery(self):
        with self.assertRaises(ValueError):
            validate_project(
                self.root / "missing-project",
                self.root / "missing-evidence",
                expected_count=0,
            )

    def test_validator_cli_allows_omitting_expected_count(self):
        evidence = self.make_complete_fixture(count=3)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "validate_output.py"),
                "--root",
                str(self.root),
                "--evidence-dir",
                str(evidence),
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(3, json.loads(result.stdout)["counts"]["expected"])

    def test_validator_accepts_thirteen_page_balanced_batches(self):
        evidence = self.make_complete_fixture(count=13)
        result = validate_project(self.root, evidence)
        self.assertTrue(result["ok"], result["errors"])

    def test_validator_rejects_missing_output_page(self):
        evidence = self.make_complete_fixture(count=2)
        (self.root / "输出" / "0002.jpg").unlink()
        self.assertFalse(validate_project(self.root, evidence, 2)["ok"])

    def test_scene_mode_validator_rejects_extra_output_page(self):
        evidence = self.make_complete_fixture(count=2)
        Image.new("RGB", (32, 48), "red").save(self.root / "输出" / "0003.jpg")
        errors = validate_project(self.root, evidence, 2)["errors"]
        self.assertTrue(any("output count mismatch" in error for error in errors))

    def test_scene_mode_validator_requires_cluster_and_task_traceability(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "comic_run_manifest.json",
            lambda page: page.update(cluster_id=None, task_id=None),
        )
        self.assertTrue(any("cluster_id" in error for error in result["errors"]))
        self.assertTrue(any("task_id" in error for error in result["errors"]))

    def test_scene_mode_validator_rejects_unfinished_queue_task(self):
        evidence = self.make_complete_fixture()
        path = evidence / "task_queue.json"
        queue = json.loads(path.read_text(encoding="utf-8"))
        queue["tasks"][0]["state"] = "queued"
        atomic_write_json(path, queue)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("unfinished task" in error for error in errors))

    def test_scene_mode_validator_rejects_duplicate_input_mapping(self):
        evidence = self.make_complete_fixture(count=2)
        path = evidence / "comic_run_manifest.json"
        run = json.loads(path.read_text(encoding="utf-8"))
        run["pages"][1]["input_name"] = run["pages"][0]["input_name"]
        run["pages"][1]["input_sha256"] = run["pages"][0]["input_sha256"]
        atomic_write_json(path, run)
        errors = validate_project(self.root, evidence, 2)["errors"]
        self.assertTrue(any("bijection" in error for error in errors))

    def test_scene_mode_validator_rejects_untraceable_failure_rule(self):
        evidence = self.make_complete_fixture()
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pages"][0]["failure_rule_ids"] = ["rule-does-not-exist"]
            atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("failure_rule_ids" in error for error in errors))

    def test_scene_mode_validator_requires_style_reference_pack_file(self):
        evidence = self.make_complete_fixture()
        (evidence / "style_reference_packs.json").unlink()
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("style_reference_packs.json" in error for error in errors))

    def test_scene_mode_validator_rejects_unknown_reference_pack_id(self):
        evidence = self.make_complete_fixture()
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pages"][0]["reference_pack_id"] = "missing-reference-pack"
            atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("reference_pack_id is not traceable" in error for error in errors))

    def test_scene_registry_hash_tampering_is_rejected(self):
        evidence = self.make_complete_fixture(3)
        path = evidence / "scene_clusters.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["clusters"][0]["canary_page"] = "9999.jpg"
        atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 3)["errors"]
        self.assertTrue(any("scene_clusters registry_hash" in error for error in errors))

    def test_scene_cluster_membership_and_context_are_hard_bound(self):
        evidence = self.make_complete_fixture(9)
        self.rewrite_registry(
            evidence,
            "scene_clusters.json",
            lambda document: document["clusters"][0]["member_pages"].pop(),
        )
        errors = validate_project(self.root, evidence, 9)["errors"]
        self.assertTrue(any("cluster membership" in error for error in errors))

    def test_scene_mode_rejects_unbound_reference_pack_cluster(self):
        evidence = self.make_complete_fixture()
        self.rewrite_registry(
            evidence,
            "scene_clusters.json",
            lambda document: document["clusters"][0].update(
                reference_pack_id=None,
                reference_pack_state="unbound",
            ),
        )
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(
            any("reference pack must be bound" in error for error in errors),
            errors,
        )

    def test_valid_primary_style_pack_uses_top_level_stable_pages(self):
        evidence = self.make_complete_fixture(3)
        input_page = sorted_input_pages(self.root / "输入")[0]
        stable_path = input_page.relative_to(self.root).as_posix()
        stable_hash = sha256_file(input_page)

        def mutate(document):
            cluster_id = document["reference_packs"][0]["cluster_id"]
            document["stable_pages"] = [stable_path]
            document["approved_hashes"] = {stable_path: stable_hash}
            document["reference_packs"][0] = build_reference_pack(
                {"cluster_id": cluster_id},
                [
                    {
                        "path": stable_path,
                        "role": "primary_style",
                        "sha256": stable_hash,
                    }
                ],
                stable_pages=[stable_path],
            )

        pack_document = self.rewrite_registry(
            evidence, "style_reference_packs.json", mutate
        )
        pack = pack_document["reference_packs"][0]
        self.rewrite_registry(
            evidence,
            "scene_clusters.json",
            lambda document: document["clusters"][0].update(
                reference_pack_id=pack["reference_pack_id"]
            ),
        )
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            for page in document["pages"]:
                page["reference_pack_id"] = pack["reference_pack_id"]
            atomic_write_json(path, document)
        self.rewrite_registry(
            evidence,
            "task_queue.json",
            lambda document: [
                task.update(prompt_reference_hash=pack["reference_binding_hash"])
                for task in document["tasks"]
            ],
        )
        self.assertEqual([], validate_project(self.root, evidence, 3)["errors"])

    def test_replacing_reference_pack_content_requires_new_id_and_task_binding(self):
        evidence = self.make_complete_fixture()
        replacement_file = self.root / "人物参考图" / "替换角色.png"
        Image.new("RGB", (32, 48), "blue").save(replacement_file)
        replacement_path = replacement_file.relative_to(self.root).as_posix()
        replacement_hash = sha256_file(replacement_file)

        def mutate(document):
            pack = document["reference_packs"][0]
            document["approved_hashes"][replacement_path] = replacement_hash
            pack["references"] = [
                {
                    "path": replacement_path,
                    "role": "identity_only",
                    "sha256": replacement_hash,
                }
            ]

        self.rewrite_registry(evidence, "style_reference_packs.json", mutate)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(
            any("reference_pack_id content mismatch" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any("prompt_reference_hash" in error for error in errors),
            errors,
        )

    def test_task_completion_actor_must_match_page_generator(self):
        evidence = self.make_complete_fixture()

        def mutate(document):
            document["tasks"][0]["completed_by"] = "different-generator"

        self.rewrite_registry(evidence, "task_queue.json", mutate)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("completed_by" in error for error in errors))

    def test_scene_mode_rejects_extra_completed_task_for_duplicate_page(self):
        evidence = self.make_complete_fixture(count=2)
        queue_path = evidence / "task_queue.json"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        queue.pop("registry_hash")
        first_page = json.loads(
            (evidence / "comic_run_manifest.json").read_text(encoding="utf-8")
        )["pages"][0]
        extra_payload_hash = canonical_hash({"purpose": "duplicate-page-regression"})
        extra = add_task(
            queue,
            page_id=first_page["output_name"],
            task_type="text_repair",
            payload_hash=extra_payload_hash,
            cluster_id=first_page["cluster_id"],
            prompt_reference_hash=extra_payload_hash,
            now="2026-07-13T13:00:00+08:00",
        )
        leased = claim_task(
            queue,
            worker="extra-worker",
            now="2026-07-13T13:01:00+08:00",
        )
        self.assertEqual(extra["task_id"], leased["task_id"])
        complete_task(
            queue,
            task_id=extra["task_id"],
            worker="extra-worker",
            candidate_path="extra-candidate/0001.jpg",
            candidate_hash=first_page["output_sha256"],
            now="2026-07-13T13:02:00+08:00",
        )
        atomic_write_json(queue_path, with_registry_hash(queue))

        errors = validate_project(self.root, evidence, 2)["errors"]
        self.assertTrue(
            any("task queue must map exactly one completed task per manifest page" in error for error in errors),
            errors,
        )

    def test_validator_accepts_task_queue_after_official_save(self):
        evidence = self.make_complete_fixture()
        queue_path = evidence / "task_queue.json"
        queue = load_queue(queue_path)
        self.assertEqual(
            save_queue(queue_path, queue, expected_revision=queue["revision"]),
            1,
        )
        self.assertEqual(queue, load_queue(queue_path))
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(result["ok"], result["errors"])

    def test_page_reviewer_must_match_page_qa_reviewer(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        repair = json.loads(path.read_text(encoding="utf-8"))
        repair["pages"][0]["page_qa"]["reviewer"] = "different-reviewer"
        atomic_write_json(path, repair)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("reviewed_by must match page_qa reviewer" in error for error in errors))

    def test_scene_cluster_qa_is_required(self):
        evidence = self.make_complete_fixture(3)
        (evidence / "scene_cluster_qa.json").unlink()
        errors = validate_project(self.root, evidence, 3)["errors"]
        self.assertTrue(any("scene_cluster_qa.json" in error for error in errors))

    def test_scene_cluster_qa_reviewer_is_independent(self):
        evidence = self.make_complete_fixture(3)
        self.rewrite_registry(
            evidence,
            "scene_cluster_qa.json",
            lambda document: document["clusters"][0].update(
                reviewed_by="fixture-worker"
            ),
        )
        errors = validate_project(self.root, evidence, 3)["errors"]
        self.assertTrue(any("cluster reviewer must be independent" in error for error in errors))

    def test_visual_validation_scans_input_pages_only_once(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with mock.patch.object(
                validator_module,
                "sorted_input_pages",
                wraps=validator_module.sorted_input_pages,
            ) as scan:
                result = validator_module.validate_project(self.root, evidence, 3)
        self.assertTrue(result["ok"], result["errors"])
        self.assertEqual(1, scan.call_count)
        self.assertFalse(
            any(issubclass(item.category, DeprecationWarning) for item in caught),
            [str(item.message) for item in caught],
        )

    def test_scene_mode_validator_rejects_worker_identity_mismatch_between_logs(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        repair = json.loads(path.read_text(encoding="utf-8"))
        repair["pages"][0]["generated_by"] = "other-worker"
        atomic_write_json(path, repair)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("generated_by mismatch" in error for error in errors))

    def test_scene_mode_validator_requires_nonempty_worker_identities(self):
        evidence = self.make_complete_fixture()
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pages"][0]["reviewed_by"] = None
            atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("reviewed_by missing" in error for error in errors))

    def test_scene_mode_processed_page_generator_cannot_review_own_page(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document["pages"][1].update(
                generated_by="same-worker",
                reviewed_by="same-worker",
            )
            atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 3)["errors"]
        self.assertTrue(any("independent review" in error for error in errors))

    def test_scene_mode_full_page_generator_cannot_review_own_page(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        repair_path = evidence / "repair_log.json"
        repair = json.loads(repair_path.read_text(encoding="utf-8"))
        page = repair["pages"][1]
        mask_path, mask_hash = self.add_style_artifact(
            evidence, "full-mask-independent.png", "full_mask"
        )
        page.update(
            visual_issue_extent="page_wide",
            visual_edit_mode="full_page_regeneration",
            visual_mask_path=mask_path,
            visual_mask_sha256=mask_hash,
            changed_region_ratio=1.0,
            full_page_regeneration_reason="page-wide contradiction",
            generated_by="same-worker",
            reviewed_by="same-worker",
        )
        page["style_review"]["full_page_exception_approved"] = True
        atomic_write_json(repair_path, repair)
        errors = validate_project(self.root, evidence, 3)["errors"]
        self.assertTrue(any("independent review" in error for error in errors))

    def test_legacy_fixture_without_pipeline_mode_remains_compatible(self):
        evidence = self.make_complete_fixture()
        new_fields = {
            "cluster_id", "reference_pack_id", "task_id", "failure_rule_ids",
            "generated_by", "reviewed_by",
        }
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document.pop("pipeline_mode", None)
            document.pop("evidence_files", None)
            document["schema_version"] = "2.0"
            for page in document["pages"]:
                for field in new_fields:
                    page.pop(field, None)
            atomic_write_json(path, document)
        for filename in (
            "scene_clusters.json",
            "style_reference_packs.json",
            "task_queue.json",
            "failure_learning.json",
            "scene_cluster_qa.json",
        ):
            (evidence / filename).unlink()
        self.assertEqual([], validate_project(self.root, evidence, 1)["errors"])

    def test_schema_three_missing_pipeline_mode_is_rejected(self):
        evidence = self.make_complete_fixture()
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document.pop("pipeline_mode", None)
            atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("schema_version 3.0 requires pipeline_mode" in error for error in errors))

    def test_schema_two_cannot_hide_new_mode_page_fields(self):
        evidence = self.make_complete_fixture()
        for filename in ("comic_run_manifest.json", "repair_log.json"):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document["schema_version"] = "2.0"
            document.pop("pipeline_mode", None)
            atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("legacy schema contains scene-cluster fields" in error for error in errors))

    def test_validator_rejects_noncontiguous_output_names(self):
        evidence = self.make_complete_fixture(count=2)
        (self.root / "输出" / "0002.jpg").rename(self.root / "输出" / "0003.jpg")
        result = validate_project(self.root, evidence, 2)
        self.assertFalse(result["ok"])
        self.assertTrue(any("contiguous" in error for error in result["errors"]))

    def test_validator_rejects_corrupt_output_image(self):
        evidence = self.make_complete_fixture()
        (self.root / "输出" / "0001.jpg").write_bytes(b"broken")
        self.assertFalse(validate_project(self.root, evidence, 1)["ok"])

    def test_validator_rejects_missing_output_hash(self):
        evidence = self.make_complete_fixture()
        path = evidence / "comic_run_manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["output_sha256"] = None
        atomic_write_json(path, document)
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(any("output hash" in error for error in result["errors"]))

    def test_validator_rejects_readable_unrelated_output_replacement(self):
        evidence = self.make_complete_fixture()
        Image.new("RGB", (32, 48), "red").save(self.root / "输出" / "0001.jpg")
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(any("output hash" in error for error in result["errors"]))

    def test_validator_rejects_changed_input_hash(self):
        evidence = self.make_complete_fixture()
        Image.new("RGB", (32, 48), "red").save(self.root / "输入" / "189.jpg")
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(any("input hash" in error for error in result["errors"]))

    def test_validator_rejects_incomplete_top_level_statuses(self):
        evidence = self.make_complete_fixture()
        for filename, status in (
            ("comic_run_manifest.json", "inventoried"),
            ("novel_alignment.json", "inventoried"),
            ("repair_log.json", "inventoried"),
        ):
            path = evidence / filename
            document = json.loads(path.read_text(encoding="utf-8"))
            document["status"] = status
            atomic_write_json(path, document)
        self.assertTrue(any("status" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_noncanonical_source_priority(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["source_priority"].reverse()
        atomic_write_json(path, document)
        self.assertTrue(any("source_priority" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_extra_continuity_lock_field(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["locks"][0]["source_priority"] = ["novel"]
        atomic_write_json(path, document)
        self.assertTrue(any("fields" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_arbitrary_text_reset_reason(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence, "repair_log.json", lambda page: page.update(text_reset_reason="随便写")
        )
        self.assertTrue(any("text_reset_reason" in error for error in result["errors"]))

    def test_validator_rejects_empty_final_report(self):
        evidence = self.make_complete_fixture()
        (evidence / "FINAL_QA_REPORT.md").write_text("", encoding="utf-8")
        self.assertTrue(any("final QA report" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_duplicate_final_report_status(self):
        evidence = self.make_complete_fixture()
        path = evidence / "FINAL_QA_REPORT.md"
        path.write_text(path.read_text(encoding="utf-8") + "status: passed\n", encoding="utf-8")
        self.assertTrue(any("final QA report format" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_reversed_final_report_keys(self):
        evidence = self.make_complete_fixture()
        path = evidence / "FINAL_QA_REPORT.md"
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("# FINAL QA REPORT\n\n" + "\n".join(reversed(lines[2:])) + "\n", encoding="utf-8")
        self.assertTrue(any("final QA report format" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_extra_final_report_key(self):
        evidence = self.make_complete_fixture()
        path = evidence / "FINAL_QA_REPORT.md"
        path.write_text(path.read_text(encoding="utf-8") + "extra_forged_key: yes\n", encoding="utf-8")
        self.assertTrue(any("final QA report format" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_unknown_final_report_text(self):
        evidence = self.make_complete_fixture()
        path = evidence / "FINAL_QA_REPORT.md"
        path.write_text(path.read_text(encoding="utf-8") + "一切都通过了\n", encoding="utf-8")
        self.assertTrue(any("final QA report format" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_extra_output_debug_file(self):
        evidence = self.make_complete_fixture()
        (self.root / "输出" / "debug.txt").write_text("debug", encoding="utf-8")
        self.assertTrue(any("output directory" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_incomplete_qa_check_set(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["page_qa"]["checks"] = {
            name: True for name in manifest_module.PAGE_QA_CHECKS[:6]
        }
        atomic_write_json(path, document)
        self.assertTrue(any("checks incomplete" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_wrong_batch_count_for_98_pages(self):
        evidence = self.make_complete_fixture(count=98)
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"].append(dict(document["batches"][-1], batch_id="batch-010"))
        atomic_write_json(path, document)
        self.assertTrue(any("batch count" in error for error in validate_project(self.root, evidence, 98)["errors"]))

    def test_validator_rejects_external_candidate_fake_prefix(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(
                used_external_text_resource=True,
                external_candidate={
                    "run_dir": r"D:\漫画文字修复\输出\fake_run",
                    "source_path": r"D:\漫画文字修复\输出\fake_run\final\0001.jpg",
                    "source_sha256": page["candidate_sha256"],
                    "imported_at": "2026-07-13T12:00:00+08:00",
                    "imported_by": "operator",
                    "status": "supervised_import",
                },
            ),
        )
        self.assertTrue(any("external_candidate" in error for error in result["errors"]))

    def test_validator_rejects_external_candidate_hash_break(self):
        evidence = self.make_complete_fixture()
        run_dir = Path(r"D:\漫画文字修复\输出\repair-comic-continuity_20990101_010101")
        final_dir = run_dir / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, run_dir, True)
        source = final_dir / "0001.jpg"
        shutil.copy2(self.root / "输出" / "0001.jpg", source)
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(
                used_external_text_resource=True,
                external_candidate={
                    "run_dir": str(run_dir),
                    "source_path": str(source),
                    "source_sha256": "0" * 64,
                    "imported_at": "2026-07-13T12:00:00+08:00",
                    "imported_by": "operator",
                    "status": "supervised_import",
                },
            ),
        )
        self.assertTrue(any("external_candidate" in error for error in result["errors"]))

    def test_validator_rejects_empty_continuity_bible(self):
        evidence = self.make_complete_fixture()
        atomic_write_json(evidence / "continuity_bible.json", {})
        self.assertTrue(any("continuity bible" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_missing_continuity_coverage(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["coverage"].pop("prop")
        atomic_write_json(path, document)
        self.assertTrue(any("coverage" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_forged_continuity_lock(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["locks"][0]["source_type"] = "fabricated"
        atomic_write_json(path, document)
        self.assertTrue(any("continuity lock" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_unconfirmed_continuity_lock(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["locks"][0]["confirmed"] = False
        atomic_write_json(path, document)
        self.assertTrue(any("confirmed" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_lock_missing_review_contract(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["locks"][0].update({"reviewer": "", "reviewed_at": "2026-07-13T12:00:00"})
        atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("reviewer" in error for error in errors))
        self.assertTrue(any("reviewed_at" in error for error in errors))

    def test_validator_rejects_coverage_fake_lock_id(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["coverage"]["character"]["lock_ids"] = ["fake-lock"]
        atomic_write_json(path, document)
        self.assertTrue(any("coverage" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_coverage_cross_category_lock(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["coverage"]["character"]["lock_ids"] = ["lock-extra"]
        atomic_write_json(path, document)
        self.assertTrue(any("category" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_confirmed_coverage_without_lock_ids(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["coverage"]["character"]["lock_ids"] = []
        atomic_write_json(path, document)
        self.assertTrue(any("lock_ids" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_page_without_continuity_lock_reference(self):
        evidence = self.make_complete_fixture()
        path = evidence / "comic_run_manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["continuity_lock_ids"] = []
        atomic_write_json(path, document)
        self.assertTrue(any("continuity_lock_ids" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_run_and_repair_lock_reference_mismatch(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["continuity_lock_ids"] = ["lock-character"]
        atomic_write_json(path, document)
        self.assertTrue(any("continuity_lock_ids mismatch" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_lock_not_applying_to_referencing_page(self):
        evidence = self.make_complete_fixture()
        path = evidence / "continuity_bible.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["locks"][0]["applies_to_pages"] = ["9999.jpg"]
        atomic_write_json(path, document)
        self.assertTrue(any("does not apply" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_unfinished_state(self):
        evidence = self.make_complete_fixture()
        path = evidence / "comic_run_manifest.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["state"] = "inventoried"
        atomic_write_json(path, document)
        self.assertTrue(any("state" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_unconfirmed_novel_alignment(self):
        evidence = self.make_complete_fixture()
        path = evidence / "novel_alignment.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0].update({"status": "unconfirmed", "evidence": ""})
        atomic_write_json(path, document)
        self.assertTrue(any("novel alignment" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_missing_novel_alignment_field(self):
        evidence = self.make_complete_fixture()
        path = evidence / "novel_alignment.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["chapter"] = ""
        atomic_write_json(path, document)
        self.assertTrue(any("chapter" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_empty_scene_summary(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence, "novel_alignment.json", lambda page: page.update(scene_summary="")
        )
        self.assertTrue(any("scene_summary" in error for error in result["errors"]))

    def test_validator_rejects_empty_involved_characters(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence, "novel_alignment.json", lambda page: page.update(involved_characters=[])
        )
        self.assertTrue(any("involved_characters" in error for error in result["errors"]))

    def test_validator_rejects_invalid_dialogue_owners_list(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence, "novel_alignment.json", lambda page: page.update(dialogue_owners=[1])
        )
        self.assertTrue(any("dialogue_owners" in error for error in result["errors"]))

    def test_validator_rejects_invalid_props_list(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence, "novel_alignment.json", lambda page: page.update(props="道具")
        )
        self.assertTrue(any("props" in error for error in result["errors"]))

    def test_validator_rejects_empty_location_and_story_time(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "novel_alignment.json",
            lambda page: page.update(location="", story_time=""),
        )
        self.assertTrue(any("location" in error for error in result["errors"]))
        self.assertTrue(any("story_time" in error for error in result["errors"]))

    def test_validator_rejects_forged_novel_hash(self):
        evidence = self.make_complete_fixture()
        path = evidence / "novel_alignment.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["novel_sha256"] = "0" * 64
        atomic_write_json(path, document)
        self.assertTrue(any("novel_sha256" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_invalid_novel_offsets(self):
        evidence = self.make_complete_fixture()
        path = evidence / "novel_alignment.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0].update({"start_offset": -1, "end_offset": 9999})
        atomic_write_json(path, document)
        self.assertTrue(any("offset" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_excerpt_not_equal_to_novel_slice(self):
        evidence = self.make_complete_fixture()
        path = evidence / "novel_alignment.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["source_excerpt"] = "伪造摘录"
        atomic_write_json(path, document)
        self.assertTrue(any("source_excerpt" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_out_of_range_confidence(self):
        evidence = self.make_complete_fixture()
        path = evidence / "novel_alignment.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["confidence"] = 1.5
        atomic_write_json(path, document)
        self.assertTrue(any("confidence" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_wrong_candidate_hash(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["candidate_sha256"] = "0" * 64
        atomic_write_json(path, document)
        self.assertTrue(any("candidate" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_wrong_text_snapshot_hash(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["text_snapshot_sha256"] = "0" * 64
        atomic_write_json(path, document)
        self.assertTrue(any("text snapshot" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_unsafe_candidate_path(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["candidate_path"] = str(self.root.parent / "outside.jpg")
        atomic_write_json(path, document)
        self.assertTrue(any("candidate" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_same_page_and_batch_pass_id(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["batch_qa"]["pass_id"] = document["pages"][0]["page_qa"]["pass_id"]
        atomic_write_json(path, document)
        self.assertTrue(any("pass_id" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_missing_visual_review_check(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["visual_review"]["checks"].pop(manifest_module.VISUAL_REVIEW_CHECKS[0])
        atomic_write_json(path, document)
        self.assertTrue(any("visual_review checks" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_missing_page_qa_check(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["page_qa"]["checks"].pop(manifest_module.PAGE_QA_CHECKS[0])
        atomic_write_json(path, document)
        self.assertTrue(any("page_qa checks" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_missing_batch_qa_check(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["batch_qa"]["checks"].pop(manifest_module.BATCH_QA_CHECKS[0])
        atomic_write_json(path, document)
        self.assertTrue(any("batch_qa checks" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_invalid_action_and_text_reset_mode(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0].update({"action": "fabricated", "text_reset_mode": "always_page"})
        atomic_write_json(path, document)
        errors = validate_project(self.root, evidence, 1)["errors"]
        self.assertTrue(any("action" in error for error in errors))
        self.assertTrue(any("text_reset_mode" in error for error in errors))

    def test_validator_accepts_each_valid_repair_strategy(self):
        strategies = (
            {"action": "edited", "repair_scope": "visual", "had_ordinary_text": True, "text_recognition": "reliable", "text_reset_mode": "page", "text_reset_reason": "visual_repair_requires_page_reset"},
            {"action": "edited", "repair_scope": "visual", "had_ordinary_text": False, "text_recognition": "not_applicable", "text_reset_mode": "none", "text_reset_reason": "no_ordinary_text"},
            {"action": "edited", "repair_scope": "text_only", "had_ordinary_text": True, "text_recognition": "reliable", "text_reset_mode": "block", "text_reset_reason": "reliable_block_match"},
            {"action": "edited", "repair_scope": "text_only", "had_ordinary_text": True, "text_recognition": "unreliable", "text_reset_mode": "page", "text_reset_reason": "unreliable_text_page_reset"},
        )
        for strategy in strategies:
            with self.subTest(strategy=strategy):
                evidence = self.make_complete_fixture(count=3)
                try:
                    if strategy["repair_scope"] == "visual":
                        self.make_localized_visual_repair(evidence, page_index=1)
                    result = self.mutate_page(
                        evidence,
                        "repair_log.json",
                        lambda page, value=strategy: page.update(value),
                        page_index=1,
                    )
                    self.assertTrue(result["ok"], result["errors"])
                finally:
                    shutil.rmtree(evidence, ignore_errors=True)

    def test_validator_rejects_unchanged_copy_with_wrong_policy(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(repair_scope="visual", text_recognition="reliable"),
        )
        self.assertTrue(any("repair policy" in error for error in result["errors"]))

    def test_validator_rejects_visual_text_page_without_page_reset(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(action="edited", repair_scope="visual", had_ordinary_text=True, text_recognition="reliable", text_reset_mode="block"),
        )
        self.assertTrue(any("repair policy" in error for error in result["errors"]))

    def test_validator_rejects_visual_page_without_text_using_reset(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(action="edited", repair_scope="visual", had_ordinary_text=False, text_recognition="not_applicable", text_reset_mode="page"),
        )
        self.assertTrue(any("repair policy" in error for error in result["errors"]))

    def test_validator_rejects_text_only_policy_mismatch(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(action="edited", repair_scope="text_only", had_ordinary_text=True, text_recognition="unreliable", text_reset_mode="block"),
        )
        self.assertTrue(any("repair policy" in error for error in result["errors"]))

    def test_validator_rejects_empty_text_reset_reason(self):
        evidence = self.make_complete_fixture()
        result = self.mutate_page(
            evidence, "repair_log.json", lambda page: page.update(text_reset_reason="")
        )
        self.assertTrue(any("text_reset_reason" in error for error in result["errors"]))

    def test_validator_rejects_missing_reviewers(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["visual_review"]["reviewer"] = ""
        document["pages"][0]["page_qa"]["reviewer"] = ""
        document["batches"][0]["batch_qa"]["reviewer"] = ""
        atomic_write_json(path, document)
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(any("visual_review" in error and "reviewer" in error for error in result["errors"]))
        self.assertTrue(any("page_qa" in error and "reviewer" in error for error in result["errors"]))
        self.assertTrue(any("batch_qa" in error and "reviewer" in error for error in result["errors"]))

    def test_validator_rejects_review_times_without_timezone(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["visual_review"]["reviewed_at"] = "2026-07-13T12:10:00"
        document["pages"][0]["page_qa"]["reviewed_at"] = "not-a-time"
        document["batches"][0]["batch_qa"]["reviewed_at"] = "2026-07-13T12:30:00"
        atomic_write_json(path, document)
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(any("reviewed_at" in error for error in result["errors"]))

    def test_validator_rejects_duplicate_pass_id_across_pages(self):
        evidence = self.make_complete_fixture(count=2)
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][1]["page_qa"]["pass_id"] = document["pages"][0]["page_qa"]["pass_id"]
        atomic_write_json(path, document)
        self.assertTrue(any("globally unique" in error for error in validate_project(self.root, evidence, 2)["errors"]))

    def test_validator_rejects_batch_review_not_later_than_page_review(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["batch_qa"]["reviewed_at"] = document["pages"][0]["page_qa"]["reviewed_at"]
        atomic_write_json(path, document)
        result = validate_project(self.root, evidence, 1)
        self.assertTrue(any("strictly later" in error for error in result["errors"]))

    def test_validator_rejects_batch_missing_page(self):
        evidence = self.make_complete_fixture(count=2)
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["member_pages"].pop()
        atomic_write_json(path, document)
        self.assertTrue(any("batch membership" in error for error in validate_project(self.root, evidence, 2)["errors"]))

    def test_validator_rejects_duplicate_batch_membership(self):
        evidence = self.make_complete_fixture(count=2)
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["member_pages"].append("0001.jpg")
        atomic_write_json(path, document)
        self.assertTrue(any("batch membership" in error for error in validate_project(self.root, evidence, 2)["errors"]))

    def test_validator_rejects_wrong_batch_context(self):
        evidence = self.make_complete_fixture(count=2)
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["context_after"] = ["9999.jpg"]
        atomic_write_json(path, document)
        self.assertTrue(any("batch context" in error for error in validate_project(self.root, evidence, 2)["errors"]))

    def test_validator_rejects_failed_page_qa(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["pages"][0]["page_qa"]["status"] = "failed"
        atomic_write_json(path, document)
        self.assertTrue(any("page/batch QA" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_failed_batch_qa(self):
        evidence = self.make_complete_fixture()
        path = evidence / "repair_log.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["batches"][0]["batch_qa"]["status"] = "failed"
        atomic_write_json(path, document)
        self.assertTrue(any("page/batch QA" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_final_report_without_passed_status(self):
        evidence = self.make_complete_fixture()
        (evidence / "FINAL_QA_REPORT.md").write_text("# Final QA\n\nstatus: failed\n", encoding="utf-8")
        self.assertTrue(any("final QA report status" in error for error in validate_project(self.root, evidence, 1)["errors"]))

    def test_validator_rejects_full_page_regeneration_for_localized_issue(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(
                visual_edit_mode="full_page_regeneration",
                full_page_regeneration_reason="更容易统一人物",
            ),
            page_index=1,
        )
        self.assertTrue(
            any("full-page regeneration requires page-wide issue" in error for error in result["errors"])
        )

    def test_validator_rejects_character_reference_as_style_source(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(character_reference_role="primary_style"),
            page_index=1,
        )
        self.assertTrue(any("character_reference_role" in error for error in result["errors"]))

    def test_validator_rejects_visual_repair_without_mask_or_comparison(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        result = self.mutate_page(
            evidence,
            "repair_log.json",
            lambda page: page.update(visual_mask_path=None, style_review={}),
            page_index=1,
        )
        self.assertTrue(any("visual mask" in error or "style_review" in error for error in result["errors"]))

    def test_validator_accepts_complete_localized_style_evidence(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        self.assertEqual([], validate_project(self.root, evidence, 3)["errors"])

    def test_validator_rejects_unapproved_page_wide_regeneration(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        repair_path = evidence / "repair_log.json"
        repair = json.loads(repair_path.read_text(encoding="utf-8"))
        page = repair["pages"][1]
        mask_path, mask_hash = self.add_style_artifact(evidence, "full-mask.png", "full_mask")
        page.update(
            visual_issue_extent="page_wide",
            visual_edit_mode="full_page_regeneration",
            visual_mask_path=mask_path,
            visual_mask_sha256=mask_hash,
            changed_region_ratio=1.0,
            full_page_regeneration_reason="整页场景与小说地点不符，局部修复无法保留构图",
        )
        atomic_write_json(repair_path, repair)
        result = validate_project(self.root, evidence, 3)
        self.assertTrue(any("full-page regeneration approval missing" in error for error in result["errors"]))

    def test_validator_accepts_approved_page_wide_regeneration(self):
        evidence = self.make_complete_fixture(3)
        self.make_localized_visual_repair(evidence, page_index=1)
        repair_path = evidence / "repair_log.json"
        repair = json.loads(repair_path.read_text(encoding="utf-8"))
        page = repair["pages"][1]
        mask_path, mask_hash = self.add_style_artifact(evidence, "full-mask.png", "full_mask")
        page.update(
            visual_issue_extent="page_wide",
            visual_edit_mode="full_page_regeneration",
            visual_mask_path=mask_path,
            visual_mask_sha256=mask_hash,
            changed_region_ratio=1.0,
            full_page_regeneration_reason="整页场景与小说地点不符，局部修复无法保留构图",
        )
        page["style_review"]["full_page_exception_approved"] = True
        atomic_write_json(repair_path, repair)
        self.assertEqual([], validate_project(self.root, evidence, 3)["errors"])


if __name__ == "__main__":
    unittest.main()
