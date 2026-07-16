# Human Issue Annotations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add hash-bound human issue annotations that direct the current repair and enter the existing failure-learning system only through evidence-gated promotion.

**Architecture:** A focused validator owns the exact annotation schema and normalized geometry. `validate_audit.py` binds annotations to selected visual pages and page-audit evidence. A separate transactional learning bridge maps validated annotations to observed failure records without weakening outcome or promotion gates.

**Tech Stack:** Python 3.11 standard library, Pillow test fixtures, `unittest`, Markdown Skill contracts, RSA PKCS#1 v1.5 SHA-256 release certificate.

---

### Task 1: Lock the annotation schema with failing tests

**Files:**
- Create: `tests/test_human_issue_annotations.py`
- Create: `tests/test_human_issue_learning.py`
- Modify: `tests/test_validate_audit.py`
- Modify: `tests/test_skill_contract.py`

- [ ] Write validator tests covering a valid annotation, multiple regions, empty annotations, unsafe or unknown pages, unselected pages, stale hashes, duplicate IDs, invalid timestamps, unknown failure codes, and malformed normalized boxes.
- [ ] Write learning tests proving one failure per target, exact before-page binding, diagnosis/action geometry, idempotence, and transactional rollback.
- [ ] Write audit tests proving annotated pages require a hash-bound annotation audit record and unannotated backward-compatible runs still pass.
- [ ] Write public-contract tests for `human_issue_annotations.json`, explicit region/state/correction capture, evidence-gated learning, and prohibition on immediate permanent learning.
- [ ] Run `python -m unittest tests.test_human_issue_annotations tests.test_human_issue_learning tests.test_validate_audit tests.test_skill_contract -v` and confirm failure because the new modules and behavior are absent.

### Task 2: Implement deterministic validation and learning ingestion

**Files:**
- Create: `scripts/validate_human_issue_annotations.py`
- Create: `scripts/human_issue_learning.py`
- Modify: `scripts/validate_audit.py`

- [ ] Validate exact manifest, annotation, region, page, timestamp, policy, defect-code, target, hash, selection-membership, and natural-order contracts.
- [ ] Return normalized annotations, unique annotated pages, and counts without mutating source evidence.
- [ ] Require one annotation artifact record on each annotated page and verify its manifest-relative path and SHA-256.
- [ ] Map validated annotation-target pairs transactionally through `record_failure`; commit the copied store only after every record validates.
- [ ] Run the targeted tests until all pass.

### Task 3: Publish the controlled-learning contract

**Files:**
- Modify: `SKILL.md`
- Modify: `references/scene-cluster-pipeline.md`
- Modify: `references/failure-learning.md`

- [ ] Add a concise human-annotation rule to `SKILL.md` without duplicating the full schema.
- [ ] Document the exact manifest and page-audit binding in the scene-cluster reference.
- [ ] Document annotation ingestion, outcome evidence, four promotion gates, scope progression, revocation, and signed-release permanence in the failure-learning reference.
- [ ] Run Skill contract tests and UTF-8 `quick_validate.py`.

### Task 4: Version and sign V5.4

**Files:**
- Modify: `scripts/pipeline_version.py`
- Modify: `scripts/validate_release_certificate.py`
- Modify: `tests/test_release_version_contract.py`
- Modify: `assets/release_evidence/runtime_manifest.json`
- Modify: `assets/release_certificate.json`
- Modify: `assets/release_public_key.json`

- [ ] Change the release ID to `continuity-v5.4-signed-20260716` and require both new control scripts in the signed manifest.
- [ ] Recompute canonical hashes for every runtime file, generate an in-memory RSA keypair, sign the canonical certificate payload, and persist only the public key and signed artifacts.
- [ ] Run release-version, signature, line-ending, and tamper tests.

### Task 5: Verify, deploy, package, and publish

**Files:**
- Sync plan-owned files to `C:/Users/骆阳/.codex/skills/repair-comic-continuity`
- Create: `D:/自动修改/7878/repair-comic-continuity-v5.4-signed-20260716.zip`

- [ ] Run the full source test suite, UTF-8 Skill validation, and signed certificate validation.
- [ ] Verify installed files have no overlapping user edits, copy only plan-owned files, then repeat the full installed test suite and validators.
- [ ] Build a ZIP with top-level `repair-comic-continuity/`, excluding only caches; verify safe paths, exact content hashes, extracted Skill validation, and extracted certificate validation.
- [ ] Review Git diff, stage only plan-owned files, commit, push the existing branch, and verify local/upstream commit equality while preserving unrelated worktree modifications.
