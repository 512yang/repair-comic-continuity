# Embedded text engine

The text engine is part of this Skill. Never depend on `D:\漫画文字修复` or a second Skill at runtime.

## Bundled components

- `scripts/text_engine_pipeline.py`: OCR orchestration, masks, cleanup staging, deterministic rendering, page isolation, reports, and exact-name output mapping.
- `scripts/text_cleanup_router.py`: fail-closed deterministic fill, LaMa, GPT Image 2, and blocked routing.
- `scripts/text_style_contract.py`: converts reviewed style and font measurements into hash-bound `style_lock` records.
- `scripts/comic_repair/`: font catalog, font matching, input ordering, style analysis, and text/background QA.
- `assets/text_fonts/`: seven reviewed Chinese font assets, catalog hashes, and OFL licenses.

LaMa model weights are a runtime cache, not a second project. Resolve them in this order: `REPAIR_COMIC_LAMA_ROOT`, `runtime/lama` inside the Skill, then the existing `D:\AI\LaMa_IOPaint` cache. Use `scripts/text_cleanup_router.py` for the exact route `deterministic fill -> LaMa -> GPT Image 2 -> evidence_blocked`; never fall back to OpenCV inpaint or a flat patch on artwork.

## Required page policy

Use `page_reset_preserve_style` for ordinary text:

1. Inspect the full-resolution source page and inventory every ordinary-text region. Keep reviewed SFX and art text separate.
   Before page classification, validate the hash-bound source audit with `scripts/validate_source_text_audit.py`; machine and independent visual block inventories must match, source crops must pixel-match the current page, adjacent Chinese repeats must be explicitly reviewed, and every block must bind exact novel offsets plus a semantic decision.
2. Bind each region to page, panel, speaker, balloon, bbox, original line boxes, reading order, OCR evidence, source text, replacement text, and novel offsets.
3. Analyze the source crop with the embedded style analyzer and font matcher. Build `style_lock` with font family, font asset hash, confidence, fill/stroke RGBA, font size, stroke width, letter/line spacing, writing mode, alignment, rotation, anchor, and line boxes.
4. Require style and font confidence of at least `0.95` with no fallback. Otherwise mark `evidence_blocked` and request full-resolution review; do not guess.
5. Remove the complete ordinary-text block. Use deterministic flat/background fill only where it cannot alter artwork. Use LaMa for texture or artwork overlap only after its canary passes. If LaMa is unavailable, fails, or cannot safely reconstruct the reviewed region, use GPT Image 2 to remove text only. It must never typeset Chinese, add a balloon, or alter page geometry; preserve everything outside the reviewed mask. Reject residual glyphs, seams, broken line art, background damage, or outside-mask drift.
6. Render the reviewed replacement with the locked font asset and exact style/geometry. Preserve the original line count. Do not add a balloon, move a block, or cover more artwork.
7. If the replacement cannot fit, emit `text_overflow`; do not shrink below the locked size, change line count, drop text, or save the page as passed.
8. Bind OCR, render manifest, source/candidate crops, and full-resolution glyph review to current hashes. OCR equality alone never proves glyph shape.

## Output and promotion

Internal staging may use temporary names, but final candidates and promoted outputs must use the exact input relative path, filename, Unicode spelling, case, and extension. Never use sequential names such as `1.jpg` or flatten nested paths.

The engine writes only to an isolated candidate directory below the project output workspace. It never promotes its own result. `candidate_preflight.py` must verify exact block coverage, style-lock equality, changed-pixel containment, OCR text, render-manifest crops, and visual glyph review before the coordinator can promote a page.

## Background routing

- Flat balloon/caption background with reviewed boundary: deterministic fill and redraw.
- Paper, cloud, watercolour, or light texture: LaMa with a tight binary PNG mask, followed by seam/background QA.
- Face, hair, hands, clothing line art, panel borders, or dense ink: LaMa only after a canary; failure routes to reviewed visual repair or `evidence_blocked`.
- When the LaMa canary is unsafe: build a hash-bound GPT Image 2 request with `scripts/text_cleanup_router.py`; remove text only, reconstruct the masked background, preserve everything outside the reviewed mask, and run independent outside-mask QA before deterministic typesetting.
- Never use a rectangular white patch, weak OpenCV inpaint, or full-page image generation merely to repair ordinary text. GPT Image 2 produces a textless cleanup candidate only; it never typesets Chinese.
