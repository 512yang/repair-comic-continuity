import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from failure_learning import (  # noqa: E402
    FAILURE_CODES,
    new_failure_store,
    promote_rule as promote_rule_with_evidence,
    record_failure,
    record_outcome,
    revoke_rule,
    select_effective_rules,
    validate_failure_store,
)


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
SHA = lambda char: char * 64


def promote_rule(store, failure_id, scope, **overrides):
    evidence = {
        "promoted_by": "independent-reviewer",
        "positive_regression_passed": True,
        "positive_regression_artifact_hash": SHA("9"),
        "clean_control_passed": True,
        "clean_control_artifact_hash": SHA("8"),
        "variation_passed": True,
        "variation_artifact_hash": SHA("7"),
        "independently_reviewed": True,
        "independent_review_artifact_hash": SHA("6"),
    }
    evidence.update(overrides)
    return promote_rule_with_evidence(store, failure_id, scope, **evidence)


def add_effective_failure(
    store,
    page_id,
    cluster_id,
    before_char,
    after_char,
    *,
    character="hero",
    codes=("over_rendering",),
    action="remove_identity_sheet_from_style_inputs",
):
    failure = record_failure(
        store,
        page_id,
        cluster_id,
        character,
        codes,
        "identity sheet contaminated the comic style",
        action,
        f"before/{page_id}.png",
        SHA(before_char),
        prompt_reference_hash="prompt-ref",
        created_at=NOW,
    )
    failure = record_outcome(
        store,
        failure["failure_id"],
        f"after/{page_id}.png",
        SHA(after_char),
        True,
        "independent-reviewer",
        NOW,
    )
    return failure


class FailureLearningTests(unittest.TestCase):
    def test_rule_requires_positive_clean_control_variation_and_independent_review(self):
        store = new_failure_store()
        failure = add_effective_failure(store, "1", "cluster-a", "a", "b")
        checks = (
            ("positive_regression_passed", "positive regression"),
            ("clean_control_passed", "clean control"),
            ("variation_passed", "variation"),
            ("independently_reviewed", "independent review"),
        )
        for field, message in checks:
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, message):
                    promote_rule(store, failure["failure_id"], "page", **{field: False})
        self.assertEqual(store["rules"], [])

    def test_promoted_rule_binds_four_artifact_hashes(self):
        store = new_failure_store()
        failure = add_effective_failure(store, "1", "cluster-a", "a", "b")
        rule = promote_rule(store, failure["failure_id"], "page")
        self.assertEqual(
            set(rule["promotion_evidence"]),
            {"positive_regression", "clean_control", "variation", "independent_review"},
        )
        self.assertTrue(all(
            item["passed"] and len(item["artifact_hash"]) == 64
            for item in rule["promotion_evidence"].values()
        ))
    def test_constants_store_and_effective_page_rule_selection(self):
        self.assertEqual(
            FAILURE_CODES,
            {
                "style_drift",
                "identity_drift",
                "costume_prop_drift",
                "composition_drift",
                "anatomy_error",
                "scene_drift",
                "text_leak",
                "over_rendering",
            },
        )
        store = new_failure_store()
        self.assertEqual(
            set(store),
            {"schema_version", "revision", "failures", "rules", "promotion_events"},
        )
        failure = add_effective_failure(store, "252（1）.jpg", "cluster-a", "a", "b")
        rule = promote_rule(store, failure["failure_id"], "page")

        selected = select_effective_rules(
            store,
            page_id="252(1)",
            cluster_id="cluster-a",
            character="hero",
            codes=["over_rendering"],
        )

        self.assertEqual(failure["page_id"], "252(1)")
        self.assertTrue(failure["outcome_id"].startswith("outcome-"))
        self.assertEqual(rule["scope"], "page")
        self.assertEqual(rule["source_outcome_ids"], [failure["outcome_id"]])
        self.assertEqual(
            store["promotion_events"][0]["evidence_ids"],
            [failure["outcome_id"]],
        )
        self.assertEqual(selected[0]["corrective_action"], "remove_identity_sheet_from_style_inputs")
        self.assertEqual(selected[0]["rule_id"], rule["rule_id"])
        self.assertEqual(store["promotion_events"][0]["from"], "observed")
        self.assertEqual(store["promotion_events"][0]["to"], "page")

    def test_record_failure_rejects_bad_code_hash_and_naive_time(self):
        base = dict(
            page_id="1",
            cluster_id="cluster-a",
            character="hero",
            codes=["over_rendering"],
            diagnosis="bad style",
            corrective_action="remove sheet",
            before_candidate_path="before.png",
            before_candidate_hash=SHA("a"),
            created_at=NOW,
        )
        variants = (
            {"codes": ["not-a-code"]},
            {"codes": []},
            {"before_candidate_hash": "bad"},
            {"created_at": NOW.replace(tzinfo=None)},
        )
        for override in variants:
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    record_failure(new_failure_store(), **(base | override))

    def test_record_failure_is_idempotent_but_distinct_immutable_content_can_coexist(self):
        store = new_failure_store()
        kwargs = dict(
            page_id="1.jpg",
            cluster_id="cluster-a",
            character="hero",
            codes=["style_drift"],
            diagnosis="flat color",
            corrective_action="restore_comic_palette",
            before_candidate_path="before.png",
            before_candidate_hash=SHA("a"),
            created_at=NOW,
        )
        first = record_failure(store, **kwargs)
        self.assertIs(first, record_failure(store, **kwargs))
        self.assertEqual(len(store["failures"]), 1)
        second = record_failure(
            store,
            **(kwargs | {"character": "villain", "diagnosis": "different"}),
        )
        self.assertNotEqual(first["failure_id"], second["failure_id"])
        self.assertEqual(len(store["failures"]), 2)

    def test_created_at_is_a_required_keyword_argument(self):
        with self.assertRaises(TypeError):
            record_failure(
                new_failure_store(),
                "1",
                "cluster-a",
                "hero",
                ["style_drift"],
                "bad",
                "fix",
                "before.png",
                SHA("a"),
            )

    def test_outcome_requires_distinct_complete_evidence_and_is_write_once(self):
        store = new_failure_store()
        failure = record_failure(
            store,
            "1",
            "cluster-a",
            "hero",
            ["style_drift"],
            "bad",
            "fix",
            "before.png",
            SHA("a"),
            created_at=NOW,
        )
        failure_id = failure["failure_id"]
        with self.assertRaises(ValueError):
            record_outcome(store, failure_id, "", SHA("b"), True, "reviewer", NOW)
        with self.assertRaisesRegex(ValueError, "different"):
            record_outcome(store, failure_id, "after.png", SHA("a"), True, "reviewer", NOW)
        result = record_outcome(
            store, failure_id, "after.png", SHA("b"), False, "reviewer", NOW
        )
        self.assertIs(result, record_outcome(
            store, failure_id, "after.png", SHA("b"), False, "reviewer", NOW
        ))
        with self.assertRaisesRegex(ValueError, "already"):
            record_outcome(store, failure_id, "after.png", SHA("c"), True, "reviewer", NOW)
        with self.assertRaises(ValueError):
            promote_rule(store, failure_id, "page")

    def test_promotion_cannot_skip_levels(self):
        store = new_failure_store()
        failure = add_effective_failure(store, "1", "cluster-a", "a", "b")
        for scope in ("cluster", "project", "skill_candidate"):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError):
                    promote_rule(store, failure["failure_id"], scope)

    def test_two_pages_promote_cluster_and_are_idempotent(self):
        store = new_failure_store()
        first = add_effective_failure(store, "1", "cluster-a", "a", "b")
        second = add_effective_failure(store, "2", "cluster-a", "c", "d")
        promote_rule(store, first["failure_id"], "page")
        promote_rule(store, second["failure_id"], "page")
        cluster_rule = promote_rule(store, first["failure_id"], "cluster")
        self.assertEqual(cluster_rule["scope"], "cluster")
        self.assertEqual(len(cluster_rule["evidence_failure_ids"]), 2)
        event_count = len(store["promotion_events"])
        self.assertIs(cluster_rule, promote_rule(store, second["failure_id"], "cluster"))
        self.assertEqual(len(store["promotion_events"]), event_count)

    def test_same_page_signature_uses_character_specific_semantic_rules(self):
        store = new_failure_store()
        failures = []
        for page_id, character, diagnosis, before, after in (
            ("1", "hero", "first diagnosis", "a", "b"),
            ("1", "villain", "second diagnosis", "c", "d"),
            ("2", "hero", "third diagnosis", "e", "f"),
            ("2", "villain", "fourth diagnosis", "1", "2"),
        ):
            failure = record_failure(
                store,
                page_id,
                "cluster-a",
                character,
                ["over_rendering"],
                diagnosis,
                "remove_identity_sheet_from_style_inputs",
                f"before/{page_id}-{character}.png",
                SHA(before),
                created_at=NOW,
            )
            record_outcome(
                store,
                failure["failure_id"],
                f"after/{page_id}-{character}.png",
                SHA(after),
                True,
                "reviewer",
                NOW,
            )
            failures.append(failure)

        page_rules = [promote_rule(store, item["failure_id"], "page") for item in failures]
        self.assertNotEqual(page_rules[0]["semantic_key"], page_rules[1]["semantic_key"])
        self.assertNotEqual(page_rules[0]["rule_id"], page_rules[1]["rule_id"])
        self.assertNotEqual(page_rules[2]["rule_id"], page_rules[3]["rule_id"])
        self.assertEqual(len([rule for rule in store["rules"] if rule["scope"] == "page"]), 4)

        hero_cluster = promote_rule(store, failures[0]["failure_id"], "cluster")
        villain_cluster = promote_rule(store, failures[1]["failure_id"], "cluster")
        self.assertNotEqual(hero_cluster["semantic_key"], villain_cluster["semantic_key"])
        self.assertEqual(len(hero_cluster["source_rule_ids"]), 2)
        self.assertEqual(len(hero_cluster["source_failure_ids"]), 2)
        self.assertEqual(
            {next(item for item in store["rules"] if item["rule_id"] == source)["page_id"]
             for source in hero_cluster["source_rule_ids"]},
            {"1", "2"},
        )
        self.assertEqual(
            [item["character"] for item in select_effective_rules(
                store,
                page_id="1",
                cluster_id="cluster-a",
                character="hero",
                codes=["over_rendering"],
            )],
            ["hero", "hero"],
        )

    def test_cross_character_evidence_cannot_satisfy_cluster_promotion(self):
        store = new_failure_store()
        hero = add_effective_failure(
            store, "1", "cluster-a", "a", "b", character="hero"
        )
        villain = add_effective_failure(
            store, "2", "cluster-a", "c", "d", character="villain"
        )
        promote_rule(store, hero["failure_id"], "page")
        promote_rule(store, villain["failure_id"], "page")
        before = copy.deepcopy(store)

        for failure in (hero, villain):
            with self.subTest(character=failure["character"]):
                with self.assertRaises(ValueError):
                    promote_rule(store, failure["failure_id"], "cluster")
                self.assertEqual(store, before)

    def test_cross_character_cluster_rules_cannot_satisfy_project_promotion(self):
        store = new_failure_store()
        representatives = []
        for cluster, character, pages, chars in (
            ("cluster-a", "hero", ("1", "2"), (("a", "b"), ("c", "d"))),
            ("cluster-b", "villain", ("3", "4"), (("e", "f"), ("1", "2"))),
        ):
            local = []
            for page, (before, after) in zip(pages, chars):
                failure = add_effective_failure(
                    store,
                    page,
                    cluster,
                    before,
                    after,
                    character=character,
                )
                promote_rule(store, failure["failure_id"], "page")
                local.append(failure)
            promote_rule(store, local[0]["failure_id"], "cluster")
            representatives.append(local[0])
        before = copy.deepcopy(store)

        for failure in representatives:
            with self.subTest(character=failure["character"]):
                with self.assertRaises(ValueError):
                    promote_rule(store, failure["failure_id"], "project")
                self.assertEqual(store, before)

    def test_failed_mutations_leave_the_store_deeply_unchanged(self):
        store = new_failure_store()
        failure = add_effective_failure(store, "1", "cluster-a", "a", "b")
        promote_rule(store, failure["failure_id"], "page")
        before = copy.deepcopy(store)

        with self.assertRaises(ValueError):
            promote_rule(store, failure["failure_id"], "cluster")
        self.assertEqual(store, before)
        with self.assertRaises(ValueError):
            revoke_rule(store, "missing-rule", "bad", "coordinator", NOW)
        self.assertEqual(store, before)

    def test_two_clusters_promote_project_then_skill_candidate(self):
        store = new_failure_store()
        failures = []
        for cluster, page_ids, chars in (
            ("cluster-a", ("1", "2"), (("a", "b"), ("c", "d"))),
            ("cluster-b", ("3", "4"), (("e", "f"), ("1", "2"))),
        ):
            local = []
            for page_id, (before, after) in zip(page_ids, chars):
                failure = add_effective_failure(store, page_id, cluster, before, after)
                promote_rule(store, failure["failure_id"], "page")
                local.append(failure)
                failures.append(failure)
            promote_rule(store, local[0]["failure_id"], "cluster")

        project = promote_rule(store, failures[0]["failure_id"], "project")
        skill = promote_rule(store, failures[0]["failure_id"], "skill_candidate")

        self.assertEqual(project["scope"], "project")
        self.assertEqual(skill["scope"], "skill_candidate")
        self.assertEqual(len(store["promotion_events"]), 8)

    def test_selection_orders_specificity_and_filters_character_and_codes(self):
        store = new_failure_store()
        failures = []
        for cluster, pages, chars in (
            ("cluster-a", ("1", "2"), (("a", "b"), ("c", "d"))),
            ("cluster-b", ("3", "4"), (("e", "f"), ("1", "2"))),
        ):
            local = []
            for page, (before, after) in zip(pages, chars):
                failure = add_effective_failure(store, page, cluster, before, after)
                promote_rule(store, failure["failure_id"], "page")
                local.append(failure)
                failures.append(failure)
            promote_rule(store, local[0]["failure_id"], "cluster")
        promote_rule(store, failures[0]["failure_id"], "project")
        promote_rule(store, failures[0]["failure_id"], "skill_candidate")

        selected = select_effective_rules(
            store, page_id="1", cluster_id="cluster-a", character="hero",
            codes=["over_rendering"],
        )
        self.assertEqual(
            [rule["scope"] for rule in selected],
            ["page", "cluster", "project", "skill_candidate"],
        )
        self.assertEqual(
            select_effective_rules(store, character="other", codes=["text_leak"]),
            [],
        )

    def test_revocation_is_audited_and_removes_rule_from_selection(self):
        store = new_failure_store()
        failure = add_effective_failure(store, "1", "cluster-a", "a", "b")
        rule = promote_rule(store, failure["failure_id"], "page")
        revoked = revoke_rule(store, rule["rule_id"], "superseded", "coordinator", NOW)

        self.assertTrue(revoked["revoked"])
        self.assertFalse(revoked["effective"])
        self.assertEqual(store["rules"][0]["rule_id"], rule["rule_id"])
        self.assertEqual(select_effective_rules(
            store, page_id="1", cluster_id="cluster-a", character="hero",
            codes=["over_rendering"],
        ), [])
        self.assertEqual(store["promotion_events"][-1]["event_type"], "revoked")

    def test_revoking_a_lower_rule_cascades_to_all_active_dependents(self):
        store = new_failure_store()
        failures = []
        for cluster, pages, chars in (
            ("cluster-a", ("1", "2"), (("a", "b"), ("c", "d"))),
            ("cluster-b", ("3", "4"), (("e", "f"), ("1", "2"))),
        ):
            local = []
            for page, (before, after) in zip(pages, chars):
                failure = add_effective_failure(store, page, cluster, before, after)
                promote_rule(store, failure["failure_id"], "page")
                local.append(failure)
                failures.append(failure)
            promote_rule(store, local[0]["failure_id"], "cluster")
        promote_rule(store, failures[0]["failure_id"], "project")
        promote_rule(store, failures[0]["failure_id"], "skill_candidate")
        page_rule = next(
            rule for rule in store["rules"]
            if rule["scope"] == "page" and rule["page_id"] == "1"
        )

        revoke_rule(store, page_rule["rule_id"], "invalid evidence", "coordinator", NOW)

        dependent_scopes = {
            rule["scope"] for rule in store["rules"]
            if rule["revoked"]
        }
        self.assertEqual(
            dependent_scopes,
            {"page", "cluster", "project", "skill_candidate"},
        )
        self.assertEqual(
            select_effective_rules(
                store,
                page_id="1",
                cluster_id="cluster-a",
                character="hero",
                codes=["over_rendering"],
            ),
            [],
        )

    def test_validate_failure_store_rejects_duplicates_broken_chain_and_bad_types(self):
        store = new_failure_store()
        failure = add_effective_failure(store, "1", "cluster-a", "a", "b")
        promote_rule(store, failure["failure_id"], "page")
        variants = []
        duplicate = copy.deepcopy(store)
        duplicate["failures"].append(copy.deepcopy(duplicate["failures"][0]))
        variants.append(duplicate)
        broken = copy.deepcopy(store)
        broken["rules"][0]["evidence_failure_ids"] = ["missing"]
        variants.append(broken)
        nonfinite = copy.deepcopy(store)
        nonfinite["revision"] = float("nan")
        variants.append(nonfinite)
        wrong_type = copy.deepcopy(store)
        wrong_type["rules"] = {}
        variants.append(wrong_type)
        unhashable_rule_id = copy.deepcopy(store)
        unhashable_rule_id["rules"][0]["rule_id"] = []
        variants.append(unhashable_rule_id)
        missing_audit = copy.deepcopy(store)
        missing_audit["promotion_events"] = []
        variants.append(missing_audit)
        wrong_event_evidence = copy.deepcopy(store)
        wrong_event_evidence["promotion_events"][0]["evidence_ids"] = [
            wrong_event_evidence["rules"][0]["rule_id"]
        ]
        variants.append(wrong_event_evidence)
        tampered_failure = copy.deepcopy(store)
        tampered_failure["failures"][0]["diagnosis"] = "tampered"
        variants.append(tampered_failure)
        tampered_rule_review = copy.deepcopy(store)
        tampered_rule_review["rules"][0]["promoted_by"] = "tampered"
        variants.append(tampered_rule_review)
        tampered_rule_source = copy.deepcopy(store)
        tampered_rule_source["rules"][0]["evidence_failure_ids"] = []
        variants.append(tampered_rule_source)
        tampered_event_time = copy.deepcopy(store)
        tampered_event_time["promotion_events"][0]["timestamp"] = (
            datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc).isoformat()
        )
        variants.append(tampered_event_time)
        for field, value in (
            ("after_path", "tampered.png"),
            ("after_hash", SHA("c")),
            ("effective", False),
            ("reviewed_by", "tampered-reviewer"),
            (
                "reviewed_at",
                datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc).isoformat(),
            ),
        ):
            tampered_outcome = copy.deepcopy(store)
            failure_record = tampered_outcome["failures"][0]
            if field == "after_path":
                failure_record["after_candidate"]["path"] = value
            elif field == "after_hash":
                failure_record["after_candidate"]["hash"] = value
            else:
                failure_record[field] = value
            variants.append(tampered_outcome)
        for invalid in variants:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    validate_failure_store(invalid)

    def test_validate_failure_store_rejects_a_structurally_broken_promotion_chain(self):
        store = new_failure_store()
        first = add_effective_failure(store, "1", "cluster-a", "a", "b")
        second = add_effective_failure(store, "2", "cluster-a", "c", "d")
        promote_rule(store, first["failure_id"], "page")
        promote_rule(store, second["failure_id"], "page")
        cluster_rule = promote_rule(store, first["failure_id"], "cluster")
        cluster_rule["evidence_rule_ids"] = []

        with self.assertRaises(ValueError):
            validate_failure_store(store)


if __name__ == "__main__":
    unittest.main()
