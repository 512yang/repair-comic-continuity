# QA Checklist

## Scene-Cluster v1 Gate

- [ ] Confirm a normal cluster owns 8–20 pages, has exact available ±2 context, and has an approved `canary` before expensive visual work. Permit `boundary_exception=true` only for a single whole-project cluster with total `N < 8` and reason `project_total_below_min`; every other `undersized: true` cluster must remain blocked with `evidence_blocked`/`evidence_resolution` members and fail final QA.
- [ ] Confirm `page_class=full_page_redraw` maps to `action=full_page_regeneration`; reject either token in the other's field.
- [ ] Confirm every reference role is explicit and no `reference contamination` exists: no rejected candidate, identity sheet as style, or unpromoted candidate as an authority.
- [ ] Confirm every page has matching `cluster_id`, `reference_pack_id`, `task_id`, and `failure_rule_ids` in manifest and repair log.
- [ ] Confirm each task has one valid `lease` history, an idempotency key, and a completed candidate path/hash bound to the correct page and cluster.
- [ ] Confirm all five v3 registries exist, declare the expected `registry_hash`, and bind canonical content without tampering: `scene_clusters.json`, `style_reference_packs.json`, `task_queue.json`, `failure_learning.json`, and `scene_cluster_qa.json`.
- [ ] Confirm `completed_by == generated_by`, `reviewed_by == page_qa.reviewer`, and every cluster-QA reviewer differs from all generators in that cluster.
- [ ] Confirm there are `zero unresolved tasks`: none remain queued, leased, failed, expired, blocked without evidence, or in an unknown state.
- [ ] Confirm deterministic preflight checks file type, dimensions, hash, prompt/reference trace, crop/blank/text leak, and collision before visual judgment.
- [ ] Confirm generated pages satisfy `generated_by != reviewed_by`; review is full-size and independent, not a renamed generator pass.
- [ ] Confirm every failure rule resolves through before/after/outcome evidence and no revoked rule appears in `failure_rule_ids`.
- [ ] Confirm the `speaker graph` binds every ordinary text block to page/panel/order/speaker/novel offsets with no ambiguity.
- [ ] Confirm every balloon obeys the 25-character rule or records an override, and total visible text does not exceed `page_density_budget`.
- [ ] Confirm every input maps to one output and every output maps back to one input; final output contains `exactly N` ordered JPG files and no extra entry.
- [ ] Confirm only the coordinator performs single-writer promotion after page QA and cluster-boundary QA pass.

Read this before the first page review and repeat it before accepting every batch. A page passes only when every applicable item is verified at full resolution against source evidence.

## Page-Level Checklist

### Source and status

- [ ] Match the page to the correct natural-order input and located novel/script passage.
- [ ] Confirm the current status follows `inventoried → located → planned → edited → page_qa_passed` without gaps.
- [ ] Confirm `unchanged_copy` pages were copied rather than regenerated.
- [ ] Confirm source material remains unchanged and the candidate is in a temporary directory.
- [ ] Record candidate path/SHA-256 and original-text snapshot path/SHA-256 in `repair_log.json`.
- [ ] Record full-size visual review with `reviewer` and a timezone-aware ISO 8601 timestamp, not a thumbnail-only or summary review.
- [ ] Record exactly one `page_qa` on the page with project-wide unique `pass_id`, `reviewer`, timezone-aware ISO 8601 timestamp, result, and complete per-check outcomes; reject a bare `passed` value.
- [ ] Confirm the page has exactly one `batch_id` and no embedded `batch_qa`.
- [ ] Confirm every `continuity_lock_ids` entry resolves to a `confirmed: true` lock whose `applies_to_pages` includes the page.
- [ ] Confirm the page's `continuity_lock_ids` sets in `comic_run_manifest.json` and `repair_log.json` are identical.
- [ ] Confirm `repair_scope`, `had_ordinary_text`, `text_recognition`, and `text_reset_reason` match the text-reset matrix.
- [ ] Confirm `text_reset_reason` is exactly one of `unchanged_page`, `visual_repair_requires_page_reset`, `no_ordinary_text`, `reliable_block_match`, or `unreliable_text_page_reset`.
- [ ] If `used_external_text_resource` is false, confirm `external_candidate` is null. If true, verify the exact six-field import object, direct-child run name pattern, source under `run_dir\final`, three-way hash equality, `supervised_import`, importer, and zoned import time.

### Style preservation contract

- [ ] For `unchanged_copy` and `text_only`, confirm `visual_issue_extent` and `visual_edit_mode` are `not_applicable`, style/reference lists are empty, mask/comparison fields are null, changed area is zero, and `style_review.status` is `not_applicable`.
- [ ] For a visual repair, confirm `visual_issue_extent`, `visual_edit_mode`, style references, identity references, reference role, mask path/hash, changed-area ratio, full-page reason, and `style_review` are all present and internally consistent.
- [ ] Confirm the visual-defect mask is separate from the ordinary-text mask, matches the source dimensions, is nonblank, and measures the recorded `changed_region_ratio`.
- [ ] Confirm the target original page and stable adjacent comic pages are the primary style sources.
- [ ] If a character sheet is used, confirm `character_reference_role: identity_only`; reject `primary_style` or any equivalent use.
- [ ] For a localized repair, confirm only the masked region changed and `full_page_regeneration_reason` is null.
- [ ] For `full_page_regeneration`, confirm a genuinely page-wide issue, a full mask, ratio `1.0`, a non-empty reason, full-size comparison evidence, and `full_page_exception_approved: true`.
- [ ] Reject convenience, speed, or character “unification” as justification for full-page regeneration.

### Character, crowd, and anatomy

- [ ] Match every named character's identity, skin tone, face shape, hairstyle/color, headwear, and defining marks to the highest-priority evidence.
- [ ] Match clothing layers, cut, color, footwear, accessories, and scene-specific damage/wetness.
- [ ] Keep unnamed bystanders plausible and continuous without inventing identities or duplicating a protagonist face.
- [ ] Check limb count, hands, fingers, face, gaze, expression, pose, and hand-object contact.
- [ ] Verify no identity swaps, melted features, extra limbs, missing hands, or unexplained appearance changes remain.

### Props, scene, and causality

- [ ] Verify each key prop's owner, carrier hand, count, position, orientation, state, and reason for presence.
- [ ] Verify location axis, entrances, windows, furniture, fixtures, horizon, and camera relation against adjacent pages.
- [ ] Verify time, weather, lighting, wetness, damage, and occupancy follow the story chronology.
- [ ] Confirm the repair adds no unsupported person, prop, action, emotion, event, or plot reveal.

### Ordinary text and sound effects

- [ ] Before modification, preserve each block's content, position, reading order, speaker/role, style, and confidence.
- [ ] When a confirmed typo existed, verify the whole affected block was rewritten and no old glyph residue remains.
- [ ] When recognition was unreliable, verify all ordinary text was removed page-wide and rebuilt only from the aligned source.
- [ ] When art was repaired, verify all ordinary text was removed during the visual pass and re-typeset locally.
- [ ] Confirm wording, punctuation, simplified/traditional form, speaker, narration/dialogue role, and reading order against the novel alignment.
- [ ] Confirm every sound effect matches a depicted action and is correctly kept, removed, or rewritten.
- [ ] Confirm phone readability; prefer ≤25 Chinese characters per balloon without damaging meaning or pacing.
- [ ] Inspect at full size for white-on-white, black-on-black, ink blocks, overflow, clipping, touching borders, bad line breaks, doubled glyphs, residue, mask seams, or hidden key art.
- [ ] Treat automatic `D:\漫画文字修复\final` as a candidate only; confirm any font, color, mask, balloon, or layout override explicitly.

### File integrity

- [ ] Confirm final dimensions, color mode, format, transparency, and orientation meet project requirements.
- [ ] Confirm no accidental crop, scale change, compression damage, blank region, or candidate watermark.
- [ ] Confirm final promotion is atomic and `output_sha256` matches the promoted file.
- [ ] Confirm external text-repair candidates came from `D:\漫画文字修复\输出\repair-comic-continuity_<timestamp>` and were not written directly to the project output.

## Required Machine Check Sets

Require every listed check key to exist and equal `true`; summaries or partial dictionaries do not pass.

### `visual_review.checks`

- `original_candidate_comparison`
- `full_size_text`
- `full_size_artifacts`
- `reference_character_match`
- `novel_scene_match`

Also require `visual_review.status: passed`, `full_size: true`, reviewer, and zoned ISO 8601 `reviewed_at`.

### `page_qa.checks`

- `novel_fidelity`
- `character_identity`
- `skin_hair_crown`
- `anatomy_hands_face`
- `clothing_props`
- `extra_continuity`
- `scene_axis_time_weather`
- `text_accuracy_speaker`
- `text_density_contrast_layout`
- `sfx`
- `residual_text`
- `artifact_damage`
- `style_match`
- `mobile_readability`

Also require `page_qa.status: passed`, globally unique `pass_id`, reviewer, and zoned ISO 8601 `reviewed_at`.

### `style_review.checks`

- `unchanged_regions_preserved`
- `panel_geometry_preserved`
- `line_weight_brush_match`
- `color_texture_match`
- `face_simplification_match`
- `detail_density_match`
- `adjacent_comic_style_match`
- `character_reference_identity_only`
- `no_rendering_upgrade`

For every visual repair also require `style_review.status: passed`, `full_size: true`, reviewer, zoned ISO 8601 `reviewed_at`, a real comparison path/SHA-256 pair, and a boolean `full_page_exception_approved` consistent with `visual_edit_mode`.

### `batch_qa.checks`

- `story_order`
- `cross_page_character`
- `cross_page_extras`
- `cross_page_clothing_props`
- `cross_page_scene_axis`
- `cross_page_injuries_state`
- `text_sequence_speaker`
- `boundary_context`
- `page_count_order`
- `unresolved_issues_zero`

Also require `batch_qa.status: passed`, globally unique `pass_id`, reviewer, zoned ISO 8601 `reviewed_at`, and a review time later than every member page QA.

## Page Rework Gate

Set `page_qa_passed` only if all applicable checks pass. If any check fails:

1. Keep the existing final, if any, untouched.
2. Return the page to `planned` or `edited` as appropriate.
3. Record the exact defect, evidence, intended correction, and new candidate path.
4. Rework the page.
5. Repeat the entire page-level checklist, not only the failed item.
6. Repeat batch QA because a local repair can change cross-page continuity.

Hard-stop instead of reworking when source location, identity, input integrity, output ownership, or required text evidence is unresolved.

## Legacy QA Batch Checklist (Balanced, Maximum 12 Pages)

- [ ] Confirm `repair_log.json` has a true top-level `batches` collection.
- [ ] Set `batch_count=ceil(N/12)`, then distribute contiguous pages as evenly as possible with `divmod`; require all batch sizes to differ by at most one and never exceed 12.
- [ ] Confirm every page appears in exactly one batch and its page-level `batch_id` matches.
- [ ] Confirm `member_pages` is exact, unique, and naturally ordered.
- [ ] Confirm `context_before` and `context_after` list the exact immediately adjacent existing page IDs, with at most two on each side and fewer only at project boundaries.
- [ ] Verify natural order, including `252 → 252（1） → next` and `269 → 269（1） → next`.
- [ ] Verify all target pages reached `page_qa_passed` before batch review.
- [ ] Compare recurring faces, skin, hair, headwear, outfits, injuries, carried items, and bystanders across the sequence.
- [ ] Track each key prop through appearance, hand-off, use, loss, or destruction; verify count and owner on every page.
- [ ] Check scene axis, entrances, fixtures, weather, time, lighting, crowd population, and damage state across page boundaries.
- [ ] Read all dialogue, captions, and sound effects in order; verify speaker ownership, chronology, density, and causal timing.
- [ ] Confirm `unchanged_copy` pages align visually and narratively with repaired neighbors.
- [ ] Confirm no repair introduced a premature reveal, missing beat, duplicate action, or unsupported transition.
- [ ] Confirm every page has one coherent repair-log record and matching final hash.
- [ ] Confirm `batch_qa` exists only on the top-level batch, with reviewer, complete checks, and a timezone-aware ISO 8601 timestamp later than every member page's `page_qa`.
- [ ] Confirm this batch `pass_id` is distinct from every page and batch `pass_id` in the project.
- [ ] Set `batch_qa_passed` only after all boundary and sequence checks pass.

## Final Whole-Run Checklist

- [ ] Discover expected page count `N` from the naturally sorted, decodable input inventory and bind it in evidence.
- [ ] Confirm every input is decodable, unique, accounted for, and mapped to exactly one final output.
- [ ] Confirm all pages are in natural reading order and insert pages are correctly placed.
- [ ] Confirm “按时间顺序” was interpreted as novel/story chronology, not filesystem or run timestamps.
- [ ] Confirm the output directory contains only the `N` contiguous regular JPG files from `0001.jpg` through the zero-padded `N`th JPG; reject every subdirectory, hidden file, report, temporary file, timestamped page, or other entry.
- [ ] Confirm every final page has status `batch_qa_passed` and an `output_sha256` binding.
- [ ] Reconcile `comic_run_manifest.json`, `continuity_bible.json`, `novel_alignment.json`, `repair_log.json`, and `FINAL_QA_REPORT.md`.
- [ ] Confirm `continuity_bible.json` has top-level `status`, `reviewer`, timezone-aware ISO 8601 `reviewed_at`, ordered `source_priority`, `coverage`, and `locks`; reject obsolete top-level `time`.
- [ ] Confirm coverage includes `character`, `extra`, `clothing`, `prop`, and `scene`; each `confirmed` category has one or more same-category `lock_ids`, while each `not_applicable` category has empty `lock_ids` and a non-empty `rationale`.
- [ ] Confirm every lock has `lock_id`, `confirmed: true`, `reviewer`, `reviewed_at`, `category`, `subject`, `attribute`, `value`, `source_type`, `source_ref`, `applies_to_pages`, `confidence`, `created_by`, and `created_at`, with no lock-level `source_priority`.
- [ ] Confirm every page has an identical `continuity_lock_ids` set in `comic_run_manifest.json` and `repair_log.json`, and all IDs resolve to applicable locks.
- [ ] Confirm every novel alignment contains novel SHA-256, chapter/scene, exact offsets, exact `source_excerpt`, surrounding context, `scene_summary`, `involved_characters`, `dialogue_owners`, `props`, `location`, and `story_time`.
- [ ] Confirm visual, page-QA, and top-level batch-QA timestamps are timezone-aware ISO 8601 values; all page/batch `pass_id` values are unique across the entire project and batch times follow member-page QA times.
- [ ] Confirm no temporary candidate, mask, debug overlay, OCR artifact, or stale output is included in final delivery.
- [ ] Confirm top-level statuses: run manifest `batch_qa_passed`, continuity bible `confirmed`, novel alignment `confirmed`, repair log `batch_qa_passed`.
- [ ] Confirm `FINAL_QA_REPORT.md` has exactly the fixed completion keys, derives every count from `N`, reports `status=passed`, and has `blocking_issues=0`, `unresolved_issues=0`, reviewer, and zoned review time.
- [ ] Confirm `build_output_manifest.py` was run once immediately after Inventory, without `--force`, and was not rerun during final validation.
- [ ] Run `validate_output.py` and record its exact result in `FINAL_QA_REPORT.md`.
- [ ] Report completion only when validation succeeds; otherwise report the blocking pages and evidence.

## Immediate Red Flags

Reject the page or batch immediately for any of the following:

- Guessed novel wording, dialogue owner, character identity, or plot event.
- Damaged, duplicate, missing, or ambiguously ordered input.
- Face, skin, hair, headwear, costume, or prop-owner drift.
- A local face, hair, costume, prop, hand, or anatomy defect repaired by redrawing the whole page.
- Character-sheet linework, coloring, texture, facial detail, or illustration composition leaking into the comic.
- Page-wide art regeneration justified only by page-wide text removal.
- Missing visual mask, inaccurate changed-area ratio, missing adjacent style evidence, or missing style-comparison hash.
- Extra/missing limbs, malformed hands/faces, broken object contact, or duplicated props.
- Axis flip, impossible room geography, unexplained day/night, weather, lighting, or fixture change.
- Old and new text layered together; partial glyph replacement; OCR hallucination.
- White-on-white, black-on-black, ink-blocked, clipped, overflowing, residual, or phone-illegible text.
- Sound effect inconsistent with the depicted action.
- Automatic output accepted without full-size supervision.
- Final output written non-atomically or without matching `output_sha256`.
- External text-repair `--output` points at `D:\自动修改\7878\输出` or anywhere outside `D:\漫画文字修复\输出`.
- Final page names contain timestamps, gaps, alternate stems, or depart from the contiguous `0001.jpg`–`N`th-JPG contract.

## Common QA Errors

| Error | Corrective action |
|---|---|
| Review only the edited crop | Reopen the entire page at full resolution and check composition, text, and seams |
| Check only target pages | Include two pages before and after every batch |
| Fix the failed item only | Rerun the entire page checklist and then batch QA |
| Trust a stable-looking distant page | Reapply the evidence priority and prefer explicit novel/reference/adjacent evidence |
| Accept a correct sentence with bad typography | Fail it for contrast, density, overflow, residue, or reading order |
| Assume a nearby prop belongs to a character | Trace introduction and transfer; stop if ownership remains unresolved |
| Regenerate a correct page for consistency | Restore `unchanged_copy` and preserve the original pixels |
| Mark blocked evidence as a warning | Hard-stop and name the missing authoritative decision |
