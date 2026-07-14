import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from scene_clusters import (  # noqa: E402
    build_reference_pack,
    build_scene_clusters,
    cluster_size_controls,
    scene_key,
    validate_reference_pack,
)
import scene_clusters as scene_clusters_module  # noqa: E402
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


class SceneKeyTests(unittest.TestCase):
    def test_scene_key_normalizes_missing_and_blank_values(self):
        self.assertEqual(
            scene_key({"chapter": "  ", "location": None}),
            ("unknown", "unknown", "unknown", "unknown"),
        )

    def test_scene_key_is_ordered_by_named_fields(self):
        self.assertEqual(
            scene_key(
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
    def test_cluster_size_controls_define_normal_boundary_and_blocked_short_cases(self):
        self.assertEqual(
            cluster_size_controls(20, 10, False),
            {
                "undersized": False,
                "boundary_exception": False,
                "undersized_reason": None,
                "blocked": False,
                "blocker_codes": [],
            },
        )
        self.assertEqual(
            cluster_size_controls(4, 4, True),
            {
                "undersized": True,
                "boundary_exception": True,
                "undersized_reason": "project_total_below_min",
                "blocked": False,
                "blocker_codes": [],
            },
        )
        self.assertEqual(
            cluster_size_controls(13, 6, False),
            {
                "undersized": True,
                "boundary_exception": False,
                "undersized_reason": "scene_fragment_below_min",
                "blocked": True,
                "blocker_codes": ["UNDERSIZED_CLUSTER"],
            },
        )

    def test_twenty_page_run_splits_into_two_ten_page_clusters_with_boundary_context(self):
        pages = [make_page(index) for index in range(1, 21)]

        clusters = build_scene_clusters(pages, min_size=8, max_size=10)

        self.assertEqual([len(item["member_pages"]) for item in clusters], [10, 10])
        self.assertEqual(clusters[0]["member_pages"], [str(i) for i in range(1, 11)])
        self.assertEqual(clusters[0]["context_before"], [])
        self.assertEqual(clusters[0]["context_after"], ["11", "12"])
        self.assertEqual(clusters[1]["context_before"], ["9", "10"])
        self.assertEqual(clusters[1]["context_after"], [])

    def test_short_adjacent_runs_merge_only_when_chapter_and_location_match(self):
        pages = [
            *[make_page(index, scene_id="scene-a") for index in range(1, 5)],
            *[
                make_page(index, scene_id="scene-b", story_time="night")
                for index in range(5, 9)
            ],
            *[
                make_page(
                    index,
                    chapter="chapter-2",
                    scene_id="scene-c",
                    story_time="night",
                )
                for index in range(9, 13)
            ],
        ]

        clusters = build_scene_clusters(pages, min_size=8, max_size=20)

        self.assertEqual(clusters[0]["member_pages"], [str(i) for i in range(1, 9)])
        self.assertFalse(clusters[0]["undersized"])
        self.assertEqual(clusters[1]["member_pages"], [str(i) for i in range(9, 13)])
        self.assertTrue(clusters[1]["undersized"])

    def test_short_run_does_not_merge_across_location(self):
        pages = [
            *[make_page(index, location="courtyard") for index in range(1, 5)],
            *[make_page(index, location="hall") for index in range(5, 9)],
        ]

        clusters = build_scene_clusters(pages, min_size=8, max_size=20)

        self.assertEqual([len(item["member_pages"]) for item in clusters], [4, 4])
        self.assertTrue(all(item["undersized"] for item in clusters))
        self.assertTrue(all(item["blocked"] for item in clusters))
        self.assertTrue(all(item["boundary_exception"] is False for item in clusters))
        self.assertTrue(
            all(item["undersized_reason"] == "scene_fragment_below_min" for item in clusters)
        )
        self.assertTrue(
            all("UNDERSIZED_CLUSTER" in item["blocker_codes"] for item in clusters)
        )

    def test_whole_project_below_min_gets_only_boundary_exception(self):
        cluster = build_scene_clusters(
            [make_page(index) for index in range(1, 5)],
            min_size=8,
            max_size=20,
        )[0]

        self.assertTrue(cluster["undersized"])
        self.assertTrue(cluster["boundary_exception"])
        self.assertEqual(cluster["undersized_reason"], "project_total_below_min")
        self.assertFalse(cluster["blocked"])
        self.assertEqual(cluster["blocker_codes"], [])

    def test_short_runs_with_unknown_chapter_or_location_do_not_merge(self):
        variants = (
            {"chapter": "  ", "location": "courtyard"},
            {"chapter": "chapter-1", "location": None},
        )
        for shared_fields in variants:
            with self.subTest(shared_fields=shared_fields):
                pages = [
                    *[
                        make_page(
                            index,
                            **shared_fields,
                            scene_id="scene-a",
                            story_time="day",
                        )
                        for index in range(1, 5)
                    ],
                    *[
                        make_page(
                            index,
                            **shared_fields,
                            scene_id="scene-b",
                            story_time="night",
                        )
                        for index in range(5, 9)
                    ],
                ]

                clusters = build_scene_clusters(pages, min_size=8, max_size=20)

                self.assertEqual(
                    [len(item["member_pages"]) for item in clusters], [4, 4]
                )
                self.assertTrue(all(item["undersized"] for item in clusters))

    def test_context_never_duplicates_member_pages(self):
        pages = [make_page(index) for index in range(1, 13)]

        clusters = build_scene_clusters(
            pages, min_size=3, max_size=4, context_radius=2
        )

        for cluster in clusters:
            members = set(cluster["member_pages"])
            self.assertTrue(members.isdisjoint(cluster["context_before"]))
            self.assertTrue(members.isdisjoint(cluster["context_after"]))

    def test_canary_uses_highest_risk_and_breaks_ties_by_story_order(self):
        pages = [
            make_page("1", risk_score=1),
            make_page("2", risk_score=9),
            make_page("3", risk_score=9),
            make_page("4", risk_score=2),
        ]

        cluster = build_scene_clusters(pages, min_size=1, max_size=20)[0]

        self.assertEqual(cluster["canary_page"], "2")

    def test_canary_without_risk_uses_earlier_middle_page(self):
        pages = [make_page(index) for index in range(1, 5)]

        cluster = build_scene_clusters(pages, min_size=1, max_size=20)[0]

        self.assertEqual(cluster["canary_page"], "2")

    def test_canary_rejects_nonfinite_risk_scores(self):
        for risk_score in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(risk_score=risk_score):
                with self.assertRaises(ValueError):
                    build_scene_clusters(
                        [make_page("1", risk_score=risk_score)],
                        min_size=1,
                    )

    def test_cluster_identifiers_are_stable_for_equivalent_input(self):
        pages = [make_page(index) for index in range(1, 9)]

        first = build_scene_clusters(pages)
        second = build_scene_clusters([dict(page) for page in pages])

        self.assertEqual(first, second)
        self.assertTrue(first[0]["cluster_id"].startswith("cluster-"))
        self.assertIsNone(first[0]["reference_pack_id"])
        self.assertEqual(first[0]["reference_pack_state"], "unbound")

    def test_invalid_cluster_parameters_are_rejected(self):
        invalid = [
            {"min_size": 0},
            {"max_size": 0},
            {"min_size": 4, "max_size": 3},
            {"context_radius": -1},
            {"context_radius": 3},
            {"min_size": True},
        ]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    build_scene_clusters([make_page("1")], **kwargs)


class ReferencePackTests(unittest.TestCase):
    def test_bind_reference_pack_is_immutable_idempotent_and_rejects_rebind(self):
        cluster = build_scene_clusters([make_page("1")], min_size=1)[0]
        pack = build_reference_pack(
            cluster,
            [{"path": "001.jpg", "role": "adjacent_style", "sha256": "a" * 64}],
        )
        self.assertTrue(hasattr(scene_clusters_module, "bind_reference_pack"))
        bound = scene_clusters_module.bind_reference_pack(cluster, pack)
        self.assertIsNone(cluster["reference_pack_id"])
        self.assertEqual(cluster["reference_pack_state"], "unbound")
        self.assertEqual(bound["reference_pack_id"], pack["reference_pack_id"])
        self.assertEqual(bound["reference_pack_state"], "bound")
        self.assertEqual(scene_clusters_module.bind_reference_pack(bound, pack), bound)

        replacement = build_reference_pack(
            cluster,
            [{"path": "002.jpg", "role": "adjacent_style", "sha256": "b" * 64}],
        )
        with self.assertRaisesRegex(ValueError, "rebind"):
            scene_clusters_module.bind_reference_pack(bound, replacement)

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

    def test_validate_reference_pack_accepts_identity_sheet_as_identity_only(self):
        pack = {
            "references": [
                {"path": r"D:\refs\人物参考图\hero.png", "role": "identity_only"}
            ]
        }

        self.assertTrue(validate_reference_pack(pack))

    def test_validate_reference_pack_rejects_primary_style_contamination(self):
        suspicious_paths = [
            r"D:\refs\人物参考图\hero.png",
            r"D:\refs\identity_sheet.png",
            r"D:\evidence\page_candidates\224.png",
            r"page_candidates\224.png",
            r"D:\refs\candidate_rejected.png",
        ]
        for path in suspicious_paths:
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    validate_reference_pack(
                        {"references": [{"path": path, "role": "primary_style"}]}
                    )

    def test_primary_style_must_be_in_stable_pages_when_allowlist_is_supplied(self):
        with self.assertRaises(ValueError):
            validate_reference_pack(
                {
                    "references": [
                        {"path": r"D:\comic\010.jpg", "role": "primary_style"}
                    ]
                },
                stable_pages=[r"D:\comic\009.jpg"],
            )

        self.assertTrue(
            validate_reference_pack(
                {
                    "references": [
                        {"path": r"D:\comic\010.jpg", "role": "primary_style"}
                    ]
                },
                stable_pages=[r"D:\comic\010.jpg"],
            )
        )

    def test_primary_style_requires_a_nonempty_stable_page_allowlist(self):
        pack = {
            "references": [
                {"path": r"D:\comic\010.jpg", "role": "primary_style"}
            ]
        }

        for stable_pages in (None, []):
            with self.subTest(stable_pages=stable_pages):
                with self.assertRaises(ValueError):
                    validate_reference_pack(pack, stable_pages=stable_pages)

    def test_build_reference_pack_requires_stable_pages_for_primary_style(self):
        cluster = build_scene_clusters([make_page("1")], min_size=1)[0]

        with self.assertRaises(ValueError):
            build_reference_pack(
                cluster,
                [{"path": r"D:\comic\010.jpg", "role": "primary_style"}],
            )

    def test_reference_pack_rejects_duplicate_unknown_and_empty_references(self):
        invalid_packs = [
            {
                "references": [
                    {"path": "page.jpg", "role": "adjacent_style"},
                    {"path": "page.jpg", "role": "adjacent_style"},
                ]
            },
            {"references": [{"path": "page.jpg", "role": "unknown_role"}]},
            {"references": [{"path": "  ", "role": "adjacent_style"}]},
            {},
        ]
        for pack in invalid_packs:
            with self.subTest(pack=pack):
                with self.assertRaises(ValueError):
                    validate_reference_pack(pack)

    def test_reference_role_must_be_a_string(self):
        for role in ([], {}, 1, None):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    validate_reference_pack(
                        {"references": [{"path": "page.jpg", "role": role}]}
                    )

    def test_references_must_be_a_nonempty_list(self):
        for references in ([], (), None):
            with self.subTest(references=references):
                with self.assertRaises(ValueError):
                    validate_reference_pack({"references": references})

    def test_build_reference_pack_is_stable_and_binds_cluster(self):
        cluster = build_scene_clusters(
            [make_page(index) for index in range(1, 9)]
        )[0]
        references = [
            {"role": "identity_only", "path": r"D:\refs\人物参考图\hero.png"},
            {"path": r"D:\comic\001.jpg", "role": "primary_style"},
        ]

        first = build_reference_pack(
            cluster, references, stable_pages=[r"D:\comic\001.jpg"]
        )
        second = build_reference_pack(
            cluster,
            list(reversed(references)),
            stable_pages=[r"D:\comic\001.jpg"],
        )

        self.assertEqual(first, second)
        self.assertEqual(first["cluster_id"], cluster["cluster_id"])
        self.assertEqual(
            first["reference_pack_id"],
            canonical_hash(
                {
                    "cluster_id": cluster["cluster_id"],
                    "references": first["references"],
                }
            ),
        )

    def test_reference_pack_id_and_binding_hash_cover_path_role_and_sha256(self):
        cluster = build_scene_clusters([make_page("1")], min_size=1)[0]
        base_references = [
            {
                "path": r"D:\comic\001.jpg",
                "role": "adjacent_style",
                "sha256": "a" * 64,
            },
            {
                "path": r"D:\refs\hero.png",
                "role": "identity_only",
                "sha256": "b" * 64,
            },
        ]
        base = build_reference_pack(cluster, base_references)
        self.assertEqual(base["references"][0].get("sha256"), "a" * 64)
        self.assertEqual(
            base["reference_binding_hash"],
            canonical_hash(
                {
                    "reference_pack_id": base["reference_pack_id"],
                    "references": base["references"],
                }
            ),
        )

        mutations = (
            {**base_references[0], "path": r"D:\comic\002.jpg"},
            {**base_references[0], "role": "composition_only"},
            {**base_references[0], "sha256": "c" * 64},
        )
        for changed_reference in mutations:
            with self.subTest(changed_reference=changed_reference):
                changed = build_reference_pack(
                    cluster,
                    [changed_reference, base_references[1]],
                )
                self.assertNotEqual(
                    changed["reference_pack_id"], base["reference_pack_id"]
                )

    def test_build_reference_pack_rejects_missing_path(self):
        cluster = build_scene_clusters([make_page("1")], min_size=1)[0]

        with self.assertRaises(ValueError):
            build_reference_pack(
                cluster,
                [{"role": "adjacent_style"}],
            )


if __name__ == "__main__":
    unittest.main()
