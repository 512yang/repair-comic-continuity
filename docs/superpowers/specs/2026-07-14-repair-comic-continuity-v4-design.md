# Repair Comic Continuity V4 Design

## Purpose

Upgrade `repair-comic-continuity` from a documented workflow into an enforceable, evidence-backed comic repair pipeline. V4 must preserve every correct page, detect known continuity and text failures, block uncertain cases, repair only confirmed defects, and keep every output image's relative path, filename, extension, dimensions, and story position aligned with its input.

The design is based on the 2026-07-14 failed 98-page run and the user's corrections to its review criteria. The failed run is a regression source, not proof of completion.

## Goals

- Review every decodable input page at full resolution.
- Treat comic continuity as the primary visual standard; do not require shot-for-shot novel reenactment.
- Track character, facial-hair, costume, prop, and scene state across adjacent and distant appearances.
- Keep correct pages byte-identical.
- Use full-page, text-free redraws for confirmed visual defects under the default V4 repair profile.
- Restore Chinese text deterministically after visual approval.
- Require independent, evidence-backed review before promotion.
- Scale to hundreds of pages with semantic scene clusters, three parallel workers, caching, canaries, checkpoints, and circuit breakers.
- Produce exactly one output for every input with the same relative path, filename, and extension.
- Make incomplete or suspicious evidence fail closed.

## Non-Goals

- Reproduce every pose, grip, expression, camera angle, or action described by the novel.
- Improve or modernize the established comic style.
- Regenerate correct pages for visual polish.
- Let a generator approve its own output.
- Let a post-processing script manufacture `passed` evidence without review artifacts.
- Process project images while implementing or validating the reusable Skill.

## Recommended Architecture

V4 uses four separated layers:

1. **Evidence layer** inventories immutable inputs, decodes the novel without modifying it, aligns pages, builds semantic scene clusters, and records entity state timelines.
2. **Audit layer** performs full-resolution primary review and risk-adaptive independent review without modifying images.
3. **Repair layer** creates isolated candidates for pages with confirmed defects only.
4. **Promotion layer** validates candidates, performs blind review and cluster review, then atomically promotes exact-name outputs.

Audit decisions are immutable inputs to repair. Repair cannot silently reclassify a page; it must reopen audit with new evidence.

## Core Policy: Continuity First

The novel is a guardrail, not a storyboard.

Visual review may use the novel to decide:

- identity and real appearance changes;
- costume transitions;
- prop introduction, ownership, transfer, damage, loss, or destruction;
- scene, time, weather, injury, and plot-state transitions;
- contradictions that make the comic logically impossible.

The following are not defects by themselves:

- one hand versus two hands on a prop;
- an equivalent pose or facial expression;
- a different camera angle;
- an altered non-critical object count or arrangement;
- compressed staging that preserves story logic.

Ordinary text remains source-faithful. Visual continuity and text fidelity are separate review dimensions.

## Exact Input/Output Contract

Discover inputs by natural story order, but use an internal `page_id` only for scheduling. Never use the internal index as the output filename.

For every input image:

- preserve its relative path, basename, extension, and case-preserving spelling;
- preserve its width and height unless an explicit project contract authorizes a different canvas;
- map it to exactly one output and map every output back to exactly one input;
- keep an `unchanged` output byte-identical to the input;
- create no additional regular image in the final output tree.

The validator compares normalized relative-path sets and rejects missing, extra, renamed, duplicate, or extension-changed outputs. For the failed-run example, `191.jpg` must remain `191.jpg`; it must not become `0003.jpg`.

## Source Encoding Contract

Read the novel without altering the source file:

1. preserve and record the raw-byte SHA-256;
2. detect BOM when present;
3. try strict UTF-8;
4. fall back to strict GB18030 when UTF-8 fails;
5. record the chosen encoding and decoded-text SHA-256;
6. block ambiguous or lossy decoding.

Text offsets refer to the decoded text and remain bound to both hashes and the recorded encoding.

## Semantic Scene Clusters

Create clusters from story boundaries, not fixed page counts. A boundary is supported by changes in location, time, cast, costume state, major prop state, or explicit transition.

- Prefer 8-20 owned pages for throughput.
- Allow shorter semantic scenes with a recorded reason.
- Never merge different scenes merely to satisfy the preferred size.
- Attach up to two context pages on each side without duplicating ownership.
- Select a canary only for a cluster that contains expensive visual work.
- Select the highest-risk visual page as the canary, not a fixed ordinal page.

Each cluster records why it begins and ends, its cast, persistent props, costume state, scene fingerprint, context pages, and confidence.

## Entity State Timeline

Add `entity_state_timeline.json` as a required V4 evidence registry. Track page intervals and source-backed transitions for:

### Characters

- identity, apparent age, face shape, skin tone;
- eyebrows, eye shape and eye color;
- hair style and hair color;
- beard, moustache, sideburn, and stubble type, length, density, color, and connection pattern;
- scars, moles, wrinkles, and other stable marks;
- height, build, shoulder width, and musculature;
- headwear, crown, ribbon, clothes, shoes, accessories, fixed weapons, and recurring props.

### Props

- shape, material, color, ornamentation, scale, owner, location, and condition;
- introduction, transfer, damage, repair, loss, and destruction events.

### Scenes

- location identity, indoor/outdoor state, entrances, windows, furniture, axis, time, weather, lighting, and persistent environmental state.

Compare each appearance with adjacent stable pages and canonical long-range anchors. This prevents gradual drift that an adjacent-only check misses.

## Reference-Pack Integrity

Every cluster reference pack has explicit roles:

- `target_composition`: the original target page;
- `comic_style_anchor`: reviewed stable comic pages;
- `identity_only`: character sheets for the actual cast;
- `prop_anchor`: reviewed prop appearances;
- `scene_anchor`: reviewed location appearances.

Reject a pack when:

- it has no stable comic style anchor for a visual task;
- it binds every cluster to an arbitrary first reference file;
- it lacks coverage for a named character being repaired;
- a rejected or unreviewed candidate is used as an anchor;
- an identity sheet is used as a style source;
- its declared cast conflicts with the scene cluster or timeline.

Reference anchors are versioned. Updating an anchor invalidates dependent approvals until they are rechecked.

## Dual Review and Confidence Routing

Use two separated review perspectives:

1. **Continuity reviewer:** sees the comic pages, stable anchors, and character references, but not detailed novel action wording. Checks identity, facial hair, costume, props, scene, anatomy, and style.
2. **Source reviewer:** sees the aligned novel evidence and the page. Checks plot contradiction, state transitions, dialogue ownership, and ordinary text.

Each issue records evidence, canonical error codes, confidence, and the smallest supported repair scope.

- High-confidence correct: classify `unchanged`.
- High-confidence defect: classify `text_only` or `full_page_redraw`.
- Medium confidence: require a second independent review.
- Low confidence or conflicting evidence: classify `evidence_blocked`.

The coordinator resolves reviewer disagreement. Neither reviewer can promote a candidate alone.

## Full-Resolution Audit Evidence

Contact sheets and thumbnails are orientation aids only. They can never be pass evidence.

Every page must record:

- source hash and full-resolution dimensions;
- the review board or crop artifacts and their hashes;
- inspected panels and entities;
- character, facial hair, anatomy, costume, prop, scene, style, text, and SFX checks;
- review perspective, reviewer, time, confidence, findings, and classification.

An `unchanged` page requires negative evidence across the full check matrix. A bare `full_size: true`, a repeated boilerplate sentence, or a missing comparison artifact fails validation.

## Page Classes and Repair Profile

V4 keeps four classes:

| Class | Action |
|---|---|
| `unchanged` | Losslessly copy the source under the exact same relative path. |
| `text_only` | Keep all artwork fixed and repair declared ordinary-text regions. |
| `full_page_redraw` | Generate a text-free full-page candidate using the original page and validated references. |
| `evidence_blocked` | Produce no promotable final page until evidence is resolved. |

The default profile is `continuity_first_full_page`:

- every confirmed visual defect uses a full-page, text-free redraw;
- local inpainting is disabled unless a project explicitly selects a different profile;
- the redraw preserves canvas size, panel topology, reading order, camera intent, major subject placement, established style, and correct story content;
- the image model never renders final Chinese ordinary text.

## Deterministic Text Layer

Capture text and geometry before visual work. Bind every ordinary-text block to panel, balloon or caption shape, speaker, original text, replacement text, reading order, and exact novel offsets.

- Use `block_replace` only when recognition and geometry are reliable.
- Use `page_reset` when any ordinary-text block is uncertain: remove all ordinary text, then rebuild only declared source-backed blocks.
- Preserve existing balloon and caption geometry unless the source layout is unusable and the evidence records an approved adjustment.
- Do not add a dialogue box merely to fit text.
- Preserve only allowlisted art text and SFX.
- Render Chinese text deterministically with the project font profile; do not ask the image generator to draw it.
- Run source-string comparison, OCR, and full-resolution glyph-shape review after rendering.
- Include malformed-glyph regression cases such as visually incorrect forms of `强` and `遇` even when OCR returns the intended character.

For `text_only`, pixels outside approved masks must remain identical, allowing only declared antialiasing tolerance at mask boundaries.

## Candidate, Canary, and Circuit Breaker

- Workers write only to worker-isolated candidate directories.
- A single coordinator owns evidence updates and final promotion.
- Run at most three workers on a four-slot machine.
- Reuse hashes, decoding, novel alignment, OCR, reference packs, and reviewed anchors through a content-addressed cache.
- Generate one highest-risk canary before releasing the remaining visual tasks in its cluster.
- Allow at most two generation attempts for the same failure family. A repeated failure opens the circuit breaker and returns the page to diagnosis.
- Never use a rejected candidate as reference evidence.
- Save durable checkpoints so interrupted runs resume without repeating completed audit or generation.

## Independent and Blind QA

Every generated or externally typeset candidate requires `generated_by != reviewed_by`.

Primary page QA compares the source, candidate, stable adjacent pages, canonical anchors, state timeline, and applicable novel facts at full resolution. A second blind reviewer examines modified pages, uncertain pages, cluster boundaries, and selected clean controls without being told what was changed or what the generator intended to fix.

QA gates:

1. **Page gate:** no new identity, anatomy, costume, prop, scene, style, text, or artifact defect.
2. **Cluster gate:** cross-page states and boundaries remain coherent.
3. **Book gate:** long-range entity timelines, exact output mapping, counts, hashes, text, and unresolved issues pass.

## Evidence Integrity and Anti-Fabrication

V4 evidence is append-only for review events and hash-bound for registry snapshots.

The validator rejects:

- mass-generated `passed` fields without artifact hashes;
- identical novel offsets or boilerplate facts reused across implausibly many pages;
- timestamps that imply review before candidate creation;
- the same actor recorded as generator and reviewer;
- missing or unreadable review artifacts;
- reference packs with empty stable anchors for visual work;
- scene QA records created before every member page passed;
- a final report that disagrees with registry states;
- any attempt to force, rebuild, or backfill evidence during final validation.

Final reports are generated from validated registries. If any gate is pending or failed, the only valid overall status is `blocked` or `failed`; the report cannot claim completion.

## Failure Learning

Record candidate failures with before evidence, error codes, diagnosis, corrective constraint, after evidence, and outcome. A proposed rule becomes active only after:

1. it fixes its positive regression case;
2. it does not alter clean controls;
3. it passes at least one varied case;
4. it receives independent review.

Rules are versioned and revocable. The system does not autonomously promote a rule from one failure.

## Regression Corpus

Build synthetic and metadata-only fixtures that reproduce the failed run without publishing copyrighted comic pages or novel text.

Required cases include:

- renamed outputs despite equal counts;
- unconfirmed novel alignments;
- pending page and final QA;
- fixed-size page-number clusters that ignore semantic boundaries;
- empty stable pages and arbitrary single-character reference binding;
- repeated novel offsets and boilerplate page facts;
- hard-coded `True` review fields without artifacts;
- missing facial hair and long-range identity drift;
- costume, prop, scene, anatomy, and style drift;
- exact-action differences that must not trigger repair;
- clean pages that must remain byte-identical;
- text-only edits that modify artwork outside the mask;
- added dialogue boxes, changed text geometry, wrong text, and malformed glyphs;
- full-page redraw candidates with panel, composition, or style drift;
- exact filename and extension preservation.

Before repair-capable execution, run an audit-only dry run against the 98-page project. It may write evidence and reports but must not create or promote image candidates. Known confirmed defects must be found, non-defect action differences must remain unflagged, and clean controls must remain `unchanged`.

## Performance Design

For a typical 100-page project with 20 confirmed visual pages, three workers, and a previous baseline of ten minutes per generated page:

- full-resolution audit and alignment target: 45-90 minutes;
- visual generation target: 70-110 minutes;
- deterministic text work target: 20-45 minutes;
- independent QA and promotion target: 30-60 minutes;
- expected total: approximately 2.5-5 hours.

These are planning estimates, not completion claims. A ten-page benchmark run must record actual stage timings before publishing performance expectations.

Speed safeguards that do not reduce quality:

- audit every page once at full resolution;
- second-review only modified, uncertain, boundary, anchor, and sampled clean pages;
- generate only confirmed visual pages;
- skip image-generation canaries for clusters with no visual task;
- reuse immutable evidence through content-addressed caching;
- parallelize independent cluster work, but serialize promotion;
- stop repeated failure families after two attempts.

## Schema and Files

New runs use `pipeline_mode: continuity_v4` and `schema_version: 4.0`.

Required evidence includes existing V3 registries plus:

- `entity_state_timeline.json`;
- `page_audit.json`;
- `review_events.jsonl`;
- `text_geometry.json`;
- `regression_summary.json` for Skill validation and project dry runs.

V3 evidence remains readable for diagnosis, but it cannot be promoted as V4 by deleting fields or mass-filling defaults. Migration is dry-run only and must preserve source hashes and unresolved status.

## Implementation Boundaries

- Keep `SKILL.md` concise: core policy, required references, pipeline order, classes, and hard completion gates.
- Put detailed continuity and entity rules in `references/continuity-rules.md`.
- Put semantic clustering, dual review, canaries, caching, and promotion in `references/scene-cluster-pipeline.md`.
- Put evidence integrity and all QA matrices in `references/qa-checklist.md`.
- Put rule promotion and circuit-breaker behavior in `references/failure-learning.md`.
- Put deterministic constraints in focused Python modules and tests rather than prose alone.
- Regenerate `agents/openai.yaml` only if its user-facing description becomes stale.
- Repair all mojibake and validate every Skill text file as UTF-8.

## Acceptance Criteria

The Skill revision is accepted only when:

- structural Skill validation passes;
- all existing and new unit tests pass cleanly;
- every new behavioral test was observed failing before its implementation;
- exact input/output relative paths, filenames, and extensions are enforced;
- the failed-run evidence fixture is rejected for the expected reasons;
- post-hoc mass-filled evidence is rejected;
- known continuity positives are detected;
- exact-action non-defects and clean controls are not scheduled for repair;
- correct pages remain byte-identical;
- text-only changes preserve pixels outside approved masks;
- audit-only dry run produces no image candidate and no final output;
- no project image processing occurs during reusable-Skill validation;
- the installed Skill copy matches the validated repository revision;
- GitHub receives the reviewed, committed revision only after local validation passes.
