# QA checklist

## Before generation

- Confirm the novel hash, offsets, scene summary, cast, dialogue owners, props, location, and time for every page.
- Confirm semantic cluster membership, complete reference roles, stable comic anchors, and entity timeline transitions.
- Require two independent full-resolution audits and resolve any routed second review.
- Treat contact sheets as orientation only.

## Candidate review

- Verify source hash, candidate hash, task, page class, request, reference pack, generator, and timestamps.
- Require a full-resolution artifact for the original, candidate, and canonical side-by-side comparison.
- Use blind review: inspect the candidate against locked facts without trusting the generator's claimed result.
- Preserve panel geometry, composition, people, skin, hair, facial hair, clothing, props, recurring extras, scene axis, line work, color, texture, and detail density.
- Reject self-review, stale artifacts, missing hashes, seams, unintended balloons, extra text, and reference contamination.

## Text review

- Compare each block with its novel-backed source slice and speaker.
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
- Run read-only validation and reject any mismatch between files, hashes, registries, events, and `FINAL_QA_REPORT.md`.
