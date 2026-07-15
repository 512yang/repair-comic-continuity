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

## Promote candidates

Class-specific preflight precedes blind review. The generator and reviewer must differ. Page review must occur after candidate creation; cluster QA must occur after every member page review; the final report must occur after cluster QA.

Promotion preserves the exact input/output bijection and exact relative filenames. No migration confirmation may create V4 audits or pass states. V3 proposals remain diagnostic and unresolved.

The coordinator composes the small queue, audit, prompt, preflight, learning, and validation libraries. There is no shortcut generation command that bypasses this sequence.
