---
name: repair-comic-continuity
description: Audit and repair Chinese comic pages against a novel, character references, and adjacent pages while preserving the established art style, correct content, exact filenames, and cross-page continuity.
---

# Repair Comic Continuity

Use the `continuity_v4` pipeline. The coordinator audits first, changes only proven defects, and treats every generated or externally typeset image as an untrusted candidate until independent review promotes it.

## Required references

- Read [continuity-rules.md](references/continuity-rules.md) before building identity, facial-hair, costume, prop, extra, scene, or speaker state.
- Read [scene-cluster-pipeline.md](references/scene-cluster-pipeline.md) before clustering, scheduling, canary release, audit-only work, or promotion.
- Read [failure-learning.md](references/failure-learning.md) before retrying, pausing a lane, promoting a learned rule, or revoking one.
- Read [qa-checklist.md](references/qa-checklist.md) before candidate review and final validation.

## Immutable pipeline order

1. Inventory naturally sorted, decodable input images and hash the novel, references, and source pages.
2. Preserve the same relative path, filename, and extension for every output. Input count and output count must both equal `N`; no page may be added, omitted, flattened, or renamed.
3. Confirm per-page novel alignment with source offsets and evidence.
4. Build semantic scene clusters, reference packs, and `entity_state_timeline.json`.
5. Run full-resolution dual audits before generation; contact sheets are orientation aids only and are never pass evidence.
6. Assign exactly one page class: `unchanged`, `text_only`, `full_page_redraw`, or `evidence_blocked`.
7. Release only approved tasks. Use a canary before expensive work in a cluster.
8. Run class-specific preflight, independent page review, cluster review, and final read-only validation.

Do not skip ahead. Audit uncertainty remains `evidence_blocked`; it is never silently treated as a correct page or a redraw instruction.

## Continuity-first image policy

The visual policy is `continuity_first_full_page`:

- `unchanged`: copy the source bytes to the exact output path. Do not regenerate or re-typeset a correct page.
- `text_only`: keep all artwork fixed and repair ordinary text at the original geometry.
- `full_page_redraw`: redraw the complete page only for a confirmed page-wide visual defect. Preserve panel structure, composition, cast identity, costumes, props, scene facts, and the established comic rendering language.
- `evidence_blocked`: produce no candidate and no final output until authoritative evidence resolves the issue.

Non-critical pose, grip, camera, or expression variation is not a defect when it preserves story meaning and continuity. A character merely holding an object differently from the prose is not sufficient reason to redraw.

For a redraw, capture all ordinary text first, then generate a full-page textless candidate. Do not ask the image model to typeset Chinese. Restore text only after the image candidate passes. If text recognition is unreliable, remove all ordinary text and rebuild every declared block from novel-backed text geometry. Preserve only reviewed art text and sound effects.

## Text geometry contract

Bind every block to page, panel, balloon, speaker, original rectangle or polygon, source text, replacement text, and exact novel offsets. Preserve the original reading order, balloon style, placement, and density. Never add a new dialogue balloon unless source evidence explicitly requires one.

Use deterministic rendering for Chinese glyphs. A machine OCR match does not prove that a glyph is visually correct; review malformed strokes such as `强` and `遇` at full resolution. If a page contains too much text, shorten it only with source-faithful wording and preserve the narrative meaning.

## Semantic clusters and execution

Cluster contiguous story beats by chapter, location, story time, cast state, costume state, props, and scene axis. A normal cluster owns 8–20 pages and may inspect up to ±2 context pages without duplicating ownership. Smaller projects may use one reviewed boundary exception.

Use one coordinator and at most 3 workers. Each worker holds one durable lease and writes only to its isolated candidate directory. Use a single writer for evidence and final promotion. A generator cannot approve its own candidate. The coordinator may run independent audit and review work in parallel, but never uses parallelism to bypass gates.

Only pages with confirmed visual defects enter image generation. Correct pages still receive text and continuity checks but do not consume a generation call.

## Candidate and failure loop

Each candidate must bind the source hash, candidate hash, task, structured request, reference pack, page class, generator, timestamps, and preflight result. Review it against the original page, stable comic anchors, identity references, adjacent pages, novel facts, and entity timelines.

After the second failure in the same `(cluster_id, task_type, failure_family)`, pause that lane. Leave queued work unclaimable until a reviewed diagnosis reopens it. Do not spend repeated calls on the same unresolved cause.

A learned rule becomes effective only when positive regression, clean-control, varied-case, and independent-review artifacts all pass and their hashes are stored. A rejected candidate is never a style source.

## Audit-only gate

Use `scripts/validate_audit.py` when the requested phase is inspection only. It requires confirmed alignment, semantic clusters, role-complete reference packs, valid entity timelines, full-resolution page audits, required second reviews, and final page classifications. It rejects candidate images, completed repair tasks, and any final output image.

V3 migration output is diagnostic only. It cannot synthesize V4 audits, timelines, reference coverage, pass states, or final images, even with a confirmation token. Rebuild V4 evidence from immutable source hashes.

## Completion gates

Before promotion, require:

- every input has exactly one output at the identical relative path and extension;
- every page has a confirmed alignment, semantic cluster, reference binding, entity-state decision, class-specific preflight, completed task, and resolved dual audit;
- all full-resolution review artifacts exist and match their recorded hashes;
- generator, page reviewer, and cluster reviewer satisfy independence and chronological ordering;
- all queues, registries, page reviews, cluster reviews, and regression summaries are passed with zero unresolved issues;
- `FINAL_QA_REPORT.md` agrees with the registries;
- `scripts/validate_output.py` succeeds.

Final validation is read-only. Validation is read-only: it must not rebuild evidence, apply `--force`, generate images, repair files, rename outputs, or convert a pending state into a pass.

Do not report completion from file existence or generation success. If any gate fails, keep the affected page out of final promotion and report the exact blocker.
