import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import scene_clusters  # noqa: E402
from pipeline_contracts import canonical_hash  # noqa: E402


def make_page(page_id, **overrides):
    page = {
        "page_id": str(page_id),
        "chapter": "chapter-1",
        "location": "courtyard",
        "story_time": "day",
        "scene_id": "scene-a",
    }
    page.update(overrides)
    return page


def reviewed(reference_id):
    return {
        "status": "passed",
        "full_size": True,
        "reviewer": "reviewer-b",
        "reviewed_at": "2026-07-15T10:00:00+08:00",
        "evidence_path": f"evidence/reviews/{reference_id}.json",
        "evidence_sha256": "e" * 64,
    }


def visual_cluster(**overrides):
    cluster = {
        "cluster_id": "cluster-a",
        "member_pages": ["场景/0189.png"],
        "cast": ["邓正虎", "天朗真人"],
        "has_visual_tasks": True,
        "visual_targets": ["场景/0189.png"],
        "canary_page": "场景/0189.png",
        "repair_characters": ["邓正虎", "天朗真人"],
        "issue_schedule": ["character_identity"],
    }
    cluster.update(overrides)
    return cluster


def complete_references():
    return [
        {
            "path": "输入/场景/0189.png",
            "role": "target_composition",
            "subject": "场景/0189.png",
            "source": "immutable_input",
            "sha256": "a" * 64,
        },
        {
            "path": "输入/稳定页/0188.png",
            "role": "comic_style_anchor",
            "subject": "comic_style",
            "source": "reviewed_comic_page",
            "review": reviewed("style-review-1"),
            "sha256": "b" * 64,
        },
        {
            "path": "人物参考图/邓正虎.png",
            "role": "identity_only",
            "subject": "邓正虎",
            "source": "character_sheet",
            "review": reviewed("identity-review-1"),
            "sha256": "c" * 64,
        },
        {
            "path": "人物参考图/天朗真人.png",
            "role": "identity_only",
            "subject": "天朗真人",
            "source": "character_sheet",
            "review": reviewed("identity-review-2"),
            "sha256": "d" * 64,
        },
    ]


class SceneKeyTests(unittest.TestCase):
    def test_scene_key_normalizes_missing_and_blank_values(self):
        self.assertEqual(
            scene_clusters.scene_key({"chapter": "  ", "location": None}),
            ("unknown", "unknown", "unknown", "unknown"),
        )

    def test_scene_key_is_ordered_by_named_fields(self):
        self.assertEqual(
            scene_clusters.scene_key(
                {
                    "scene_id": "scene-a",
                    "story_time": "day",
                    "location": "courtyard",
                    "chapter": "chapter-1",
                }
            ),
            ("chapter-1", "courtyard", "day", "scene-a"),
        )


class SceneClusterTests(unittest.TestCase):
    def test_unknown_semantics_are_single_page_unresolved_blockers_not_fixed_batches(self):
        pages = [
            {
                "page_id": str(index),
                "chapter": None,
                "location": None,
                "story_time": None,
                "scene_id": None,
            }
            for index in range(1, 46)
        ]

        clusters = scene_clusters.build_scene_clusters(pages)

        self.assertEqual(len(clusters), 45)
        self.assertTrue(all(len(row["member_pages"]) == 1 for row in clusters))
        self.assertTrue(all(row["blocked"] for row in clusters))
        self.assertTrue(all(row["semantic_status"] == "unresolved" for row in clusters))
        self.assertTrue(
            all("UNRESOLVED_SEMANTIC_BOUNDARY" in row["blocker_codes"] for row in clusters)
        )
        self.assertTrue(
            all(
                row["boundary_reason"]
                == {
                    "start": ["unresolved_semantic_boundary"],
                    "end": ["unresolved_semantic_boundary"],
                }
                for row in clusters
            )
        )

    def test_cluster_size_controls_distinguish_semantic_short_from_legacy_fragment(self):
        normal = scene_clusters.cluster_size_controls(20, 10, False)
        self.assertFalse(normal["undersized"])
        self.assertFalse(normal["blocked"])
        self.assertIsNone(normal["short_scene_reason"])

        semantic = scene_clusters.cluster_size_controls(
            13, 6, False, semantically_bounded=True
        )
        self.assertTrue(semantic["undersized"])
        self.assertFalse(semantic["blocked"])
        self.assertEqual(
            semantic["short_scene_reason"],
            "semantic_scene_below_preferred_size",
        )

        legacy_fragment = scene_clusters.cluster_size_controls(13, 6, False)
        self.assertTrue(legacy_fragment["blocked"])
        self.assertIn("UNDERSIZED_CLUSTER", legacy_fragment["blocker_codes"])

    def test_short_semantic_scene_is_allowed_without_cross_scene_merge(self):
        pages = [
            make_page("189", chapter="chapter-ten", location="飞龙泉", story_time="夜", scene_id="泉中"),
            make_page("190", chapter="chapter-ten", location="飞龙泉", story_time="夜", scene_id="泉中"),
            make_page("191", chapter="chapter-ten", location="一道宗", story_time="晨", scene_id="大殿"),
        ]

        clusters = scene_clusters.build_scene_clusters(pages)

        self.assertEqual(
            [row["member_pages"] for row in clusters],
            [["189", "190"], ["191"]],
        )
        self.assertFalse(any(row["blocked"] for row in clusters))
        self.assertEqual(
            clusters[0]["short_scene_reason"],
            "semantic_scene_below_preferred_size",
        )

    def test_semantic_boundaries_include_cast_props_costume_and_transition(self):
        base = {
            "cast": ["邓正虎"],
            "persistent_props": ["狼毫笔"],
            "costume_state": {"邓正虎": "青袍"},
        }
        pages = [
            make_page("1", **base),
            make_page("2", **base),
            make_page("3", **{**base, "cast": ["邓正虎", "天朗真人"]}),
            make_page("4", **{**base, "explicit_transition": "翌日"}),
        ]

        clusters = scene_clusters.build_scene_clusters(pages, min_size=8)

        self.assertEqual([row["member_pages"] for row in clusters], [["1", "2"], ["3"], ["4"]])
        for cluster in clusters:
            for field in (
                "cast",
                "persistent_props",
                "costume_state",
                "scene_fingerprint",
                "boundary_reason",
                "confidence",
                "has_visual_tasks",
            ):
                self.assertIn(field, cluster)
            self.assertFalse(cluster["blocked"])

    def test_scene_fingerprint_binds_explicit_transition(self):
        base = make_page("1", cast=["邓正虎"])
        before = scene_clusters.build_scene_clusters([base], min_size=1)[0]
        after = scene_clusters.build_scene_clusters(
            [{**base, "explicit_transition": "翌日清晨"}], min_size=1
        )[0]
        self.assertNotEqual(before["scene_fingerprint"], after["scene_fingerprint"])

    def test_long_semantic_scene_may_split_without_crossing_semantic_boundary(self):
        pages = [make_page(index) for index in range(1, 21)]

        clusters = scene_clusters.build_scene_clusters(pages, min_size=8, max_size=10)

        self.assertEqual([len(row["member_pages"]) for row in clusters], [10, 10])
        self.assertEqual(clusters[0]["context_after"], ["11", "12"])
        self.assertEqual(clusters[1]["context_before"], ["9", "10"])

    def test_context_never_duplicates_owned_member_pages(self):
        clusters = scene_clusters.build_scene_clusters(
            [make_page(index) for index in range(1, 13)],
            min_size=3,
            max_size=4,
            context_radius=2,
        )
        for cluster in clusters:
            members = set(cluster["member_pages"])
            self.assertTrue(members.isdisjoint(cluster["context_before"]))
            self.assertTrue(members.isdisjoint(cluster["context_after"]))

    def test_canary_is_none_without_visual_tasks(self):
        cluster = scene_clusters.build_scene_clusters(
            [make_page("1", risk_score=99), make_page("2", risk_score=1)],
            min_size=1,
        )[0]

        self.assertFalse(cluster["has_visual_tasks"])
        self.assertIsNone(cluster["canary_page"])

    def test_canary_uses_highest_risk_visual_member_and_story_order_tie_break(self):
        pages = [
            make_page("1", has_visual_task=False, risk_score=100),
            make_page("2", has_visual_task=True, risk_score=9),
            make_page("3", has_visual_task=True, risk_score=9),
            make_page("4", has_visual_task=True, risk_score=2),
        ]

        cluster = scene_clusters.build_scene_clusters(pages, min_size=1)[0]

        self.assertEqual(cluster["canary_page"], "2")
        self.assertEqual(cluster["visual_targets"], ["2", "3", "4"])

    def test_nonfinite_visual_risk_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "risk_score"):
            scene_clusters.build_scene_clusters(
                [make_page("1", has_visual_task=True, risk_score=float("nan"))],
                min_size=1,
            )

    def test_relative_unicode_page_names_are_preserved_and_deterministic(self):
        pages = [
            make_page("ignored-1", relative_path="章节十/0189（1）.webp"),
            make_page("ignored-2", relative_path="章节十/0190.png"),
        ]

        first = scene_clusters.build_scene_clusters(pages, min_size=1)
        second = scene_clusters.build_scene_clusters([dict(page) for page in pages], min_size=1)

        self.assertEqual(first, second)
        self.assertEqual(first[0]["member_pages"], ["章节十/0189（1）.webp", "章节十/0190.png"])
        self.assertTrue(first[0]["cluster_id"].startswith("cluster-"))
        self.assertIsNone(first[0]["reference_pack_id"])
        self.assertEqual(first[0]["reference_pack_state"], "unbound")

    def test_duplicate_case_insensitive_relative_page_names_are_rejected(self):
        pages = [
            make_page("1", relative_path="章/A.JPG"),
            make_page("2", relative_path="章/a.jpg"),
        ]
        with self.assertRaisesRegex(scene_clusters.SceneClusterContractError, "duplicate") as raised:
            scene_clusters.build_scene_clusters(pages, min_size=1)
        self.assertEqual(raised.exception.code, "DUPLICATE_PAGE_REFERENCE")

    def test_invalid_cluster_parameters_are_rejected(self):
        for kwargs in (
            {"min_size": 0},
            {"max_size": 0},
            {"min_size": 4, "max_size": 3},
            {"context_radius": -1},
            {"context_radius": 3},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    scene_clusters.build_scene_clusters([make_page("1")], **kwargs)


class ReferencePackTests(unittest.TestCase):
    def test_generated_candidate_is_forbidden_for_identity_prop_and_scene_roles(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        identity_refs = complete_references()
        identity_refs[2] = {
            **identity_refs[2],
            "source": "generated_candidate",
        }
        cases = [
            (visual_cluster(), identity_refs, "identity_only source"),
            (
                visual_cluster(
                    issue_schedule=[{"type": "prop continuity", "subject": "狼毫笔"}],
                    persistent_props=["狼毫笔"],
                ),
                complete_references()
                + [
                    {
                        "path": "输入/稳定页/狼毫笔.png",
                        "role": "prop_anchor",
                        "subject": "狼毫笔",
                        "source": "generated_candidate",
                        "review": reviewed("prop-review"),
                        "sha256": "f" * 64,
                    }
                ],
                "prop_anchor source",
            ),
            (
                visual_cluster(
                    issue_schedule=[{"type": "scene consistency", "subject": "飞龙泉"}],
                    scene_key=["chapter-ten", "飞龙泉", "夜", "泉中"],
                ),
                complete_references()
                + [
                    {
                        "path": "输入/稳定页/飞龙泉.png",
                        "role": "scene_anchor",
                        "subject": "飞龙泉",
                        "source": "generated_candidate",
                        "review": reviewed("scene-review"),
                        "sha256": "f" * 64,
                    }
                ],
                "scene_anchor source",
            ),
        ]
        for cluster, references, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    scene_clusters.build_reference_pack(
                        cluster, references, stable_pages=stable_pages
                    )

    def test_required_prop_and_scene_subjects_need_exact_complete_anchor_coverage(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        prop_anchor = {
            "path": "输入/稳定页/狼毫笔.png",
            "role": "prop_anchor",
            "subject": "狼毫笔",
            "source": "reviewed_prop_appearance",
            "review": reviewed("prop-review"),
            "sha256": "f" * 64,
        }
        scene_anchor = {
            "path": "输入/稳定页/飞龙泉.png",
            "role": "scene_anchor",
            "subject": "飞龙泉",
            "source": "reviewed_scene_appearance",
            "review": reviewed("scene-review"),
            "sha256": "f" * 64,
        }
        cases = (
            (
                visual_cluster(
                    issue_schedule=[
                        {"type": "prop continuity", "subject": "狼毫笔"},
                        {"type": "prop continuity", "subject": "墨锭"},
                    ],
                    persistent_props=["狼毫笔", "墨锭"],
                ),
                prop_anchor,
                "prop_anchor.*complete",
            ),
            (
                visual_cluster(
                    issue_schedule=[
                        {"type": "scene consistency", "subject": "飞龙泉"},
                        {"type": "scene consistency", "subject": "一道宗大殿"},
                    ]
                ),
                scene_anchor,
                "scene_anchor.*complete",
            ),
        )
        for cluster, anchor, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    scene_clusters.build_reference_pack(
                        cluster,
                        complete_references() + [anchor],
                        stable_pages=stable_pages,
                    )

    def test_explicit_issue_subjects_override_broader_persistent_prop_fallback(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        prop_anchor = {
            "path": "输入/稳定页/狼毫笔.png",
            "role": "prop_anchor",
            "subject": "狼毫笔",
            "source": "reviewed_prop_appearance",
            "review": reviewed("prop-review"),
            "sha256": "f" * 64,
        }
        pack = scene_clusters.build_reference_pack(
            visual_cluster(
                issue_schedule=[{"type": "prop continuity", "subject": "狼毫笔"}],
                persistent_props=["狼毫笔", "墨锭"],
            ),
            complete_references() + [prop_anchor],
            stable_pages=stable_pages,
        )
        self.assertEqual(pack["cluster_id"], "cluster-a")

    def test_visual_cluster_requires_nonempty_canary_member(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        with self.assertRaisesRegex(ValueError, "canary_page"):
            scene_clusters.build_reference_pack(
                visual_cluster(canary_page=None),
                complete_references(),
                stable_pages=stable_pages,
            )

    def test_false_visual_cluster_cannot_retain_visual_evidence_or_schedule(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        mutations = (
            {"visual_targets": ["场景/0189.png"]},
            {"canary_page": "场景/0189.png", "visual_targets": []},
            {
                "issue_schedule": [{"type": "prop continuity", "subject": "狼毫笔"}],
                "visual_targets": [],
            },
            {"has_visual_task": True, "visual_targets": []},
        )
        for mutation in mutations:
            cluster = visual_cluster()
            cluster.update(
                has_visual_tasks=False,
                visual_targets=[],
                canary_page=None,
                issue_schedule=[],
            )
            cluster.update(mutation)
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, "has_visual_tasks=false|visual flag"):
                    scene_clusters.build_reference_pack(
                        cluster, complete_references(), stable_pages=stable_pages
                    )

    def test_extra_target_and_unstable_style_rows_are_rejected_individually(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        extra_target = {
            "path": "输入/场景/9999.png",
            "role": "target_composition",
            "subject": "场景/9999.png",
            "source": "immutable_input",
            "sha256": "f" * 64,
        }
        extra_style = {
            "path": "输入/稳定页/0999.png",
            "role": "comic_style_anchor",
            "subject": "comic_style",
            "source": "reviewed_comic_page",
            "review": reviewed("unstable-style"),
            "sha256": "f" * 64,
        }
        extra_identity = {
            "path": "人物参考图/无关人物.png",
            "role": "identity_only",
            "subject": "无关人物",
            "source": "character_sheet",
            "review": reviewed("unrelated-identity"),
            "sha256": "9" * 64,
        }
        for extra, message in (
            (extra_target, "target_composition.*exact"),
            (extra_style, "comic_style_anchor.*stable"),
            (extra_identity, "identity_only.*cast"),
        ):
            with self.subTest(role=extra["role"]):
                with self.assertRaisesRegex(ValueError, message):
                    scene_clusters.build_reference_pack(
                        visual_cluster(),
                        complete_references() + [extra],
                        stable_pages=stable_pages,
                    )

    def test_unrequested_or_wrong_subject_prop_and_scene_anchors_are_rejected(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        prop_anchor = {
            "path": "输入/稳定页/狼毫笔.png",
            "role": "prop_anchor",
            "subject": "错误道具",
            "source": "reviewed_prop_page",
            "review": reviewed("prop-review"),
            "sha256": "f" * 64,
        }
        scene_anchor = {
            "path": "输入/稳定页/飞龙泉.png",
            "role": "scene_anchor",
            "subject": "错误场景",
            "source": "reviewed_scene_page",
            "review": reviewed("scene-review"),
            "sha256": "f" * 64,
        }
        for anchor in (prop_anchor, scene_anchor):
            with self.subTest(role=anchor["role"]):
                with self.assertRaisesRegex(ValueError, "unexpected"):
                    scene_clusters.build_reference_pack(
                        visual_cluster(issue_schedule=["character_identity"]),
                        complete_references() + [anchor],
                        stable_pages=stable_pages,
                    )

        with self.assertRaisesRegex(ValueError, "prop_anchor subject"):
            scene_clusters.build_reference_pack(
                visual_cluster(
                    issue_schedule=[{"type": "prop continuity", "subject": "狼毫笔"}],
                    persistent_props=["狼毫笔"],
                ),
                complete_references() + [prop_anchor],
                stable_pages=stable_pages,
            )

    def test_public_v4_validation_requires_cluster_for_coverage(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        with self.assertRaisesRegex(ValueError, "V4.*cluster"):
            scene_clusters.validate_reference_pack(
                {"references": complete_references()},
                stable_pages=stable_pages,
            )

    def test_non_string_roles_are_structurally_rejected_before_set_operations(self):
        for role in ([], {}):
            with self.subTest(role=role):
                with self.assertRaises(scene_clusters.SceneClusterContractError) as raised:
                    scene_clusters.build_reference_pack(
                        visual_cluster(),
                        [{"path": "输入/场景/0189.png", "role": role}],
                    )
                self.assertEqual(raised.exception.code, "INVALID_REFERENCE_ROLE")

    def test_v4_missing_trace_fields_never_falls_back_to_legacy(self):
        reference = {
            "path": "人物参考图/邓正虎.png",
            "role": "identity_only",
            "sha256": "a" * 64,
        }
        with self.assertRaises(scene_clusters.SceneClusterContractError) as raised:
            scene_clusters.build_reference_pack(visual_cluster(), [reference])
        self.assertEqual(raised.exception.code, "MISSING_REFERENCE_FIELD")

    def test_mixed_v3_v4_reference_records_are_structurally_rejected(self):
        references = complete_references() + [
            {"path": "legacy/0188.png", "role": "adjacent_style", "sha256": "f" * 64}
        ]
        with self.assertRaises(scene_clusters.SceneClusterContractError) as raised:
            scene_clusters.build_reference_pack(visual_cluster(), references)
        self.assertEqual(raised.exception.code, "MIXED_REFERENCE_SCHEMA")

        with self.assertRaises(scene_clusters.SceneClusterContractError) as raised:
            scene_clusters.build_reference_pack(
                {"cluster_id": "legacy-cluster"},
                [complete_references()[2]],
                contract_version="v3",
            )
        self.assertEqual(raised.exception.code, "MIXED_REFERENCE_SCHEMA")

    def test_scene_pipeline_documents_explicit_reference_pack_binding(self):
        reference = (
            Path(__file__).resolve().parents[1]
            / "references"
            / "scene-cluster-pipeline.md"
        ).read_text(encoding="utf-8")
        for term in (
            "reference_pack_state",
            "bind_reference_pack",
            "unbound",
            "bound",
            "rebind",
        ):
            with self.subTest(term=term):
                self.assertIn(term, reference)

    def test_v4_reference_roles_are_exact(self):
        self.assertEqual(
            scene_clusters.REFERENCE_ROLES,
            frozenset(
                {
                    "target_composition",
                    "comic_style_anchor",
                    "identity_only",
                    "prop_anchor",
                    "scene_anchor",
                }
            ),
        )

    def test_visual_reference_pack_requires_complete_cast_coverage(self):
        references = [
            {
                "path": "人物参考图/刘正胤.png",
                "role": "identity_only",
                "subject": "刘正胤",
                "source": "character_sheet",
                "review": reviewed("identity-review-1"),
                "sha256": "a" * 64,
            }
        ]

        with self.assertRaisesRegex(ValueError, "cast coverage"):
            scene_clusters.build_reference_pack(visual_cluster(), references, stable_pages=[])

    def test_complete_visual_pack_is_deterministic_and_traceable(self):
        cluster = visual_cluster()
        references = complete_references()
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]

        first = scene_clusters.build_reference_pack(cluster, references, stable_pages=stable_pages)
        second = scene_clusters.build_reference_pack(
            cluster, list(reversed(references)), stable_pages=list(reversed(stable_pages))
        )

        self.assertEqual(first, second)
        self.assertEqual(first["cluster_id"], cluster["cluster_id"])
        self.assertEqual(
            first["reference_pack_id"],
            canonical_hash(
                {
                    "cluster_id": cluster["cluster_id"],
                    "references": first["references"],
                    "stable_pages": first["stable_pages"],
                }
            ),
        )
        self.assertTrue(all(row["subject"] and row["source"] for row in first["references"]))

    def test_visual_pack_requires_each_target_composition_and_reviewed_style_anchor(self):
        stable_pages = [
            {
                "path": "输入/稳定页/0188.png",
                "review": reviewed("style-review-1"),
                "sha256": "b" * 64,
            }
        ]
        cases = {
            "target composition": [row for row in complete_references() if row["role"] != "target_composition"],
            "reviewed comic_style_anchor": [
                {**row, "review": {**reviewed("style-review-1"), "status": "pending"}}
                if row["role"] == "comic_style_anchor"
                else row
                for row in complete_references()
            ],
        }
        for message, references in cases.items():
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    scene_clusters.build_reference_pack(
                        visual_cluster(), references, stable_pages=stable_pages
                    )

    def test_comic_identity_anchor_can_cover_character_when_reviewed(self):
        refs = [row for row in complete_references() if row.get("subject") != "天朗真人"]
        refs.append(
            {
                "path": "输入/稳定页/天朗真人-正脸.png",
                "role": "comic_style_anchor",
                "subject": "天朗真人",
                "source": "reviewed_comic_identity_anchor",
                "review": reviewed("identity-comic-review-1"),
                "sha256": "f" * 64,
            }
        )
        stable_pages = [
            {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64},
            {"path": "输入/稳定页/天朗真人-正脸.png", "review": reviewed("identity-comic-review-1"), "sha256": "f" * 64},
        ]

        pack = scene_clusters.build_reference_pack(visual_cluster(), refs, stable_pages=stable_pages)

        self.assertEqual(pack["cluster_id"], "cluster-a")

    def test_comic_identity_anchor_requires_passed_full_size_review_evidence(self):
        references = [row for row in complete_references() if row.get("subject") != "天朗真人"]
        bad_reviews = (
            {**reviewed("identity-comic-review-1"), "status": "pending"},
            {**reviewed("identity-comic-review-1"), "status": "reviewed"},
            {**reviewed("identity-comic-review-1"), "full_size": False},
        )
        for review in bad_reviews:
            with self.subTest(review=review):
                candidate = references + [
                    {
                        "path": "输入/稳定页/天朗真人-正脸.png",
                        "role": "comic_style_anchor",
                        "subject": "天朗真人",
                        "source": "reviewed_comic_identity_anchor",
                        "review": review,
                        "sha256": "f" * 64,
                    }
                ]
                stable_pages = [
                    {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64},
                    {"path": "输入/稳定页/天朗真人-正脸.png", "review": review, "sha256": "f" * 64},
                ]
                with self.assertRaisesRegex(ValueError, "passed|full-size"):
                    scene_clusters.build_reference_pack(
                        visual_cluster(), candidate, stable_pages=stable_pages
                    )

    def test_character_sheet_cannot_claim_comic_style_anchor_role(self):
        references = complete_references()
        references[1] = {
            **references[1],
            "path": "人物参考图/邓正虎.png",
            "source": "character_sheet",
        }
        stable_pages = [
            {"path": "人物参考图/邓正虎.png", "review": reviewed("fake-style"), "sha256": "b" * 64}
        ]
        with self.assertRaisesRegex(ValueError, "comic_style_anchor source"):
            scene_clusters.build_reference_pack(
                visual_cluster(), references, stable_pages=stable_pages
            )

    def test_target_composition_must_be_immutable_matching_target_path(self):
        stable_pages = [
            {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        ]
        mutations = (
            {"path": "输入/场景/9999.png"},
            {"source": "generated_candidate"},
        )
        for mutation in mutations:
            references = complete_references()
            references[0] = {**references[0], **mutation}
            with self.subTest(mutation=mutation):
                with self.assertRaisesRegex(ValueError, "target_composition"):
                    scene_clusters.build_reference_pack(
                        visual_cluster(), references, stable_pages=stable_pages
                    )

    def test_v4_references_and_stable_pages_require_sha256(self):
        stable = {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        for index in range(len(complete_references())):
            references = complete_references()
            references[index].pop("sha256")
            with self.subTest(reference_index=index):
                with self.assertRaisesRegex(ValueError, "sha256"):
                    scene_clusters.build_reference_pack(
                        visual_cluster(), references, stable_pages=[stable]
                    )

        stable_without_hash = dict(stable)
        stable_without_hash.pop("sha256")
        with self.assertRaisesRegex(ValueError, "sha256"):
            scene_clusters.build_reference_pack(
                visual_cluster(), complete_references(), stable_pages=[stable_without_hash]
            )

    def test_stable_page_hash_change_invalidates_pack_identity_and_binding(self):
        stable = {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        first = scene_clusters.build_reference_pack(
            visual_cluster(), complete_references(), stable_pages=[stable]
        )
        changed_references = complete_references()
        changed_references[1] = {**changed_references[1], "sha256": "f" * 64}
        second = scene_clusters.build_reference_pack(
            visual_cluster(),
            changed_references,
            stable_pages=[{**stable, "sha256": "f" * 64}],
        )
        self.assertNotEqual(first["reference_pack_id"], second["reference_pack_id"])
        self.assertNotEqual(first["reference_binding_hash"], second["reference_binding_hash"])

    def test_prop_and_scene_anchors_are_conditional_on_issue_schedule(self):
        stable_pages = [
            {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        ]
        cases = (("prop continuity", "prop_anchor"), ("scene consistency", "scene_anchor"))
        for issue, required_role in cases:
            with self.subTest(issue=issue):
                with self.assertRaisesRegex(ValueError, required_role):
                    scene_clusters.build_reference_pack(
                        visual_cluster(issue_schedule=[issue]),
                        complete_references(),
                        stable_pages=stable_pages,
                    )

        pack = scene_clusters.build_reference_pack(
            visual_cluster(issue_schedule=["character_identity"]),
            complete_references(),
            stable_pages=stable_pages,
        )
        self.assertEqual(pack["cluster_id"], "cluster-a")

    def test_metadata_collections_reject_mapping_singletons_and_visual_conflicts(self):
        with self.assertRaisesRegex(ValueError, "cast.*list"):
            scene_clusters.build_scene_clusters(
                [make_page("1", cast={"邓正虎": "present"})], min_size=1
            )
        with self.assertRaisesRegex(ValueError, "issue_schedule.*list"):
            scene_clusters.build_scene_clusters(
                [make_page("1", issue_schedule={"type": "prop continuity"})],
                min_size=1,
            )
        with self.assertRaisesRegex(ValueError, "visual_tasks.*list"):
            scene_clusters.build_scene_clusters(
                [make_page("1", visual_tasks={"page": "redraw"})], min_size=1
            )
        with self.assertRaisesRegex(ValueError, "contradicts"):
            scene_clusters.build_scene_clusters(
                [
                    make_page(
                        "1",
                        has_visual_task=False,
                        issue_schedule=[{"type": "prop continuity"}],
                    )
                ],
                min_size=1,
            )

        with self.assertRaisesRegex(ValueError, "has_visual_tasks.*boolean"):
            scene_clusters.build_reference_pack(
                visual_cluster(has_visual_tasks="yes"),
                complete_references(),
                stable_pages=[
                    {
                        "path": "输入/稳定页/0188.png",
                        "review": reviewed("style-review-1"),
                        "sha256": "b" * 64,
                    }
                ],
            )

    def test_mapping_prop_and_scene_issues_require_their_anchors(self):
        stable_pages = [
            {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        ]
        for issue, role in (
            ({"type": "prop continuity", "subject": "狼毫笔"}, "prop_anchor"),
            ({"type": "scene consistency", "subject": "飞龙泉"}, "scene_anchor"),
        ):
            with self.subTest(issue=issue):
                with self.assertRaisesRegex(ValueError, role):
                    scene_clusters.build_reference_pack(
                        visual_cluster(issue_schedule=[issue]),
                        complete_references(),
                        stable_pages=stable_pages,
                    )

    def test_stable_page_requires_review_evidence_not_a_bare_path(self):
        with self.assertRaisesRegex(ValueError, "stable page review evidence"):
            scene_clusters.build_reference_pack(
                visual_cluster(),
                complete_references(),
                stable_pages=["输入/稳定页/0188.png"],
            )

    def test_reference_requires_subject_source_and_valid_review_shape(self):
        stable_pages = [
            {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        ]
        for field in ("subject", "source"):
            references = complete_references()
            references[0].pop(field)
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    scene_clusters.build_reference_pack(
                        visual_cluster(), references, stable_pages=stable_pages
                    )

    def test_duplicate_and_invalid_reference_paths_are_structured_rejections(self):
        invalid_references = complete_references()
        invalid_references.append({**invalid_references[0], "path": "输入\\场景\\0189.png"})
        with self.assertRaises(scene_clusters.SceneClusterContractError) as raised:
            scene_clusters.build_reference_pack(visual_cluster(), invalid_references, stable_pages=[])
        self.assertEqual(raised.exception.code, "DUPLICATE_REFERENCE")

        invalid_references = complete_references()
        invalid_references[0]["path"] = "../0189.png"
        with self.assertRaises(scene_clusters.SceneClusterContractError) as raised:
            scene_clusters.build_reference_pack(visual_cluster(), invalid_references, stable_pages=[])
        self.assertEqual(raised.exception.code, "INVALID_REFERENCE_PATH")

    def test_legacy_v3_pack_bridge_remains_content_addressed_and_strict(self):
        cluster = {"cluster_id": "legacy-cluster"}
        references = [
            {"path": "001.jpg", "role": "adjacent_style", "sha256": "a" * 64},
            {"path": "人物参考图/hero.png", "role": "identity_only", "sha256": "b" * 64},
        ]
        first = scene_clusters.build_reference_pack(
            cluster, references, contract_version="v3"
        )
        second = scene_clusters.build_reference_pack(
            cluster, list(reversed(references)), contract_version="v3"
        )
        self.assertEqual(first, second)
        self.assertEqual(
            first["reference_pack_id"],
            canonical_hash(
                {"cluster_id": "legacy-cluster", "references": first["references"]}
            ),
        )

        changed = scene_clusters.build_reference_pack(
            cluster,
            [{**references[0], "sha256": "c" * 64}, references[1]],
            contract_version="v3",
        )
        self.assertNotEqual(first["reference_pack_id"], changed["reference_pack_id"])

    def test_legacy_v3_primary_style_still_requires_allowlisted_clean_page(self):
        clean = {
            "references": [{"path": "comic/010.jpg", "role": "primary_style"}]
        }
        self.assertTrue(
            scene_clusters.validate_reference_pack(
                clean, stable_pages=["comic/010.jpg"], contract_version="v3"
            )
        )
        with self.assertRaisesRegex(ValueError, "stable page"):
            scene_clusters.validate_reference_pack(
                clean, stable_pages=["comic/009.jpg"], contract_version="v3"
            )
        with self.assertRaisesRegex(ValueError, "contaminated"):
            scene_clusters.validate_reference_pack(
                {
                    "references": [
                        {"path": "identity_sheet.png", "role": "primary_style"}
                    ]
                },
                stable_pages=["identity_sheet.png"],
                contract_version="v3",
            )

    def test_reference_pack_rejects_empty_unknown_and_malformed_records(self):
        for pack in (
            {},
            {"references": []},
            {"references": [None]},
            {"references": [{"path": "page.jpg", "role": "unknown_role"}]},
            {"references": [{"path": "  ", "role": "adjacent_style"}]},
        ):
            with self.subTest(pack=pack):
                with self.assertRaises(ValueError):
                    scene_clusters.validate_reference_pack(pack)

    def test_bind_reference_pack_is_immutable_idempotent_and_rejects_rebind(self):
        cluster = visual_cluster()
        stable_pages = [
            {"path": "输入/稳定页/0188.png", "review": reviewed("style-review-1"), "sha256": "b" * 64}
        ]
        pack = scene_clusters.build_reference_pack(cluster, complete_references(), stable_pages=stable_pages)
        bound = scene_clusters.bind_reference_pack(cluster, pack, stable_pages=stable_pages)
        self.assertNotIn("reference_pack_id", cluster)
        self.assertEqual(bound["reference_pack_id"], pack["reference_pack_id"])
        self.assertEqual(scene_clusters.bind_reference_pack(bound, pack, stable_pages=stable_pages), bound)

        replacement_refs = complete_references()
        replacement_refs[0] = {**replacement_refs[0], "sha256": "e" * 64}
        replacement = scene_clusters.build_reference_pack(cluster, replacement_refs, stable_pages=stable_pages)
        with self.assertRaisesRegex(ValueError, "rebind"):
            scene_clusters.bind_reference_pack(bound, replacement, stable_pages=stable_pages)


if __name__ == "__main__":
    unittest.main()
