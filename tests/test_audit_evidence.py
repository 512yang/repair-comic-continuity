import sys
import tempfile
import unittest
import copy
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from audit_evidence import (  # noqa: E402
    HIGH_CONFIDENCE,
    MEDIUM_CONFIDENCE,
    NON_DEFECT_CODES,
    REQUIRED_AUDIT_CHECKS,
    aggregate_page_audits,
    append_review_event,
    record_page_audit,
    route_page_decision,
    validate_review_log,
)


def valid_audit(**overrides):
    audit = {
        "page": "场景/252（1）.jpg",
        "source_sha256": "a" * 64,
        "width": 1200,
        "height": 1600,
        "perspective": "continuity",
        "reviewer": "reviewer-a",
        "reviewed_at": "2026-07-15T08:00:00+08:00",
        "confidence": 1.0,
        "inspected_panels": [{"panel": "p1", "checks": ["identity"]}],
        "inspected_entities": [{"entity": "邓正虎", "regions": ["face"]}],
        "checks": {
            name: True
            for name in (
                "identity",
                "facial_hair",
                "anatomy",
                "costume",
                "prop",
                "scene",
                "style",
                "text",
                "sfx",
            )
        },
        "findings": [],
        "classification": "unchanged",
        "classification_evidence": ["all required checks passed"],
        "artifact": {
            "path": "artifacts/场景/252（1）-continuity.png",
            "sha256": "b" * 64,
            "kind": "full_resolution_page",
        },
    }
    audit.update(overrides)
    return audit


class AuditEvidenceRedTests(unittest.TestCase):
    def test_thumbnail_only_artifact_cannot_pass(self):
        audit = valid_audit(
            artifact={
                "path": "artifacts/contact-sheet.jpg",
                "sha256": "b" * 64,
                "kind": "contact_sheet",
            }
        )

        with self.assertRaisesRegex(ValueError, "full-resolution artifact"):
            record_page_audit(audit)

    def test_noncritical_action_variation_is_not_a_continuity_defect(self):
        continuity = valid_audit(
            findings=[
                {
                    "code": "NON_CRITICAL_ACTION_VARIATION",
                    "category": "source",
                    "blocking": False,
                }
            ]
        )
        source = valid_audit(
            perspective="source",
            reviewer="reviewer-b",
            artifact={
                "path": "artifacts/场景/252（1）-source.png",
                "sha256": "c" * 64,
                "kind": "full_resolution_page",
            },
        )

        result = aggregate_page_audits([continuity, source])

        self.assertEqual(result["decision"], "unchanged")

    def test_facial_hair_drift_at_medium_confidence_requires_second_review(self):
        continuity = valid_audit(
            confidence=0.72,
            findings=[
                {
                    "code": "FACIAL_HAIR_DRIFT",
                    "category": "visual",
                    "blocking": True,
                }
            ],
            classification="defect",
            classification_evidence=["beard differs from adjacent pages"],
        )
        source = valid_audit(
            perspective="source",
            reviewer="reviewer-b",
            confidence=0.72,
            artifact={
                "path": "artifacts/场景/252（1）-source.png",
                "sha256": "c" * 64,
                "kind": "full_resolution_page",
            },
        )

        result = aggregate_page_audits([continuity, source])

        self.assertEqual(result["decision"], "second_review_required")


class AuditRecordContractTests(unittest.TestCase):
    def test_public_constants_are_exact(self):
        self.assertEqual(HIGH_CONFIDENCE, 0.90)
        self.assertEqual(MEDIUM_CONFIDENCE, 0.60)
        self.assertEqual(
            REQUIRED_AUDIT_CHECKS,
            frozenset(
                {
                    "identity",
                    "facial_hair",
                    "anatomy",
                    "costume",
                    "prop",
                    "scene",
                    "style",
                    "text",
                    "sfx",
                }
            ),
        )
        self.assertEqual(
            NON_DEFECT_CODES,
            frozenset(
                {
                    "NON_CRITICAL_ACTION_VARIATION",
                    "CAMERA_VARIATION",
                    "EXPRESSION_VARIATION",
                }
            ),
        )

    def test_valid_unicode_nested_audit_is_canonical_and_does_not_mutate_input(self):
        audit = valid_audit()
        original = copy.deepcopy(audit)

        result = record_page_audit(audit)

        self.assertEqual(audit, original)
        self.assertEqual(result["page"], "场景/252（1）.jpg")
        self.assertEqual(result["evidence_hash"], record_page_audit(audit)["evidence_hash"])

    def test_unchanged_requires_all_checks_true_and_no_blocking_findings(self):
        false_check = valid_audit()
        false_check["checks"]["facial_hair"] = False
        with self.assertRaisesRegex(ValueError, "unchanged.*required checks"):
            record_page_audit(false_check)

        blocked = valid_audit(
            findings=[
                {"code": "TEXT_ERROR", "category": "text", "blocking": True}
            ]
        )
        with self.assertRaisesRegex(ValueError, "unchanged.*blocking"):
            record_page_audit(blocked)

    def test_rejects_missing_extra_or_non_boolean_checks(self):
        for checks in (
            {key: True for key in REQUIRED_AUDIT_CHECKS if key != "sfx"},
            {**{key: True for key in REQUIRED_AUDIT_CHECKS}, "novel_action": True},
            {**{key: True for key in REQUIRED_AUDIT_CHECKS}, "identity": 1},
        ):
            with self.subTest(checks=checks):
                with self.assertRaisesRegex(ValueError, "checks"):
                    record_page_audit(valid_audit(checks=checks))

    def test_rejects_unsafe_duplicate_or_bad_artifacts(self):
        cases = [
            ("page", "../252.jpg", "page"),
            ("source_sha256", "ABC", "sha256"),
            ("artifact", {"path": "../proof.png", "sha256": "b" * 64, "kind": "full_resolution_page"}, "artifact path"),
            ("artifact", {"path": "proof.png", "sha256": "x" * 64, "kind": "full_resolution_page"}, "sha256"),
            ("inspected_panels", [{"panel": "p1", "checks": ["identity"]}, {"panel": "p1", "checks": ["scene"]}], "duplicate"),
            ("inspected_entities", [{"entity": "甲", "regions": ["face"]}, {"entity": "甲", "regions": ["hair"]}], "duplicate"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    record_page_audit(valid_audit(**{field: value}))

    def test_rejects_bad_types_dimensions_timestamp_confidence_and_structure(self):
        cases = [
            ("width", True, "width"),
            ("height", 0, "height"),
            ("perspective", "editor", "perspective"),
            ("reviewer", " ", "reviewer"),
            ("reviewed_at", "2026-07-15T08:00:00", "timezone"),
            ("confidence", True, "confidence"),
            ("confidence", math.nan, "confidence"),
            ("confidence", 1.01, "confidence"),
            ("inspected_panels", [], "inspected_panels"),
            ("inspected_panels", ["p1"], "inspected_panels"),
            ("inspected_entities", [], "inspected_entities"),
            ("findings", {}, "findings"),
            ("classification_evidence", [], "classification_evidence"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, message):
                    record_page_audit(valid_audit(**{field: value}))

    def test_finding_requires_strict_fields(self):
        for finding in (
            {"code": "", "category": "visual", "blocking": True},
            {"code": "STYLE_DRIFT", "category": "other", "blocking": True},
            {"code": "STYLE_DRIFT", "category": "visual", "blocking": 1},
        ):
            with self.subTest(finding=finding):
                with self.assertRaisesRegex(ValueError, "finding"):
                    record_page_audit(
                        valid_audit(
                            findings=[finding],
                            classification="defect",
                            classification_evidence=["observed mismatch"],
                        )
                    )


class DualAuditAndRoutingTests(unittest.TestCase):
    def source_audit(self, **overrides):
        defaults = {
            "perspective": "source",
            "reviewer": "reviewer-b",
            "artifact": {
                "path": "artifacts/场景/252（1）-source.png",
                "sha256": "c" * 64,
                "kind": "full_resolution_page",
            },
        }
        defaults.update(overrides)
        return valid_audit(**defaults)

    def test_dual_audit_requires_both_perspectives_and_independent_reviewers(self):
        continuity = valid_audit()
        with self.assertRaisesRegex(ValueError, "both.*perspectives"):
            aggregate_page_audits([continuity])
        with self.assertRaisesRegex(ValueError, "independent reviewer"):
            aggregate_page_audits(
                [continuity, self.source_audit(reviewer="reviewer-a")]
            )

    def test_dual_audits_must_bind_same_page_source_and_dimensions(self):
        for overrides, message in (
            ({"page": "场景/253.jpg"}, "same page"),
            ({"source_sha256": "d" * 64}, "source_sha256"),
            ({"width": 999}, "dimensions"),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    aggregate_page_audits([valid_audit(), self.source_audit(**overrides)])

    def test_continuity_nondefect_codes_remain_clean(self):
        for code in sorted(NON_DEFECT_CODES):
            with self.subTest(code=code):
                continuity = valid_audit(
                    findings=[
                        {"code": code, "category": "source", "blocking": False}
                    ]
                )
                result = aggregate_page_audits([continuity, self.source_audit()])
                self.assertEqual(result["decision"], "unchanged")

    def test_high_confidence_routes_text_visual_and_combined_deterministically(self):
        self.assertEqual(
            route_page_decision(
                [{"code": "TEXT_ERROR", "category": "text", "blocking": True}],
                0.90,
            ),
            "text_only",
        )
        self.assertEqual(
            route_page_decision(
                [{"code": "STYLE_DRIFT", "category": "visual", "blocking": True}],
                1.0,
            ),
            "full_page_redraw",
        )
        combined = [
            {"code": "TEXT_ERROR", "category": "text", "blocking": True},
            {"code": "FACIAL_HAIR_DRIFT", "category": "visual", "blocking": True},
        ]
        self.assertEqual(route_page_decision(combined, 0.95), "full_page_redraw")
        self.assertEqual(
            route_page_decision(list(reversed(combined)), 0.95),
            "full_page_redraw",
        )

    def test_confidence_boundaries_and_disagreement(self):
        defect = [{"code": "FACIAL_HAIR_DRIFT", "category": "visual", "blocking": True}]
        self.assertEqual(route_page_decision([], 0.90), "unchanged")
        self.assertEqual(route_page_decision(defect, 0.899999), "second_review_required")
        self.assertEqual(route_page_decision(defect, 0.60), "second_review_required")
        self.assertEqual(route_page_decision(defect, 0.599999), "evidence_blocked")
        self.assertEqual(
            route_page_decision(defect, 1.0, perspective_disagreement=True),
            "evidence_blocked",
        )

    def test_unknown_code_cannot_silently_route_clean(self):
        self.assertEqual(
            route_page_decision(
                [{"code": "SOMETHING_NEW", "category": "visual", "blocking": False}],
                1.0,
            ),
            "evidence_blocked",
        )

    def test_route_rejects_boolean_and_nonfinite_confidence_without_mutation(self):
        findings = [{"code": "TEXT_ERROR", "category": "text", "blocking": True}]
        original = copy.deepcopy(findings)
        for confidence in (True, math.inf):
            with self.subTest(confidence=confidence):
                with self.assertRaisesRegex(ValueError, "confidence"):
                    route_page_decision(findings, confidence)
        self.assertEqual(findings, original)


class AppendOnlyReviewLogTests(unittest.TestCase):
    def event(self, sequence):
        return {
            "type": "page_audit_recorded",
            "page": "场景/252（1）.jpg",
            "sequence": sequence,
            "evidence_hash": f"{sequence:064x}",
        }

    def test_append_creates_canonical_hash_chain_without_mutating_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            first = self.event(1)
            original = copy.deepcopy(first)
            row1 = append_review_event(path, first)
            row2 = append_review_event(path, self.event(2))

            self.assertEqual(first, original)
            self.assertIsNone(row1["previous_event_hash"])
            self.assertEqual(row2["previous_event_hash"], row1["event_hash"])
            self.assertEqual(validate_review_log(path), [row1, row2])
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            lines = raw.decode("utf-8").splitlines()
            self.assertEqual(
                lines[0],
                json.dumps(row1, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            )

    def test_validation_rejects_tamper_truncation_blank_and_malformed_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            append_review_event(path, self.event(1))
            good = path.read_bytes()
            cases = [
                good.replace(b'"sequence":1', b'"sequence":9'),
                good.rstrip(b"\n"),
                good + b"\n",
                good + b"not-json\n",
            ]
            for index, bad in enumerate(cases):
                bad_path = Path(tmp) / f"bad-{index}.jsonl"
                bad_path.write_bytes(bad)
                with self.subTest(index=index):
                    with self.assertRaisesRegex(ValueError, "review log"):
                        validate_review_log(bad_path)

    def test_append_refuses_existing_corruption_and_unsafe_event_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            path.write_text("{}\n", encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "review log"):
                append_review_event(path, self.event(2))
            self.assertEqual(path.read_bytes(), before)

            clean = Path(tmp) / "clean.jsonl"
            with self.assertRaisesRegex(ValueError, "page"):
                append_review_event(clean, {**self.event(1), "page": "../bad.jpg"})
            self.assertFalse(clean.exists())

    def test_append_rejects_reserved_chain_fields_and_bad_log_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = {**self.event(1), "event_hash": "a" * 64}
            with self.assertRaisesRegex(ValueError, "reserved"):
                append_review_event(Path(tmp) / "reviews.jsonl", event)
            with self.assertRaisesRegex(ValueError, "jsonl"):
                append_review_event(Path(tmp) / "reviews.txt", self.event(1))


if __name__ == "__main__":
    unittest.main()
