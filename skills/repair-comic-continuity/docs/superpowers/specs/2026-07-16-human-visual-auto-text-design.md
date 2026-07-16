# Human-selected visual scope with full automatic text repair

## Goal

Add an optional `human_visual_auto_text` mode to the existing `repair-comic-continuity` Skill. The user supplies only the input page names or page numbers that are known to have visual continuity problems. That list limits whole-page visual redraw work, while text inspection and repair still run across every input page.

## Mode contract

The coordinator resolves the user's page list to exact input-relative paths and writes `evidence/human_visual_selection.json`:

```json
{
  "version": 1,
  "status": "confirmed",
  "mode": "human_visual_auto_text",
  "selected_pages": [
    {"path": "249.jpg", "sha256": "<sealed source hash>"}
  ],
  "visual_policy": "selected_pages_only",
  "text_policy": "all_input_pages_page_reset_preserve_style",
  "output_policy": "exact_input_bijection"
}
```

Selected pages must be unique, exist in the current sealed input, carry the current source SHA-256, and follow natural input order. An empty selection is valid when the user reports no visual defects.

## Visual path

- The manual list controls visual redraw scope only.
- Selected pages still require full-resolution evidence review against character references, adjacent pages, scene state, and novel context before a redraw decision.
- Only selected pages may enter `full_page_redraw`.
- Unselected pages receive a deterministic `human_visual_scope` audit record tied to the confirmed selection manifest. They do not consume model-based visual detection or redraw work.
- A nonempty selection requires a character appearance matrix whose `cluster_pages` exactly match the selected pages. With an empty selection the matrix is not applicable.

## Text path

Every input page still receives source-text detection, transcription, novel alignment, glyph review, and an independently confirmed source-text audit. Every ordinary text-bearing page uses `page_reset_preserve_style`: remove ordinary text and retypeset it while preserving the original font family, weight, color, size, position, orientation, line geometry, and balloon geometry. The visual selection list never narrows this text path.

## Output and fail-closed rules

Output remains an exact bijection with input: identical relative path, filename, extension, and page count. Unknown, duplicate, reordered, or hash-drifted selected pages fail before generation. An unselected `full_page_redraw`, a missing all-page source-text audit, or a missing required selected-page visual audit also fails before generation.

## Compatibility

If `human_visual_selection.json` is absent, the existing full automatic visual-audit mode remains unchanged. The new mode is implemented inside the same Skill because it changes audit scope, not the downstream text engine or output contract.
