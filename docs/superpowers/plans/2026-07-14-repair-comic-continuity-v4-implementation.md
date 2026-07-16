# Repair Comic Continuity V4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an enforceable V4 comic-continuity Skill that audits every page at full resolution, repairs only confirmed defects, rejects fabricated evidence, and preserves every input relative path, filename, extension, and correct-page byte sequence.

**Architecture:** Keep `SKILL.md` as the concise coordinator contract and move deterministic behavior into focused Python modules. V4 separates immutable evidence, audit, repair, and promotion; new runs use `pipeline_mode: continuity_v4` and `schema_version: 4.0`, while V3 remains diagnostic-only. Every behavior change follows RED-GREEN-REFACTOR and is committed independently.

**Tech Stack:** Python 3.11, standard library, Pillow, `unittest`, PowerShell, Git, Codex Skill validator.

---

## File Map

**New modules**

- `scripts/source_text.py`: strict UTF-8/GB18030 novel decoding and hash binding.
- `scripts/entity_timeline.py`: character, prop, and scene state intervals and transitions.
- `scripts/audit_evidence.py`: full-resolution page audits, dual-review decisions, confidence routing, and hash-chained review events.
- `scripts/evidence_integrity.py`: anti-fabrication checks shared by audit and final validators.
- `scripts/validate_audit.py`: audit-only validation that forbids candidates and final promotion.

**Modified modules**

- `scripts/project_common.py`: UTF-8 path literals, recursive relative image discovery, dimension records, and extension preservation.
- `scripts/inventory_project.py`: exact relative-path inventory and novel decoding metadata.
- `scripts/pipeline_contracts.py`: exact-name bijection and relative-path normalization.
- `scripts/build_output_manifest.py`: V4 evidence initialization and exact-name mappings.
- `scripts/scene_clusters.py`: semantic short-scene support and role-complete reference packs.
- `scripts/prompt_compiler.py`: continuity-first full-page profile and deterministic text geometry.
- `scripts/candidate_preflight.py`: exact dimensions, text-mask preservation, composition artifacts, and blind review bindings.
- `scripts/failure_learning.py`: positive, clean-control, variation, and independent-review promotion gates.
- `scripts/task_queue.py`: two-attempt failure-family circuit breaker.
- `scripts/validate_output.py`: V4 registry integration, exact-name output checks, and final status enforcement.
- `scripts/migrate_scene_pipeline.py`: V3 diagnosis without unsafe V4 promotion.

**Tests and fixtures**

- `tests/fixtures/failed_run_summary.json`: copyright-free metadata reproducing the known failed-run contracts.
- `tests/test_source_text.py`
- `tests/test_entity_timeline.py`
- `tests/test_audit_evidence.py`
- `tests/test_evidence_integrity.py`
- `tests/test_validate_audit.py`
- Existing tests under `tests/` are updated in the same task as their production module.

**Skill documentation**

- `SKILL.md`
- `references/continuity-rules.md`
- `references/scene-cluster-pipeline.md`
- `references/qa-checklist.md`
- `references/failure-learning.md`
- `agents/openai.yaml`

---

### Task 1: Capture the failed run as RED regression evidence

**Files:**
- Create: `tests/fixtures/failed_run_summary.json`
- Create: `tests/test_evidence_integrity.py`
- Create: `scripts/evidence_integrity.py`

- [ ] **Step 1: Add the metadata-only failure fixture**

```json
{
  "input_names": ["191.jpg", "252（1）.jpg"],
  "output_names": ["0003.jpg", "0065.jpg"],
  "alignment_statuses": ["unconfirmed", "unconfirmed"],
  "page_qa_statuses": ["pending", "pending"],
  "cluster_qa_count": 0,
  "stable_pages": [],
  "reference_packs": [
    {
      "cluster_id": "cluster-001",
      "references": [
        {"path": "人物参考图/刘正胤.png", "role": "identity_only"}
      ]
    }
  ],
  "final_status": "pending"
}
```

- [ ] **Step 2: Write failing integrity tests**

```python
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_integrity import validate_failed_run_summary


class EvidenceIntegrityRegressionTests(unittest.TestCase):
    def test_known_failed_run_is_rejected_for_every_known_gate(self):
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "failed_run_summary.json").read_text(
                encoding="utf-8"
            )
        )
        errors = validate_failed_run_summary(fixture)
        self.assertEqual(
            errors,
            [
                "OUTPUT_NAME_SET_MISMATCH",
                "ALIGNMENT_UNCONFIRMED",
                "PAGE_QA_PENDING",
                "CLUSTER_QA_MISSING",
                "STABLE_STYLE_ANCHOR_MISSING",
                "REFERENCE_CAST_COVERAGE_UNPROVEN",
                "FINAL_STATUS_NOT_PASSED",
            ],
        )

    def test_mass_filled_review_without_artifacts_is_rejected(self):
        errors = validate_failed_run_summary(
            {
                "input_names": ["191.jpg"],
                "output_names": ["191.jpg"],
                "alignment_statuses": ["confirmed"],
                "page_qa_statuses": ["passed"],
                "cluster_qa_count": 1,
                "stable_pages": ["191.jpg"],
                "reference_packs": [],
                "final_status": "passed",
                "review_artifacts": [],
                "full_size_flags": [True],
            }
        )
        self.assertIn("FULL_SIZE_ARTIFACT_MISSING", errors)
```

- [ ] **Step 3: Run the tests and verify RED**

Run:

```powershell
python -m unittest tests.test_evidence_integrity -v
```

Expected: FAIL because `scripts/evidence_integrity.py` or `validate_failed_run_summary` does not exist.

- [ ] **Step 4: Add the minimal deterministic failure classifier**

```python
"""Evidence-integrity gates that reject post-hoc or incomplete review data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def validate_failed_run_summary(summary: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if set(summary.get("input_names", [])) != set(summary.get("output_names", [])):
        errors.append("OUTPUT_NAME_SET_MISMATCH")
    if any(value != "confirmed" for value in summary.get("alignment_statuses", [])):
        errors.append("ALIGNMENT_UNCONFIRMED")
    if any(value != "passed" for value in summary.get("page_qa_statuses", [])):
        errors.append("PAGE_QA_PENDING")
    if summary.get("cluster_qa_count", 0) == 0:
        errors.append("CLUSTER_QA_MISSING")
    if not summary.get("stable_pages"):
        errors.append("STABLE_STYLE_ANCHOR_MISSING")
    if summary.get("reference_packs") and not summary.get("cast_coverage"):
        errors.append("REFERENCE_CAST_COVERAGE_UNPROVEN")
    if summary.get("final_status") != "passed":
        errors.append("FINAL_STATUS_NOT_PASSED")
    flags = summary.get("full_size_flags", [])
    artifacts = summary.get("review_artifacts", [])
    if any(flags) and not artifacts:
        errors.append("FULL_SIZE_ARTIFACT_MISSING")
    return errors
```

- [ ] **Step 5: Run GREEN and commit**

Run:

```powershell
python -m unittest tests.test_evidence_integrity -v
git add scripts/evidence_integrity.py tests/test_evidence_integrity.py tests/fixtures/failed_run_summary.json
git commit -m "test: capture failed continuity run gates"
```

Expected: 2 tests pass; commit succeeds.

---

### Task 2: Preserve exact relative filenames and decode novels safely

**Files:**
- Create: `scripts/source_text.py`
- Create: `tests/test_source_text.py`
- Modify: `scripts/project_common.py`
- Modify: `scripts/inventory_project.py`
- Modify: `scripts/pipeline_contracts.py`
- Modify: `tests/test_pipeline_contracts.py`
- Modify: `tests/test_project_tools.py`

- [ ] **Step 1: Replace old renumbering tests with exact-name and encoding tests**

```python
def test_validate_bijection_requires_exact_relative_names(self):
    mappings = [
        {"source_page": "189.jpg", "output_name": "189.jpg"},
        {"source_page": "252（1）.jpg", "output_name": "252（1）.jpg"},
    ]
    pipeline_contracts.validate_bijection(
        ["189.jpg", "252（1）.jpg"],
        mappings,
        actual_outputs=["189.jpg", "252（1）.jpg"],
    )
    with self.assertRaisesRegex(ValueError, "output must preserve source relative path"):
        pipeline_contracts.validate_bijection(
            ["189.jpg", "252（1）.jpg"],
            [
                {"source_page": "189.jpg", "output_name": "0001.jpg"},
                {"source_page": "252（1）.jpg", "output_name": "0002.jpg"},
            ],
        )
```

```python
def test_decode_novel_uses_strict_utf8_then_gb18030(self):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        utf8 = root / "utf8.txt"
        gb = root / "gb.txt"
        utf8.write_bytes("丹符神尊".encode("utf-8"))
        gb.write_bytes("强壮遇见".encode("gb18030"))
        self.assertEqual(decode_novel(utf8)["encoding"], "utf-8")
        self.assertEqual(decode_novel(gb)["encoding"], "gb18030")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_pipeline_contracts tests.test_source_text tests.test_project_tools -v
```

Expected: exact-name tests fail because V3 requires four-digit JPG output; source decoder import fails.

- [ ] **Step 3: Implement strict novel decoding**

```python
"""Read novel bytes without modifying source material."""

from __future__ import annotations

import hashlib
from pathlib import Path


def decode_novel(path: Path) -> dict[str, str]:
    raw = Path(path).read_bytes()
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    encodings = ("utf-8-sig",) if raw.startswith(b"\xef\xbb\xbf") else ("utf-8", "gb18030")
    failures: list[str] = []
    for encoding in encodings:
        try:
            text = raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            failures.append(encoding)
            continue
        canonical_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
        return {
            "text": text,
            "encoding": canonical_encoding,
            "raw_sha256": raw_sha256,
            "decoded_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    raise ValueError(f"novel decoding failed for strict encodings: {failures}")
```

- [ ] **Step 4: Implement exact relative-path inventory and bijection**

Change `INPUT_PAGE_EXTENSIONS` to:

```python
INPUT_PAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
```

Add to `project_common.py`:

```python
def relative_page_name(input_dir: Path, page: Path) -> str:
    return page.relative_to(input_dir).as_posix()
```

Replace flat input discovery with:

```python
pages = [
    path
    for path in input_dir.rglob("*")
    if path.is_file() and path.suffix.casefold() in INPUT_PAGE_EXTENSIONS
]
pages.sort(key=lambda path: tuple(natural_page_key(Path(part)) for part in path.relative_to(input_dir).parts))
```

Repair the required project directory literals exactly:

```python
required = {
    "人物参考图": root / "人物参考图",
    "输入": root / "输入",
    "输出": root / "输出",
}
```

Replace `make_output_names` with:

```python
def make_output_names(inputs: Iterable[object]) -> list[str]:
    names = [normalize_relative_image_path(value) for value in inputs]
    if not names:
        raise ValueError("inputs must not be empty")
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("duplicate input relative path")
    return names
```

Make `validate_bijection` require `output_name == normalize_relative_image_path(source_page)` for every row and compare exact normalized relative-path sets for `actual_outputs`.

Update inventory rows to contain `relative_path`, `extension`, `width`, `height`, and the decoder metadata from `decode_novel`.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_pipeline_contracts tests.test_source_text tests.test_project_tools -v
git add scripts/source_text.py scripts/project_common.py scripts/inventory_project.py scripts/pipeline_contracts.py tests/test_source_text.py tests/test_pipeline_contracts.py tests/test_project_tools.py
git commit -m "feat: preserve source names and decode novels safely"
```

Expected: focused suites pass with exact-name mappings and both encodings.

---

### Task 3: Initialize the V4 evidence set without fake completion

**Files:**
- Modify: `scripts/build_output_manifest.py`
- Modify: `tests/test_project_tools.py`

- [ ] **Step 1: Write a failing V4 manifest test**

```python
def test_manifest_initializes_v4_exact_names_and_required_registries(self):
    manifest = build_output_manifest.build_manifests(self.root, self.evidence)
    self.assertEqual(manifest["pipeline_mode"], "continuity_v4")
    self.assertEqual(manifest["schema_version"], "4.0")
    self.assertEqual(
        [row["output_name"] for row in manifest["pages"]],
        [row["input_name"] for row in manifest["pages"]],
    )
    for name in (
        "entity_state_timeline.json",
        "page_audit.json",
        "review_events.jsonl",
        "text_geometry.json",
        "regression_summary.json",
    ):
        self.assertTrue((self.evidence / name).is_file(), name)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_project_tools.ComicContinuityToolsTests.test_manifest_initializes_v4_exact_names_and_required_registries -v
```

Expected: FAIL because the manifest is V3 and V4 registries do not exist.

- [ ] **Step 3: Change constants and initialization**

Use:

```python
PIPELINE_MODE = "continuity_v4"
SCHEMA_VERSION = "4.0"
EVIDENCE_FILES = (
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
)
```

Initialize every JSON registry with `status: pending` or empty collections, initialize `review_events.jsonl` as an empty UTF-8 file, and set every page's `output_name` equal to its input relative path. Remove `_build_batches` from the V4 initialization path; semantic clusters own grouping later.

- [ ] **Step 4: Prove manifest creation is atomic and never writes final output**

Extend the existing rollback test to include all V4 evidence files, and assert the output tree is unchanged before and after `build_manifests`.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_project_tools -v
git add scripts/build_output_manifest.py tests/test_project_tools.py
git commit -m "feat: initialize continuity v4 evidence"
```

Expected: project-tool suite passes; no final image is created.

---

### Task 4: Build semantic clusters and cast-complete reference packs

**Files:**
- Modify: `scripts/scene_clusters.py`
- Modify: `tests/test_scene_clusters.py`

- [ ] **Step 1: Write failing semantic-boundary tests**

```python
def test_short_semantic_scene_is_allowed_without_cross_scene_merge(self):
    pages = [
        make_page("189", chapter="十", location="飞龙泉", story_time="夜", scene_id="泉中"),
        make_page("190", chapter="十", location="飞龙泉", story_time="夜", scene_id="泉中"),
        make_page("191", chapter="十", location="一道宗", story_time="晨", scene_id="大殿"),
    ]
    clusters = scene_clusters.build_scene_clusters(pages)
    self.assertEqual([row["member_pages"] for row in clusters], [["189", "190"], ["191"]])
    self.assertFalse(any(row["blocked"] for row in clusters))
    self.assertEqual(clusters[0]["short_scene_reason"], "semantic_scene_below_preferred_size")


def test_visual_reference_pack_requires_cast_and_stable_style_coverage(self):
    cluster = {
        "cluster_id": "cluster-a",
        "member_pages": ["189"],
        "cast": ["邓正虎", "天朗真人"],
        "has_visual_tasks": True,
    }
    references = [
        {"path": "人物参考图/刘正胤.png", "role": "identity_only", "subject": "刘正胤"}
    ]
    with self.assertRaisesRegex(ValueError, "cast coverage"):
        scene_clusters.build_reference_pack(cluster, references, stable_pages=[])
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_scene_clusters -v
```

Expected: short scenes are blocked and reference roles lack cast coverage.

- [ ] **Step 3: Implement semantic short-scene controls and V4 roles**

Set:

```python
REFERENCE_ROLES = frozenset(
    {"target_composition", "comic_style_anchor", "identity_only", "prop_anchor", "scene_anchor"}
)
```

Change `cluster_size_controls` so a semantically bounded short scene records `short_scene_reason` and remains unblocked. Add cast, persistent props, costume state, scene fingerprint, boundary reason, confidence, and `has_visual_tasks`. Set `canary_page` to `None` when no visual task exists; otherwise select the highest-risk visual member.

- [ ] **Step 4: Enforce reference role and subject coverage**

Require at least one `target_composition` per visual target, at least one reviewed `comic_style_anchor`, and one `identity_only` reference or reviewed comic identity anchor for every named character under repair. Require prop and scene anchors only when their corresponding issue type is scheduled.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_scene_clusters -v
git add scripts/scene_clusters.py tests/test_scene_clusters.py
git commit -m "feat: enforce semantic clusters and reference coverage"
```

Expected: all scene-cluster tests pass.

---

### Task 5: Add long-range entity state timelines

**Files:**
- Create: `scripts/entity_timeline.py`
- Create: `tests/test_entity_timeline.py`

- [ ] **Step 1: Write failing character, prop, and scene timeline tests**

```python
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from entity_timeline import build_timeline, validate_timeline


class EntityTimelineTests(unittest.TestCase):
    def test_character_facial_hair_change_requires_transition_evidence(self):
        timeline = build_timeline(
            [
                {"page": "189.jpg", "entity_type": "character", "entity_id": "天朗真人", "state": {"beard": "long-black"}},
                {"page": "273.jpg", "entity_type": "character", "entity_id": "天朗真人", "state": {"beard": "none"}},
            ],
            transitions=[],
        )
        with self.assertRaisesRegex(ValueError, "unsupported state transition"):
            validate_timeline(timeline)

    def test_prop_transfer_with_source_evidence_is_valid(self):
        timeline = build_timeline(
            [
                {"page": "240.jpg", "entity_type": "prop", "entity_id": "奎金狼毫笔", "state": {"owner": "邓正虎", "material": "gold"}},
                {"page": "261.jpg", "entity_type": "prop", "entity_id": "奎金狼毫笔", "state": {"owner": "简正风", "material": "gold"}},
            ],
            transitions=[{"entity_id": "奎金狼毫笔", "from_page": "240.jpg", "to_page": "261.jpg", "kind": "transfer", "source_ref": "novel:120-145"}],
        )
        self.assertTrue(validate_timeline(timeline))
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_entity_timeline -v
```

Expected: FAIL because `entity_timeline` does not exist.

- [ ] **Step 3: Implement canonical timeline records**

Implement `build_timeline(observations, transitions)` to group canonical observations by `(entity_type, entity_id)`, preserve story order, hash state dictionaries, and return schema `1.0` with `entities` and `transitions`. Reject unknown entity types, empty IDs, duplicate observations, non-finite confidence, and transitions without source references.

- [ ] **Step 4: Validate long-range changes**

Implement `validate_timeline` so changes to identity, apparent age, skin, hair, facial hair, marks, build, costume, fixed props, prop owner/condition, or scene fingerprint require a matching transition between the two observations. Equivalent pose, grip, expression, camera angle, and non-critical arrangement fields are excluded from timeline state and cannot create an error.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_entity_timeline -v
git add scripts/entity_timeline.py tests/test_entity_timeline.py
git commit -m "feat: add long range continuity timelines"
```

Expected: timeline tests pass.

---

### Task 6: Enforce full-resolution dual audits and confidence routing

**Files:**
- Create: `scripts/audit_evidence.py`
- Create: `tests/test_audit_evidence.py`

- [ ] **Step 1: Write failing audit tests**

```python
def test_thumbnail_only_audit_cannot_pass(self):
    with self.assertRaisesRegex(ValueError, "full-resolution artifact"):
        record_page_audit(
            page="189.jpg",
            source_sha256="a" * 64,
            dimensions=(896, 1200),
            perspective="continuity",
            reviewer="reviewer-a",
            confidence=0.99,
            findings=[],
            artifacts=[{"path": "contact-sheet.jpg", "sha256": "b" * 64, "kind": "thumbnail"}],
            checks={name: True for name in REQUIRED_AUDIT_CHECKS},
            reviewed_at="2026-07-14T20:00:00+08:00",
        )


def test_action_equivalence_is_not_a_visual_defect(self):
    decision = route_page_decision(
        continuity_findings=[],
        source_findings=[{"code": "NON_CRITICAL_ACTION_VARIATION", "confidence": 1.0}],
    )
    self.assertEqual(decision["page_class"], "unchanged")


def test_medium_confidence_requires_second_review(self):
    decision = route_page_decision(
        continuity_findings=[{"code": "FACIAL_HAIR_DRIFT", "confidence": 0.72}],
        source_findings=[],
    )
    self.assertEqual(decision["status"], "second_review_required")
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_audit_evidence -v
```

Expected: FAIL because audit APIs do not exist.

- [ ] **Step 3: Implement exact audit schemas**

Define `REQUIRED_AUDIT_CHECKS` as identity, facial hair, anatomy, costume, prop, scene, style, text, and SFX. Require source hash, dimensions, inspected panels/entities, full-resolution review artifact hashes, perspective (`continuity` or `source`), reviewer, zoned timestamp, confidence, findings, and classification evidence. An unchanged classification requires every check and an empty blocking finding set.

- [ ] **Step 4: Implement routing and append-only events**

Use thresholds:

```python
HIGH_CONFIDENCE = 0.90
MEDIUM_CONFIDENCE = 0.60
NON_DEFECT_CODES = frozenset({"NON_CRITICAL_ACTION_VARIATION", "CAMERA_VARIATION", "EXPRESSION_VARIATION"})
```

High-confidence defects route to `text_only` or `full_page_redraw`; high-confidence clean pages route to `unchanged`; medium confidence requires independent review; low confidence or disagreement routes to `evidence_blocked`. Implement `append_review_event` with a previous-event hash and fsync so events are append-only and tamper-evident.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_audit_evidence -v
git add scripts/audit_evidence.py tests/test_audit_evidence.py
git commit -m "feat: require full resolution dual audits"
```

Expected: audit tests pass.

---

### Task 7: Compile full-page textless redraws and deterministic text geometry

**Files:**
- Modify: `scripts/prompt_compiler.py`
- Modify: `tests/test_prompt_compiler.py`

- [ ] **Step 1: Write failing repair-profile and geometry tests**

```python
def test_default_visual_profile_is_full_page_textless(self):
    spec = base_spec()
    spec["repair_profile"] = "continuity_first_full_page"
    request = prompt_compiler.compile_redraw_request(spec)
    self.assertEqual(request["repair_profile"], "continuity_first_full_page")
    self.assertTrue(request["textless_output"])
    self.assertIn("preserve panel topology", request["compiled_prompt"])


def test_text_geometry_is_required_and_new_balloon_is_rejected(self):
    spec = base_text_spec()
    spec["blocks"][0]["geometry"] = {
        "shape": "speech_balloon",
        "bbox": [100, 120, 420, 330],
        "orientation": "horizontal",
        "reading_order": 1,
        "source_balloon_exists": False,
    }
    with self.assertRaisesRegex(ValueError, "new dialogue balloon"):
        prompt_compiler.compile_text_repair_request(spec)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_prompt_compiler -v
```

Expected: repair profile and geometry fields are unknown or absent.

- [ ] **Step 3: Add V4 redraw profile**

Require `repair_profile: continuity_first_full_page`, exact target dimensions from the source page, original target as `target_composition`, at least one stable `comic_style_anchor`, identity-only character references, textless output, panel topology and reading-order preservation, and no final Chinese ordinary text. Remove local visual repair modes from the default profile; retain them only behind an explicit non-default project profile.

- [ ] **Step 4: Add deterministic text geometry contract**

Every text block must contain shape, bounding box, orientation, reading order, font profile, source-balloon existence, source text, replacement text, speaker, and exact source offsets. `page_reset` rebuilds only declared blocks. Reject newly introduced dialogue balloons, overlapping reading order, out-of-canvas geometry, unsupported fonts, and blocks exceeding density budgets.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_prompt_compiler -v
git add scripts/prompt_compiler.py tests/test_prompt_compiler.py
git commit -m "feat: compile full page redraw and text geometry"
```

Expected: prompt compiler suites pass.

---

### Task 8: Protect unchanged artwork and bind blind candidate review

**Files:**
- Modify: `scripts/candidate_preflight.py`
- Modify: `tests/test_candidate_preflight.py`

- [ ] **Step 1: Write failing preservation tests**

```python
def test_text_only_candidate_rejects_pixels_changed_outside_mask(self):
    report = candidate_preflight.run_candidate_preflight(
        self.candidate,
        self.original,
        expected_size=(896, 1200),
        text_policy="deterministic_text",
        change_mask=self.mask,
        page_class="text_only",
        ocr_metadata=VALID_OCR,
    )
    self.assertEqual(report["checks"]["outside_mask_preserved"]["status"], "fail")


def test_review_requires_full_resolution_artifacts_and_candidate_time_order(self):
    with self.assertRaisesRegex(ValueError, "review artifact"):
        candidate_preflight.record_independent_review(
            self.report,
            generator="worker-1",
            reviewer="reviewer-2",
            decision="accepted",
            reviewed_at="2026-07-14T20:00:00+00:00",
            candidate_created_at="2026-07-14T19:00:00+00:00",
            review_artifacts=[],
            blind=True,
        )


def test_text_candidate_requires_visual_glyph_review(self):
    with self.assertRaisesRegex(ValueError, "glyph review"):
        candidate_preflight.record_independent_review(
            self.report,
            generator="text-worker-1",
            reviewer="glyph-reviewer-2",
            decision="accepted",
            reviewed_at="2026-07-14T20:00:00+00:00",
            candidate_created_at="2026-07-14T19:00:00+00:00",
            review_artifacts=[{"path": "glyph-board.png", "sha256": "c" * 64, "kind": "full_resolution"}],
            blind=True,
            page_class="text_only",
            glyph_review=None,
        )
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_candidate_preflight -v
```

Expected: new arguments are unsupported.

- [ ] **Step 3: Add class-specific machine checks**

For `unchanged`, require candidate hash equals source hash. For `text_only`, compute differences outside the approved mask and allow only a one-pixel antialiasing boundary tolerance. For `full_page_redraw`, require exact dimensions plus hashed panel-boundary/composition evidence; do not use pixel equality. Replace the fixed `(896, 1200)` default with the source image dimensions.

- [ ] **Step 4: Bind blind review evidence**

Extend review records with candidate creation time, review-board hashes, inspected panels/entities, blind flag, and check matrix. Text-bearing candidates additionally require a glyph-review artifact, reviewer, result, and explicit inspection of every rendered Chinese block; the regression vocabulary includes `强` and `遇` without assuming OCR alone can detect their malformed shapes. Reject review before creation, missing full-resolution artifacts, generator/reviewer equality, and accepted decisions that do not bind the current candidate hash and preflight ID.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_candidate_preflight -v
git add scripts/candidate_preflight.py tests/test_candidate_preflight.py
git commit -m "feat: protect artwork and bind blind reviews"
```

Expected: preflight tests pass.

---

### Task 9: Gate failure learning and stop repeated generation failures

**Files:**
- Modify: `scripts/failure_learning.py`
- Modify: `scripts/task_queue.py`
- Modify: `tests/test_failure_learning.py`
- Modify: `tests/test_task_queue.py`

- [ ] **Step 1: Write failing rule-promotion and circuit-breaker tests**

```python
def test_rule_requires_positive_clean_control_variation_and_independent_review(self):
    with self.assertRaisesRegex(ValueError, "clean control"):
        promote_rule(
            self.store,
            failure_ids=self.failure_ids,
            scope="project",
            promoted_by="reviewer-2",
            positive_regression_passed=True,
            clean_control_passed=False,
            variation_passed=True,
            independently_reviewed=True,
            now=NOW,
        )
```

```python
def test_second_same_family_failure_pauses_lane(self):
    first = add(self.queue, "189", "a" * 64)
    second = add(self.queue, "190", "b" * 64)
    fail_claimed_task(self.queue, first, family="STYLE_DRIFT")
    fail_claimed_task(self.queue, second, family="STYLE_DRIFT")
    self.assertTrue(task_queue.is_lane_paused(self.queue, "c1", "redraw"))


def test_queue_keeps_one_coordinator_and_at_most_three_workers(self):
    queue = task_queue.new_queue()
    self.assertEqual(queue["max_active_workers"], 3)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_failure_learning tests.test_task_queue -v
```

Expected: new promotion evidence and family threshold are unsupported.

- [ ] **Step 3: Add rule-promotion evidence**

Store four booleans and their artifact hashes on every promoted rule: positive regression, clean control, varied case, and independent review. Reject promotion when any is absent or false. Preserve rule version, supersession chain, and revocation support.

- [ ] **Step 4: Add two-attempt family breaker**

Track `(cluster_id, task_type, failure_family)` attempts. Pause the lane at two failures, leave remaining tasks queued but unclaimable, and require an explicit reviewed diagnosis record before `reopen_lane` succeeds.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_failure_learning tests.test_task_queue -v
git add scripts/failure_learning.py scripts/task_queue.py tests/test_failure_learning.py tests/test_task_queue.py
git commit -m "feat: gate learning and repeated failures"
```

Expected: failure-learning and queue suites pass.

---

### Task 10: Integrate anti-fabrication and exact-name final validation

**Files:**
- Modify: `scripts/evidence_integrity.py`
- Modify: `scripts/validate_output.py`
- Modify: `tests/test_evidence_integrity.py`
- Modify: `tests/test_project_tools.py`

- [ ] **Step 1: Write failing final-integrity tests**

Add tests that mutate an otherwise valid V4 fixture and assert rejection for:

```python
cases = {
    "same_alignment_for_every_page": "SUSPICIOUS_REPEATED_ALIGNMENT",
    "full_size_true_without_artifact": "FULL_SIZE_ARTIFACT_MISSING",
    "all_reference_packs_one_unrelated_identity": "REFERENCE_CAST_COVERAGE_UNPROVEN",
    "review_before_candidate": "REVIEW_TIME_ORDER_INVALID",
    "generator_equals_reviewer": "REVIEW_NOT_INDEPENDENT",
    "cluster_pass_before_page_pass": "CLUSTER_QA_TIME_ORDER_INVALID",
    "passed_report_with_pending_registry": "FINAL_REPORT_STATE_MISMATCH",
    "renamed_output": "OUTPUT_NAME_SET_MISMATCH",
}
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_evidence_integrity tests.test_project_tools -v
```

Expected: V4 integrity mutations are not all rejected.

- [ ] **Step 3: Add shared integrity checks**

Implement validators for review artifact existence/hash, suspicious repeated page facts, timeline transition support, reference cast coverage, stable anchors, event hash chains, actor independence, candidate/review/page/cluster/final time order, exact relative output names, and registry/report agreement. Suspicious repeated alignments block unless a reviewed exception names the affected pages and source range.

- [ ] **Step 4: Wire V4 into `validate_output.validate_project`**

Require all V4 registries, `continuity_v4`, schema `4.0`, page classes, full-resolution audits, dual-review resolution, entity timelines, class-specific preflight, completed tasks, cluster QA, zero unresolved issues, exact input/output names and extensions, and report status `passed`. Validation must remain read-only and must not accept `--force` or rebuild evidence.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_evidence_integrity tests.test_project_tools -v
git add scripts/evidence_integrity.py scripts/validate_output.py tests/test_evidence_integrity.py tests/test_project_tools.py
git commit -m "feat: enforce v4 evidence integrity"
```

Expected: final validator rejects every forged fixture and accepts only the complete V4 fixture.

---

### Task 11: Add an audit-only gate and keep V3 migration diagnostic-only

**Files:**
- Create: `scripts/validate_audit.py`
- Create: `tests/test_validate_audit.py`
- Modify: `scripts/migrate_scene_pipeline.py`
- Modify: `tests/test_migrate_scene_pipeline.py`

- [ ] **Step 1: Write failing audit-only tests**

```python
def test_audit_only_accepts_complete_audits_without_candidates(self):
    result = validate_audit_project(self.root, self.evidence)
    self.assertEqual(result["status"], "audit_passed")
    self.assertEqual(result["candidate_count"], 0)
    self.assertEqual(result["promoted_output_count"], 0)


def test_audit_only_rejects_any_candidate_or_final_image(self):
    (self.root / "work" / "candidates" / "189.jpg").parent.mkdir(parents=True)
    draw_image(self.root / "work" / "candidates" / "189.jpg")
    with self.assertRaisesRegex(ValueError, "audit-only run contains candidate"):
        validate_audit_project(self.root, self.evidence)
```

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_validate_audit -v
```

Expected: audit validator does not exist.

- [ ] **Step 3: Implement read-only audit validation**

`validate_audit_project` requires confirmed alignment, semantic clusters, role-complete reference packs, entity timelines, one full-resolution primary audit per page, completed second reviews where routed, and final page classifications. It rejects candidate files, repair-task completion, and any image in final output. It emits JSON status only and writes nothing.

- [ ] **Step 4: Restrict V3 migration**

Make V3 migration output explicitly `diagnostic_only: true`. Reject any request to apply V3 evidence as V4. A V4 proposal must be rebuilt from immutable source hashes and remain unresolved until V4 audits exist; confirmation tokens cannot synthesize audits, timelines, reference coverage, or pass states.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_validate_audit tests.test_migrate_scene_pipeline -v
git add scripts/validate_audit.py scripts/migrate_scene_pipeline.py tests/test_validate_audit.py tests/test_migrate_scene_pipeline.py
git commit -m "feat: add audit only gate and safe migration"
```

Expected: audit and migration tests pass.

---

### Task 12: Update the Skill contract and remove mojibake

**Files:**
- Modify: `SKILL.md`
- Modify: `references/continuity-rules.md`
- Modify: `references/scene-cluster-pipeline.md`
- Modify: `references/qa-checklist.md`
- Modify: `references/failure-learning.md`
- Modify: `agents/openai.yaml`
- Modify: `tests/test_skill_contract.py`

- [ ] **Step 1: Write failing documentation-contract tests**

Add assertions for these exact concepts:

```python
required_phrases = {
    "SKILL.md": [
        "continuity_v4",
        "same relative path, filename, and extension",
        "continuity_first_full_page",
        "contact sheets are orientation aids only",
        "generator cannot approve its own candidate",
        "validation is read-only",
    ],
    "references/continuity-rules.md": [
        "beard",
        "moustache",
        "sideburn",
        "non-critical action variation is not a defect",
        "entity_state_timeline.json",
    ],
    "references/qa-checklist.md": [
        "full-resolution artifact",
        "blind review",
        "exact relative-path set",
        "malformed glyph",
    ],
}
```

Also scan all public text files for replacement characters and typical mojibake fragments currently visible in directory names, cluster sizes, and context-radius prose.

- [ ] **Step 2: Run and verify RED**

Run:

```powershell
python -m unittest tests.test_skill_contract -v
```

Expected: V4 phrases are missing and mojibake scan fails.

- [ ] **Step 3: Rewrite the concise coordinator contract**

Keep `SKILL.md` under 500 lines. State the immutable pipeline order, exact-name output contract, continuity-first visual policy, full-resolution audit requirement, four classes, full-page textless default, deterministic text restoration, semantic clusters, dual review, three-worker limit, two-attempt breaker, audit-only gate, independent promotion, and read-only final validation.

- [ ] **Step 4: Put detailed rules in references and refresh UI metadata**

Move identity/facial-hair/state-machine details to continuity rules, orchestration details to scene pipeline, evidence and blind-review matrices to QA, and learning promotion to failure-learning. Save every file as UTF-8. Update `agents/openai.yaml` so its display text matches V4 without describing a shortcut workflow.

- [ ] **Step 5: Run tests and commit**

Run:

```powershell
python -m unittest tests.test_skill_contract -v
git add SKILL.md references agents/openai.yaml tests/test_skill_contract.py
git commit -m "docs: publish continuity v4 contract"
```

Expected: documentation contract and encoding scan pass.

---

### Task 13: Run the complete regression suite and structural validation

**Files:**
- Create: `docs/superpowers/validation/2026-07-14-continuity-v4-acceptance.md`

This task makes no production-code change. If a command fails, return to the task that owns the failing module, add a reproducing test there, and repeat its RED-GREEN cycle before restarting Task 13.

- [ ] **Step 1: Run all unit tests**

Run:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Expected: all tests pass with no traceback, warning, or skipped V4 gate.

- [ ] **Step 2: Run Skill structural validation**

Run:

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .
```

Expected: Skill validation succeeds.

- [ ] **Step 3: Run metadata-only failed-run and audit-only acceptance**

Run:

```powershell
python -m unittest tests.test_evidence_integrity tests.test_validate_audit -v
```

Expected: failed-run fixture is rejected for exact expected codes; complete synthetic audit-only fixture passes and contains zero candidates or final images.

- [ ] **Step 4: Record durable acceptance evidence**

Write the exact commands, timestamps, test counts, exit codes, Git commit, failed-run rejection codes, audit-only zero-candidate result, and statement that no project image was processed to `docs/superpowers/validation/2026-07-14-continuity-v4-acceptance.md`.

- [ ] **Step 5: Commit validation evidence**

Run:

```powershell
git add docs/superpowers/validation/2026-07-14-continuity-v4-acceptance.md
git commit -m "test: validate continuity v4 skill"
```

Expected: clean commit; no project image or copyrighted novel content appears in the repository.

---

### Task 14: Synchronize the installed Skill and publish the validated branch

**Files:**
- Update installed copy: `C:\Users\骆阳\.codex\skills\repair-comic-continuity\`
- No project images are read, modified, or generated.

- [ ] **Step 1: Verify the repository is clean and inspect the exact outgoing commits**

Run:

```powershell
git status --short
git log --oneline origin/agent/publish-skill-v3..HEAD
git diff --check origin/agent/publish-skill-v3..HEAD
```

Expected: clean status, only V4 design/plan/code/test/validation commits, and no whitespace errors.

- [ ] **Step 2: Synchronize only Skill-owned files**

Run:

```powershell
$repo = (Resolve-Path '.').Path
$installed = Join-Path $env:USERPROFILE '.codex\skills\repair-comic-continuity'
Copy-Item -LiteralPath (Join-Path $repo 'SKILL.md') -Destination (Join-Path $installed 'SKILL.md') -Force
foreach ($directory in @('agents', 'references', 'scripts')) {
    Get-ChildItem -LiteralPath (Join-Path $repo $directory) -Recurse -File | ForEach-Object {
        $relative = $_.FullName.Substring($repo.Length + 1)
        $destination = Join-Path $installed $relative
        New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}
```

Expected: only `SKILL.md`, `agents/`, `references/`, and `scripts/` payloads are copied. `.git`, `tests`, `docs`, README files, fixtures, and project artifacts are untouched.

- [ ] **Step 3: Prove installed and repository Skill payloads match**

Run:

```powershell
$repo = (Resolve-Path '.').Path
$installed = Join-Path $env:USERPROFILE '.codex\skills\repair-comic-continuity'
$payload = @('SKILL.md') + @(foreach ($directory in @('agents', 'references', 'scripts')) {
    Get-ChildItem -LiteralPath (Join-Path $repo $directory) -Recurse -File |
        ForEach-Object { $_.FullName.Substring($repo.Length + 1) }
})
$differences = foreach ($relative in $payload | Sort-Object -Unique) {
    $left = (Get-FileHash -LiteralPath (Join-Path $repo $relative) -Algorithm SHA256).Hash
    $right = (Get-FileHash -LiteralPath (Join-Path $installed $relative) -Algorithm SHA256).Hash
    if ($left -ne $right) { $relative }
}
if ($differences) { throw "installed payload differs: $($differences -join ', ')" }
```

Expected: the script returns normally with zero differing payload files.

- [ ] **Step 4: Re-run structural validation on the installed copy**

Run:

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" "$env:USERPROFILE\.codex\skills\repair-comic-continuity"
```

Expected: installed Skill validation succeeds.

- [ ] **Step 5: Push the branch and report the commit**

Run:

```powershell
git push -u origin agent/continuity-v4-hard-gates
```

Expected: GitHub accepts the branch. Report the branch URL, final commit, test count, acceptance report, and confirmation that image processing was not started.
