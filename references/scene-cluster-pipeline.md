# Scene-cluster pipeline

## Build clusters

Partition naturally ordered pages by semantic story continuity: chapter, location, story time, cast, costumes, props, injuries, weather, and scene axis. Normal clusters own 8–20 pages and may inspect up to ±2 neighboring context pages. Context never creates duplicate ownership.

Every page belongs to exactly one cluster. Each cluster binds one role-complete reference pack, a stable comic-style anchor, its exact member pages, issue schedule, and a canary when expensive visual work exists.

Reference roles are exact: target composition, comic style, identity, prop, and scene. Character sheets are identity-only. A visual pack must cover the complete involved cast; unrelated identity sheets do not satisfy coverage.

Track `reference_pack_state` explicitly. A new cluster starts `unbound`; `bind_reference_pack` verifies the content-addressed pack and moves it to `bound`. Any later `rebind` requires a new pack identity plus reviewed audit evidence; never replace references behind an existing binding.

## Schedule safely

Use one coordinator and at most 3 workers. Tasks move through `queued`, `leased`, `completed`, or `failed`. One worker holds at most one live lease, and two workers cannot own the same page. Evidence and final output use atomic single-writer promotion.

Release expensive work only after an independently reviewed canary passes. `unchanged` and `text_only` pages do not call the image generator. A two-attempt same-family circuit breaker pauses the affected lane and requires a reviewed diagnosis before reopening.

A valid signed generic release certificate removes only redundant cross-installation golden-sample labeling. Every new project still runs one automatic full-cluster canary. Escalate to the user only for unresolved independent-auditor disagreement, `evidence_blocked`, an invalid certificate, or a new uncovered failure family.

## Audit-only mode

Before generation, `validate_audit.py` proves alignment, clusters, references, timelines, full-resolution audits, second reviews, and final classifications. It rejects candidates, completed repair tasks, and output images.

### Human-selected visual scope

Use `human_visual_auto_text` when the user supplies only the visual-problem page numbers or filenames. The manual list controls visual redraw scope only; it does not narrow text detection or repair. Resolve the user's list to exact input-relative paths and write this hash-bound evidence before audit:

```json
{
  "version": 1,
  "status": "confirmed",
  "mode": "human_visual_auto_text",
  "selected_pages": [
    {"path": "249.jpg", "sha256": "<sealed-source-sha256>"}
  ],
  "visual_policy": "selected_pages_only",
  "text_policy": "all_input_pages_page_reset_preserve_style",
  "output_policy": "exact_input_bijection"
}
```

Store it at `evidence/human_visual_selection.json`. Selected paths must be unique, safe, present in the sealed input, hash-current, and listed in natural input order. An empty selection is valid. Unknown, duplicate, reordered, or hash-drifted paths fail closed.

Selected pages retain a full-resolution `continuity` audit with a `full_resolution_page` artifact and exact appearance-matrix coverage. Unselected pages must instead carry one `human_visual_scope` audit whose `user_selected_page_list` artifact binds the manifest path and SHA-256; unselected pages cannot enter `full_page_redraw` or carry a redundant full-resolution visual audit.

In this mode, every input page still receives independent machine/visual source-text coverage, novel alignment, crop evidence, repeat and malformed-glyph checks, and a confirmed source-text audit. Every text-bearing page follows `page_reset_preserve_style`: preserve font family, weight, color, size, position, writing direction, line geometry, and balloon geometry. The final output remains an exact input/output bijection with identical relative filenames and count.

## Promote candidates

Class-specific preflight precedes blind review. The generator and reviewer must differ. Page review must occur after candidate creation; cluster QA must occur after every member page review; the final report must occur after cluster QA.

Promotion preserves the exact input/output bijection and exact relative filenames. No migration confirmation may create V4 audits or pass states. V3 proposals remain diagnostic and unresolved.

The coordinator composes the small queue, audit, prompt, preflight, learning, and validation libraries. There is no shortcut generation command that bypasses this sequence.
