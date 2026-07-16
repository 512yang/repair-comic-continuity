# QA checklist

## Before generation

- Confirm the novel hash, offsets, scene summary, cast, dialogue owners, props, location, and time for every page.
- Confirm semantic cluster membership, complete reference roles, stable comic anchors, and entity timeline transitions.
- Validate `character_appearance_matrix.json`; require exact cluster-page coverage and full-resolution, reference-bound checks for skin tone, hair, facial hair, clothing, face shape, and body build for every named character.
- Require two independent full-resolution audits and resolve any routed second review.
- Treat contact sheets as orientation only.
- Save the V5.5 run preview and verify input count equals expected output count, every page has a class, every input page retains the all-page text policy, and only confirmed `full_page_redraw` pages consume generation calls.
- When human annotations exist, require the controller's `annotations_ingested` receipt and a hash-current `human_issue_binding` in every annotated redraw request.

## Candidate review

- Verify source hash, candidate hash, task, page class, request, reference pack, generator, and timestamps.
- Require a full-resolution artifact for the original, candidate, and canonical side-by-side comparison.
- Use blind review: inspect the candidate against locked facts without trusting the generator's claimed result.
- Preserve panel geometry, composition, people, skin, hair, facial hair, clothing, props, recurring extras, scene axis, line work, color, texture, and detail density.
- Compare each candidate against the confirmed appearance matrix and reject any same-character skin-tone category jump, including a drift attributed only to vague water, shadow, or mood lighting.
- Reject self-review, stale artifacts, missing hashes, seams, unintended balloons, extra text, and reference contamination.
- For an annotated candidate, review every annotation ID and require both `required_state_met=true` and `unaffected_content_preserved=true`; save the independent result under `evidence/annotation_reviews`.

## Text review

- Compare each block with its novel-backed source slice and speaker.
- Build a full-resolution source glyph board that exactly covers every ordinary-text block before page classification. Require an explicit non-OCR-only shape decision for every occurrence of 强 and 遇; if any block or occurrence is missing, the page cannot be classified.
- Validate every page's `source_text_audit` with exact independent machine/visual block inventories and transcriptions, source-crop pixel binding, exact hash-bound novel offsets and semantic decisions, and explicit review of every adjacent Chinese repeat; an empty inventory needs a third independent full-resolution textless review, and a prose assertion of full inspection is not sufficient evidence.
- Preserve the original geometry, reading order, balloon style, and density.
- Compare every rendered block with its source `style_lock`: font asset, fill/stroke color, font size, spacing, writing mode, alignment, rotation, anchor, and original line boxes must match exactly.
- Reject font fallback, confidence below `0.95`, changed line count, silent shrink/reflow, `text_overflow`, or any newly created balloon.
- Inspect every Chinese glyph visually; OCR equality does not excuse a malformed glyph.
- Check `强`, `遇`, punctuation, art text, sound effects, and residual text at full resolution.
- Do not allow text repair to alter artwork.

## Final review

- Require passed page QA before passed cluster QA, and passed cluster QA before the final report.
- Confirm the exact relative-path set, including original filename case and extension, equals the input set.
- Confirm input and output counts both equal `N`, all tasks are completed, all registries are passed, and unresolved issues equal zero.
- Confirm failure-learning promotions have positive regression, clean control, variation, and independent review evidence.
- Confirm the closed-loop stage receipts are complete and ordered, all learned rules came from `load_effective_rules_for_page`, and no unresolved conflicting effective rules remain.
- Run read-only validation and reject any mismatch between files, hashes, registries, events, and `FINAL_QA_REPORT.md`.
