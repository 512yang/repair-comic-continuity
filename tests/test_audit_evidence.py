import sys
import tempfile
import unittest
import copy
import hashlib
import json
import math
import subprocess
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
from pipeline_contracts import canonical_hash  # noqa: E402


_CHECK_BY_CODE = {
    "IDENTITY_DRIFT": "identity",
    "FACIAL_HAIR_DRIFT": "facial_hair",
    "ANATOMY_ERROR": "anatomy",
    "COSTUME_DRIFT": "costume",
    "PROP_DRIFT": "prop",
    "SCENE_DRIFT": "scene",
    "STYLE_DRIFT": "style",
    "TEXT_ERROR": "text",
    "SFX_ERROR": "sfx",
}


def finding(code, *, category=None, blocking=True, region="panel-1", **overrides):
    if category is None:
        category = "text" if code in {"TEXT_ERROR", "SFX_ERROR"} else "visual"
    if code in NON_DEFECT_CODES:
        blocking = False
        category = "source"
    evidence_sha = hashlib.sha256(f"{code}:{region}".encode("utf-8")).hexdigest()
    result = {
        "finding_id": hashlib.sha256(
            f"finding:{code}:{region}:{evidence_sha}".encode("utf-8")
        ).hexdigest(),
        "code": code,
        "category": category,
        "blocking": blocking,
        "confidence": 0.95,
        "location": region,
        "evidence": {
            "path": f"evidence/{code.casefold()}-{region}.png",
            "sha256": evidence_sha,
        },
        "repair_scope": (
            "none"
            if not blocking
            else "text_only"
            if category == "text"
            else "full_page_redraw"
        ),
    }
    result.update(overrides)
    return result


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
    if audit["classification"] == "defect" and "checks" not in overrides:
        audit["checks"] = dict(audit["checks"])
        for item in audit["findings"]:
            required_check = _CHECK_BY_CODE.get(item.get("code"))
            if item.get("blocking") is True and required_check:
                audit["checks"][required_check] = False
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
            findings=[finding("NON_CRITICAL_ACTION_VARIATION")]
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
        result = route_page_decision(
            [finding("FACIAL_HAIR_DRIFT")],
            0.72,
        )

        self.assertEqual(result, "second_review_required")


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
            findings=[finding("TEXT_ERROR")]
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
        for invalid_finding in (
            {**finding("STYLE_DRIFT"), "code": ""},
            finding("STYLE_DRIFT", category="other"),
            finding("STYLE_DRIFT", blocking=1),
        ):
            with self.subTest(finding=invalid_finding):
                with self.assertRaisesRegex(ValueError, "finding"):
                    record_page_audit(
                        valid_audit(
                            findings=[invalid_finding],
                            classification="defect",
                            classification_evidence=["observed mismatch"],
                        )
                    )

    def test_finding_registry_rejects_semantic_spoofing_and_requires_failed_check(self):
        for spoof in (
            finding("STYLE_DRIFT", category="text"),
            finding("STYLE_DRIFT", blocking=False),
            finding("STYLE_DRIFT", repair_scope="text_only"),
        ):
            with self.subTest(spoof=spoof):
                with self.assertRaisesRegex(ValueError, "STYLE_DRIFT"):
                    record_page_audit(
                        valid_audit(
                            findings=[spoof],
                            classification="defect",
                            classification_evidence=["spoof attempt"],
                        )
                    )

        audit = valid_audit(
            findings=[finding("STYLE_DRIFT")],
            classification="defect",
            classification_evidence=["line work mismatch"],
        )
        audit["checks"]["style"] = True
        with self.assertRaisesRegex(ValueError, "style.*check"):
            record_page_audit(audit)

    def test_same_code_in_different_regions_can_coexist(self):
        audit = valid_audit(
            findings=[
                finding("STYLE_DRIFT", region="panel-1"),
                finding("STYLE_DRIFT", region="panel-3"),
            ],
            classification="defect",
            classification_evidence=["two separately inspected regions drift"],
        )

        result = record_page_audit(audit)

        self.assertEqual(len(result["findings"]), 2)
        self.assertNotEqual(
            result["findings"][0]["finding_id"],
            result["findings"][1]["finding_id"],
        )

    def test_finding_requires_confidence_location_evidence_and_scope(self):
        base = finding("STYLE_DRIFT")
        for missing in ("confidence", "location", "evidence", "repair_scope"):
            invalid = dict(base)
            del invalid[missing]
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, missing):
                    record_page_audit(
                        valid_audit(
                            findings=[invalid],
                            classification="defect",
                            classification_evidence=["incomplete finding"],
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

        with self.assertRaisesRegex(ValueError, "independent reviewer"):
            aggregate_page_audits(
                [continuity, self.source_audit(reviewer="ＲＥＶＩＥＷＥＲ－Ａ")]
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
                    findings=[finding(code)]
                )
                result = aggregate_page_audits([continuity, self.source_audit()])
                self.assertEqual(result["decision"], "unchanged")

    def test_high_confidence_routes_text_visual_and_combined_deterministically(self):
        self.assertEqual(
            route_page_decision(
                [finding("TEXT_ERROR")],
                0.90,
            ),
            "text_only",
        )
        self.assertEqual(
            route_page_decision(
                [finding("STYLE_DRIFT")],
                1.0,
            ),
            "full_page_redraw",
        )
        combined = [
            finding("TEXT_ERROR"),
            finding("FACIAL_HAIR_DRIFT"),
        ]
        self.assertEqual(route_page_decision(combined, 0.95), "full_page_redraw")
        self.assertEqual(
            route_page_decision(list(reversed(combined)), 0.95),
            "full_page_redraw",
        )

    def test_confidence_boundaries_and_disagreement(self):
        defect = [finding("FACIAL_HAIR_DRIFT")]
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
                [finding("SOMETHING_NEW", blocking=False)],
                1.0,
            ),
            "evidence_blocked",
        )

    def test_high_confidence_classification_disagreement_is_evidence_blocked(self):
        continuity = valid_audit(
            findings=[finding("FACIAL_HAIR_DRIFT")],
            classification="defect",
            classification_evidence=["beard differs from stable anchor"],
        )

        result = aggregate_page_audits([continuity, self.source_audit()])

        self.assertEqual(result["decision"], "evidence_blocked")
        self.assertTrue(result["perspective_disagreement"])

    def test_matching_finding_from_both_reviewers_is_confirmed_not_duplicate(self):
        shared_finding = finding("STYLE_DRIFT")
        continuity = valid_audit(
            findings=[shared_finding],
            classification="defect",
            classification_evidence=["line work differs"],
        )
        source = self.source_audit(
            findings=[shared_finding],
            classification="defect",
            classification_evidence=["line work differs"],
        )

        result = aggregate_page_audits([continuity, source])

        self.assertEqual(result["decision"], "full_page_redraw")
        self.assertEqual(
            result["finding_evidence"][0]["perspectives"],
            ["continuity", "source"],
        )

    def test_dual_finding_group_preserves_each_complete_report(self):
        continuity_finding = finding("STYLE_DRIFT", region="panel-2")
        source_finding = finding(
            "STYLE_DRIFT",
            region="panel-2",
            finding_id="d" * 64,
            evidence={"path": "evidence/source-panel-2.png", "sha256": "e" * 64},
        )
        continuity = valid_audit(
            findings=[continuity_finding],
            classification="defect",
            classification_evidence=["continuity anchor mismatch"],
        )
        source = self.source_audit(
            findings=[source_finding],
            classification="defect",
            classification_evidence=["source review independently confirms"],
        )

        result = aggregate_page_audits([continuity, source])

        reports = result["finding_evidence"][0]["reports"]
        self.assertEqual([row["perspective"] for row in reports], ["continuity", "source"])
        self.assertEqual(reports[0]["finding"], continuity_finding)
        self.assertEqual(reports[1]["finding"], source_finding)

    def test_any_blocked_audit_keeps_dual_aggregate_evidence_blocked(self):
        blocked = valid_audit(
            classification="evidence_blocked",
            classification_evidence=["full-resolution anchor unavailable"],
        )
        unchanged = self.source_audit()
        defect = self.source_audit(
            findings=[finding("TEXT_ERROR")],
            classification="defect",
            classification_evidence=["source string differs"],
        )
        source_blocked = self.source_audit(
            classification="evidence_blocked",
            classification_evidence=["novel alignment unresolved"],
        )

        for source in (unchanged, defect, source_blocked):
            with self.subTest(source_classification=source["classification"]):
                result = aggregate_page_audits([blocked, source])
                self.assertEqual(result["decision"], "evidence_blocked")

    def test_route_rejects_boolean_and_nonfinite_confidence_without_mutation(self):
        findings = [finding("TEXT_ERROR")]
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

    def test_append_roundtrips_to_real_json_types_before_hash_and_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            event = {**self.event(1), "coordinates": (1, 2)}

            row = append_review_event(path, event)

            self.assertEqual(row["coordinates"], [1, 2])
            self.assertEqual(validate_review_log(path)[0], row)

    def test_four_processes_append_complete_continuous_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            processes = self._spawn_appenders(path, workers=4, events_per_worker=5)
            self._assert_processes_succeed(processes)

            rows = validate_review_log(path)

            self.assertEqual(len(rows), 20)
            self.assertEqual(len({row["event_hash"] for row in rows}), 20)
            self.assertTrue(Path(str(path) + ".lock").is_file())

    def test_injected_process_failure_does_not_damage_other_process_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            processes = self._spawn_appenders(
                path, workers=4, events_per_worker=4, include_failure=True
            )
            self._assert_processes_succeed(processes)

            rows = validate_review_log(path)

            self.assertEqual(len(rows), 16)
            self.assertNotIn("injected-failure", {row["type"] for row in rows})

    def _spawn_appenders(self, path, *, workers, events_per_worker, include_failure=False):
        script = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from audit_evidence import append_review_event
path = Path(sys.argv[2])
worker = int(sys.argv[3])
count = int(sys.argv[4])
fail = sys.argv[5] == "1"
if fail:
    try:
        append_review_event(path, {"type": "injected-failure", "page": "scene/fail.jpg", "evidence_hash": "f" * 64}, _fault_after_write=True)
    except OSError:
        pass
else:
    for index in range(count):
        value = worker * 100 + index
        append_review_event(path, {"type": "page_audit_recorded", "page": f"scene/{worker}-{index}.jpg", "evidence_hash": f"{value:064x}"})
'''
        scripts_dir = str(ROOT / "scripts")
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, scripts_dir, str(path), str(worker), str(events_per_worker), "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for worker in range(workers)
        ]
        if include_failure:
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, scripts_dir, str(path), "99", "0", "1"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        return processes

    def _assert_processes_succeed(self, processes):
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            results.append((process.returncode, stdout, stderr))
        failures = [result for result in results if result[0] != 0]
        self.assertFalse(failures, repr(failures))

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

    def test_existing_hash_valid_event_is_still_rejected_when_schema_is_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviews.jsonl"
            row = {
                **self.event(1),
                "page": "../unsafe.jpg",
                "previous_event_hash": None,
            }
            row["event_hash"] = canonical_hash(row)
            payload = (
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            path.write_bytes(payload.encode("utf-8"))

            with self.assertRaisesRegex(ValueError, "review log.*schema"):
                validate_review_log(path)
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "review log.*schema"):
                append_review_event(path, self.event(2))
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
