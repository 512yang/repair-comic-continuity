---
name: repair-comic-continuity
description: Use when Codex must audit or repair Chinese comic pages against a novel, character references, adjacent pages, or user-confirmed issue regions, especially for cross-page identity, costume, prop, scene, malformed-glyph, exact-filename, or style-continuity problems.
---

# Repair Comic Continuity

Use the `continuity_v5_unified` pipeline. It retains the hash-bound `continuity_v4` evidence contracts while embedding the text-repair engine, reviewed fonts, LaMa routing, deterministic typesetting, and text QA in this one Skill. Treat every generated or typeset image as an untrusted candidate until independent review promotes it.

Run production work only inside an isolated run root created by `scripts/prepare_run_workspace.py`. This byte-copies the novel, references, and input pages into a clean root while excluding old outputs, candidates, and evidence. Never use a previously processed project directory as an audit root.

## Required references

- Read [continuity-rules.md](references/continuity-rules.md) before building identity, facial-hair, costume, prop, extra, scene, or speaker state.
- Read [scene-cluster-pipeline.md](references/scene-cluster-pipeline.md) before clustering, scheduling, canary release, audit-only work, or promotion.
- Read [failure-learning.md](references/failure-learning.md) before retrying, pausing a lane, promoting a learned rule, or revoking one.
- Read [qa-checklist.md](references/qa-checklist.md) before candidate review and final validation.
- Read [text-engine.md](references/text-engine.md) before detecting, removing, rebuilding, rendering, or reviewing ordinary text.

## Immutable pipeline order

1. Inventory naturally sorted, decodable input images and hash the novel, references, and source pages.
2. Preserve the same relative path, filename, and extension for every output. Input count and output count must both equal `N`; no page may be added, omitted, flattened, or renamed.
3. Confirm per-page novel alignment with source offsets and evidence.
4. Build semantic scene clusters, reference packs, and `entity_state_timeline.json`.
5. Build and validate `character_appearance_matrix.json`, then run full-resolution dual audits. In automatic mode the matrix must bind every cluster page; in `human_visual_auto_text` mode it must bind every user-selected visual page. In either mode, bind every named character in scope to an identity reference and compare skin tone, hair, facial hair, clothing, face shape, and body build; contact sheets are orientation aids only and are never pass evidence.
6. Build a complete text-region inventory, a full-resolution source glyph board, and a hash-bound original `style_lock` for every ordinary-text block. Record a visual decision for each block and an explicit non-OCR-only shape decision for every occurrence of 强 and 遇. Validate each page with `scripts/validate_source_text_audit.py`; its independent machine/visual block inventories, bounding boxes, and transcriptions must match exactly, every source crop must pixel-match the current page, every adjacent Chinese repeat must have an explicit intentional/defect decision, and every block must bind an exact hash-bound novel excerpt plus offsets and a semantic decision. An empty inventory requires a third independent, full-resolution `textless_review`. If any ordinary-text block lacks source-crop or novel coverage, the page cannot be classified. Missing or low-confidence evidence is `evidence_blocked`.
7. Assign exactly one page class: `unchanged`, `text_only`, `full_page_redraw`, or `evidence_blocked`.
8. Release only approved tasks. Use a canary before expensive work in a cluster.
9. Run class-specific preflight, independent page review, cluster review, and final read-only validation.

At startup, run `python scripts/validate_release_certificate.py --root <skill-root> --json`. A valid signed certificate proves that this exact V5 detection control plane passed its packaged user-reviewed generic benchmark, so do not ask the user to recreate the packaged generic golden dataset or fill a generic 12-page review table. Then run an automatic project canary on one complete scene cluster from the new project. The certificate never bypasses project-specific audit gates, source evidence, independent review, or final validation.

Ask the user for project-specific adjudication only when the certificate invalid result persists after one clean recheck, auditor disagreement remains unresolved, a page is `evidence_blocked`, or a genuinely new failure family falls outside certified coverage. Without a valid certificate, do not generate or promote images. For a new Skill version without a signed certificate, compare a blind audit with a user-reviewed golden dataset using `scripts/validate_detection_benchmark.py`. Release requires zero missed confirmed defects, zero false-positive defects, exact page coverage, and correct-page protection. A contact sheet, OCR-only result, or self-authored answer key cannot satisfy this gate.

Do not skip ahead. Audit uncertainty remains `evidence_blocked`; it is never silently treated as a correct page or a redraw instruction.

## Human-selected visual mode

When the user supplies the visual-problem page numbers or names, use `human_visual_auto_text` inside this Skill; do not create a second Skill. Resolve the list to exact input-relative names and sealed source hashes in `evidence/human_visual_selection.json`, then validate it with `scripts/validate_human_visual_selection.py`. The manual list controls visual redraw scope only.

- Selected pages still require full-resolution reference, adjacency, scene-state, and novel evidence before redraw. Their appearance matrix must exactly cover the selected list.
- Unselected pages use hash-bound `human_visual_scope` evidence instead of model-based visual detection; unselected pages cannot enter `full_page_redraw`.
- In this mode, every input page still receives the complete source-text audit. Every ordinary text-bearing page still uses `page_reset_preserve_style`, preserving the original font, color, size, position, orientation, line geometry, and balloon geometry.
- Empty visual selection is valid, but it never disables all-page text work. Unknown, duplicate, reordered, or source-hash-drifted selections are `evidence_blocked`.
- Preserve the exact input/output bijection, including identical relative path, filename, extension, and page count.

If the user also identifies what is wrong and where to change it, write every user-confirmed region, observed state, required correction, affected target, and instruction to `evidence/human_issue_annotations.json`. Validate it with `scripts/validate_human_issue_annotations.py`, bind it to the selected page's audit by path and SHA-256, and ingest it transactionally with `scripts/human_issue_learning.py`. An annotation never becomes an effective learned rule by itself; it starts only as observed failure evidence.

## V5.5 closed-loop enforcement

Use `scripts/closed_loop_controller.py` for every production run that contains human annotations or learned rules. Build and save a run preview before task release; it must list every input page, page class, text policy, selected visual page, annotation ID, effective rule ID, blocker, expected output count, and estimated generation-call count.

Advance the controller only through `workspace_prepared`, `inventory_sealed`, `audit_passed`, `annotations_ingested`, `tasks_released`, `candidate_reviewed`, `learning_recorded`, `outputs_promoted`, and `release_passed`. When annotations exist, a missing `annotations_ingested` receipt blocks task release. Do not replace a missing receipt with prose, a file-existence claim, or an agent assertion.

For every annotated redraw, build a hash-bound `human_issue_binding` and include it in the complete redraw request. The generator must repair every bound required state and preserve all unaffected content. After generation, write an independent per-annotation review to `evidence/annotation_reviews/<input-relative-name>.json`; every annotation must pass both `required_state_met` and `unaffected_content_preserved`. `scripts/release_gate.py` rejects an annotated page when the prompt binding, candidate hash, preflight ID, reviewer identity, or annotation review is missing or stale.

Load promoted rules through `load_effective_rules_for_page`; do not manually copy selected rules into a prompt. Resolve specificity as `page > cluster > project > skill_candidate`. Block conflicting effective rules at the same scope. Persist annotation intake and after-candidate outcomes atomically before advancing the matching controller receipt.

Promote learned corrections only after positive regression, clean control, variation, and independent review pass with hash-bound artifacts. Follow `page -> cluster -> project -> skill_candidate` without skipping scope. A candidate becomes permanent across installations only through a reviewed Skill change and fresh signed release.

See [scene-cluster-pipeline.md](references/scene-cluster-pipeline.md) for the exact manifest and audit-record contracts.

## Continuity-first image policy

The visual policy is `continuity_first_full_page`:

- `unchanged`: copy the source bytes to the exact output path. Do not regenerate or re-typeset a correct page.
- `text_only`: keep all artwork fixed and repair ordinary text at the original geometry.
- `full_page_redraw`: redraw the complete page only for a confirmed page-wide visual defect. Preserve panel structure, composition, cast identity, costumes, props, scene facts, and the established comic rendering language.
- `evidence_blocked`: produce no candidate and no final output until authoritative evidence resolves the issue.

Non-critical pose, grip, camera, or expression variation is not a defect when it preserves story meaning and continuity. A character merely holding an object differently from the prose is not sufficient reason to redraw.

For a redraw, capture all ordinary text first, then generate a full-page textless candidate. Do not ask the image model to typeset Chinese. Restore text only after the image candidate passes. The default policy is `page_reset_preserve_style`: remove all ordinary text and rebuild every ordinary-text block on every text-bearing page because malformed glyphs can survive OCR. Preserve only reviewed art text and sound effects.

## Text geometry contract

Bind every block to page, panel, balloon, speaker, original rectangle or polygon, source text, replacement text, exact novel offsets, and a hash-bound `style_lock`. Preserve the original font or independently reviewed visual equivalent, font asset hash, confidence, fill and stroke colors, font size, stroke width, letter and line spacing, writing mode, alignment, rotation, anchor, line boxes, reading order, balloon style, placement, and density. Never add a new dialogue balloon unless source evidence explicitly requires one.

Use only the embedded engine in `scripts/text_engine_pipeline.py`, `scripts/text_style_contract.py`, `scripts/comic_repair/`, and `assets/text_fonts/`. Do not require or import `D:\漫画文字修复`, another text-repair Skill, or an external font project. LaMa weights may live in the Skill runtime cache or `REPAIR_COMIC_LAMA_ROOT`; missing runtime evidence blocks complex-background cleaning instead of triggering a weak fallback.

Do not silently shrink, reflow, recolor, recenter, rotate, or substitute a font. Keep the original line count and line boxes. If replacement text cannot fit under the locked style and geometry, mark `text_overflow` and route to reviewed layout adjustment; never drop characters or save an overflowed page.

Use deterministic rendering for Chinese glyphs. A machine OCR match does not prove that a glyph is visually correct; review malformed strokes such as `强` and `遇` at full resolution. If a page contains too much text, shorten it only with source-faithful wording and preserve the narrative meaning.

## Semantic clusters and execution

Cluster contiguous story beats by chapter, location, story time, cast state, costume state, props, and scene axis. A normal cluster owns 8–20 pages and may inspect up to ±2 context pages without duplicating ownership. Smaller projects may use one reviewed boundary exception.

Use one coordinator and at most 3 workers. Each worker holds one durable lease and writes only to its isolated candidate directory. Use a single writer for evidence and final promotion. A generator cannot approve its own candidate. The coordinator may run independent audit and review work in parallel, but never uses parallelism to bypass gates.

Only pages with confirmed visual defects enter image generation. Correct pages still receive the text checks required by the active mode but do not consume a generation call.

Run `python scripts/validate_appearance_matrix.py evidence/character_appearance_matrix.json` before classifying any page. A missing character page, missing identity reference, non-full-resolution observation, `not_visible` trait on a visible character, or unexplained trait drift blocks confirmation. A source matrix may use `defects_confirmed` only when two independent full-resolution reviewers agree, every drift trait has a hash-bound `confirmed_defect` review, and the affected page is eligible for `full_page_redraw` classification. This state authorizes a repair task, not release: release still requires a `confirmed` appearance matrix with every repaired trait matching. Do not excuse a same-scene skin-tone category change as lighting without explicit full-resolution evidence. Reject generated candidates that reintroduce a drift already recorded in the matrix.

Run `python scripts/validate_source_text_audit.py evidence/source_text_audit/<page>.json --root <run-root>` before classifying each page. The release manifest must bind that confirmed audit to the exact sealed source page. A worker's prose claim that it inspected every block is not evidence and cannot satisfy this gate.

## Candidate and failure loop

Each candidate must bind the source hash, candidate hash, task, structured request, reference pack, page class, generator, timestamps, and preflight result. Review it against the original page, stable comic anchors, identity references, adjacent pages, novel facts, and entity timelines.

After the second failure in the same `(cluster_id, task_type, failure_family)`, pause that lane. Leave queued work unclaimable until a reviewed diagnosis reopens it. Do not spend repeated calls on the same unresolved cause.

A learned rule becomes effective only when positive regression, clean-control, varied-case, and independent-review artifacts all pass and their hashes are stored. A rejected candidate is never a style source.

## Audit-only gate

Use `scripts/validate_audit.py` when the requested phase is inspection only. It requires confirmed alignment, semantic clusters, role-complete reference packs, valid entity timelines, a validated appearance matrix, every per-page source text audit, full-resolution page audits, required second reviews, and final page classifications. It rejects candidate images, completed repair tasks, and any final output image.

V3 migration output is diagnostic only. It cannot synthesize V4 audits, timelines, reference coverage, pass states, or final images, even with a confirmation token. Rebuild V4 evidence from immutable source hashes.

Do not run audit-only validation against a directory that already contains text-engine masks, dry-run PNGs, candidates, or prior outputs. Create a new isolated run root, rebuild evidence from the copied immutable inputs, and keep all generation disabled until `scripts/validate_audit.py` succeeds.

## Completion gates

Before promotion, require:

- every input has exactly one output at the identical relative path and extension;
- every page has a confirmed alignment, semantic cluster, reference binding, entity-state decision, class-specific preflight, completed task, and resolved dual audit;
- every automatic-mode scene cluster has a confirmed `character_appearance_matrix.json` with exact cluster coverage; every human-mode matrix has exact selected-page coverage; neither may retain unresolved skin, hair, facial-hair, clothing, face-shape, or body-build drift;
- every page has a confirmed `source_text_audit` with exact machine/visual block coverage, pixel-bound source crops, exact novel offsets and semantic decisions, reviewed adjacent repeats, and non-OCR shape checks for every 强 and 遇 occurrence;
- all full-resolution review artifacts exist and match their recorded hashes;
- generator, page reviewer, and cluster reviewer satisfy independence and chronological ordering;
- all queues, registries, page reviews, cluster reviews, and regression summaries are passed with zero unresolved issues;
- `FINAL_QA_REPORT.md` agrees with the registries;
- `scripts/validate_output.py` succeeds.

Build verified release bindings only after class-specific preflight and independent review. Promote with `scripts/promote_outputs.py`; unchanged pages are copied only from sealed input, while repaired pages are copied only from their hash-bound candidates. The promoted directory is provisional until the release gate passes.

Run `scripts/release_gate.py` after promotion. It must recompute sealed input, output, candidate, appearance-matrix, source-text-audit, preflight, and independent-review hashes and then call the legacy final validator; never trust a self-reported passed or accepted string. A missing artifact, stale hash, forged registry, modified `unchanged` page, filename mismatch, or review without independence blocks release.

Final validation is read-only. Validation is read-only: it must not rebuild evidence, apply `--force`, generate images, repair files, rename outputs, or convert a pending state into a pass.

Do not report completion from file existence or generation success. If any gate fails, keep the affected page out of final promotion and report the exact blocker.
