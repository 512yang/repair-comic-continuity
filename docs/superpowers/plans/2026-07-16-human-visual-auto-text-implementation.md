# Human-selected Visual Scope Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a fail-closed `human_visual_auto_text` mode in which a user-confirmed page list limits visual redraw work while full text audit and style-preserving text reset still run on every input page.

**Architecture:** Introduce one focused manifest validator, then branch `validate_audit.py` only at the visual-coverage checks. Preserve all alignment, text, output, and automatic-mode gates. Document the public contract, version the control plane, re-sign the release evidence, and sync the verified files to the installed Skill.

**Tech Stack:** Python 3.11 standard library, Pillow test fixtures, `unittest`, Markdown Skill contracts, RSA PKCS#1 v1.5 SHA-256 release certificate.

---

### Task 1: Lock the manifest contract with failing tests

**Files:**
- Create: `tests/test_human_visual_selection.py`
- Modify: `tests/test_validate_audit.py`
- Modify: `tests/test_skill_contract.py`

**Steps:**
1. Add validator tests for valid selection, empty selection, unknown paths, duplicates, natural-order violations, policy drift, and SHA-256 drift.
2. Add audit tests proving selected pages retain full-resolution review, unselected pages use the scope record, all pages retain source-text audits, and unselected pages cannot be redrawn.
3. Add public Skill contract assertions for mode name, manifest name, all-page text behavior, style preservation, and exact output.
4. Run the targeted tests and confirm they fail because the new validator and contract do not exist yet.

### Task 2: Implement the fail-closed mode

**Files:**
- Create: `scripts/validate_human_visual_selection.py`
- Modify: `scripts/validate_audit.py`

**Steps:**
1. Validate the exact manifest schema and fixed policy values.
2. Resolve every selected path inside the project, verify exact input membership and source SHA-256, enforce uniqueness and natural order, and return normalized selected paths.
3. Keep automatic mode behavior unchanged when the manifest is absent.
4. In human mode, require full-resolution visual audits and appearance coverage only for selected pages; require deterministic scope evidence and prohibit redraw for unselected pages.
5. Keep source-text evidence mandatory for every input page and report mode plus selected-page count.
6. Run the targeted tests until green.

### Task 3: Publish the mode contract

**Files:**
- Modify: `SKILL.md`
- Modify: `references/scene-cluster-pipeline.md`

**Steps:**
1. Add a concise selectable-mode section to `SKILL.md`.
2. Add detailed manifest, visual-scope, text-scope, and fail-closed rules to the pipeline reference.
3. Run Skill contract tests and quick validation.

### Task 4: Version and re-sign the release

**Files:**
- Modify: `scripts/pipeline_version.py`
- Modify: `tests/test_release_version_contract.py`
- Modify: `assets/release_evidence/runtime_manifest.json`
- Modify: `assets/release_certificate.json`
- Modify: `assets/release_public_key.json`

**Steps:**
1. Advance the signed release identifier to `continuity-v5.3-signed-20260716`.
2. Include the new validator and all changed runtime control files in the manifest with current hashes.
3. Generate a fresh RSA keypair in memory, sign the canonical certificate payload, and write only the public key and signature artifacts.
4. Run the release-certificate validator and release tests.

### Task 5: Full verification, installation sync, and publication

**Files:**
- Sync only files changed by this plan to `C:/Users/骆阳/.codex/skills/repair-comic-continuity`

**Steps:**
1. Run `python -m unittest discover -s tests -v` in the source worktree.
2. Run `quick_validate.py` and the signed certificate validator.
3. Copy the verified changed files to the installed Skill without touching unrelated local modifications.
4. Run the installed Skill's full test suite, quick validation, and certificate validation.
5. Review Git diff, stage only plan-owned files, commit, push the existing feature branch, and verify the remote commit hash.
