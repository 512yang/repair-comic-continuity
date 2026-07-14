# Continuity Rules

Use this reference when building locks, planning a page, or choosing between unchanged copy, text-only repair, visual repair, and hard stop.

## Stateful Continuity and Speaker Ownership

Track clothing, prop, and scene facts as explicit state machines. Every observable state is one of `present`, `absent`, or `unknown`; never treat `unknown` as evidence of absence or permission to invent presence.

| Domain | State key | Allowed transition | Required evidence |
|---|---|---|---|
| Clothing | character + garment/layer | `unknown → present`, `present → absent`, `absent → present` | novel costume change, visible dressing/removal, scene transition, or confirmed adjacent continuity |
| Prop | prop + owner/carrier + hand + condition | introduction, transfer, use, loss, destruction | novel action or visible causal hand-off; proximity alone is insufficient |
| Scene | location + fixture/axis/time/weather | entrance/exit, camera-axis transition, elapsed time, weather change | located novel beat or establishing panel; never teleport fixtures |

Use source priority in this order: explicit located novel fact, approved identity reference for stable identity only, stable adjacent comic page, distant established page, then conservative inference. A lower source cannot override a higher source. Resolve equal-rank conflicts before promotion.

Keep each unnamed `extra` consistent inside its scene by role, build, clothing family, screen position, and distinguishing features. Do not give an extra a protagonist face, named identity, signature prop, or unexplained costume change.

Build a `speaker graph`: bind every dialogue balloon, narration box, and ordinary sound-effect text to page, panel, reading order, `speaker_id`/role, and exact novel offsets. Missing or contradictory ownership is `evidence_blocked`; never guess the speaker.

Classify `art text` separately from ordinary dialogue, narration, and sound effects. Preserve only source-backed items in the explicit art-text allowlist. Delete or rebuild malformed art text only when its depicted action and timing are supported.

## Evidence Matrix

| Rank | Evidence | Use | Conflict rule |
|---:|---|---|---|
| 1 | Explicit novel/script statement at the located beat | Identity, action, dialogue, costume change, prop state, place/time | Overrides visual convention and distant continuity |
| 2 | Approved character reference image | Face, skin tone, hair, headwear, baseline costume, signature prop | Overrides unstable generated pages; defer to explicit scene-specific novel changes |
| 3 | Stable adjacent pages (±2) | Current wardrobe, position, hand-held prop, axis, damage/state, lighting | Require repetition or causal support; ignore a visibly defective neighbor |
| 4 | Distant stable pages | Persistent traits and recurring locations | Use only when higher-ranked evidence is silent and scene state has not changed |
| 5 | Conservative inference | Minimal fill for unobservable details | Never create plot, identity, ownership, text, or a new continuity fact |

Record the evidence source and confidence for every lock. When equal-rank sources conflict, stop if the conflict affects identity, wording, plot causality, or irreversible final output. Otherwise choose the least assumptive option and document it.

## Style Evidence Matrix

Keep style evidence separate from plot and identity evidence. A high-priority identity reference does not become a high-priority style reference.

| Rank | Style evidence | Allowed use | Reject when |
|---:|---|---|---|
| 1 | Correct regions of the target original page | Line weight, brush texture, color density, paper texture, detail density, panel geometry | A repair changes correct regions or upgrades rendering |
| 2 | Stable adjacent comic pages (±2) | Current scene rendering, facial simplification, costume depiction, effects | A defective neighbor or different scene is treated as authoritative |
| 3 | Confirmed same-character comic panels | In-comic face, hair, costume, and signature-prop depiction | A distant state change is copied without causal support |
| 4 | Character sheet marked `identity_only` | Face shape, skin, hair, headwear, clothing structure, signature props | Its polished linework, coloring, texture, detail, or composition leaks into the page |

Require the original page plus at least one stable adjacent page when adjacent pages exist. If no stable adjacent page exists, record the exact same-character comic panels used instead. Never use a character sheet as `primary_style`.

## Continuity Bible Structure

Require these top-level fields: `status`, `reviewer`, timezone-aware ISO 8601 `reviewed_at`, `source_priority`, `coverage`, and `locks`. Set `status: confirmed` and set `source_priority` exactly to `["novel", "reference", "adjacent_page", "established_page", "inference"]`. Store locks as structured objects with exactly the fields below and no extras:

| Lock field | Requirement |
|---|---|
| `lock_id` | Unique across the project |
| `confirmed` | Literal `true`; unconfirmed decisions are not locks |
| `reviewer`, `reviewed_at` | Reviewer plus timezone-aware ISO 8601 review time |
| `category` | One of `character`, `extra`, `clothing`, `prop`, `scene` |
| `subject` | Stable character, extra group, garment, prop, or scene identifier |
| `attribute` | The property being locked |
| `value` | Evidence-backed canonical value |
| `source_type`, `source_ref` | One canonical source type and a resolvable source reference |
| `applies_to_pages` | Exact page IDs covered by the lock |
| `confidence` | Explicit confidence value using the project convention |
| `created_by`, `created_at` | Creator plus timezone-aware ISO 8601 creation time |

Do not put `source_priority` inside a lock and do not use top-level `time`; use top-level `reviewed_at`.

Require `coverage` for all five categories. A `confirmed` entry must contain one or more `lock_ids`, and every referenced lock must have the same category. A `not_applicable` entry must contain empty `lock_ids` plus a non-empty `rationale`. For every page, require the `continuity_lock_ids` sets in `comic_run_manifest.json` and `repair_log.json` to match exactly and resolve to confirmed locks whose `applies_to_pages` includes the page.

## Continuity Lock Matrix

| Domain | Lock fields | Required cross-check | Reject when |
|---|---|---|---|
| Named character | identity, skin tone, face shape, eyes, hair style/color, headwear | character reference, located novel beat, adjacent pages | face/skin/hair drift, swapped identity, lost headwear without cause |
| Clothing | layers, cut, color, shoes, accessories, damage/wetness | scene transition and explicit costume change | unexplained outfit/color change or layer teleportation |
| Crowd/extra | role, approximate age/build, clothing family, screen position | adjacent panels and scene population | unsupported named identity, duplicate protagonist face, recurring extra drift |
| Anatomy | limb count, pose, hand-object contact, gaze, expression | action beat and previous/next pose | extra/missing limb, fused fingers, broken hand-off, wrong emotion |
| Prop | description, owner, carrier hand, presence, orientation, state | introduction, transfer, use, loss, destruction | owner swap, teleportation, unexplained duplication/disappearance |
| Scene | location, camera axis, entrances, windows, major fixtures, horizon | adjacent establishing views and script geography | axis flip without transition, fixture relocation, impossible geography |
| Atmosphere | time, weather, light direction/color, surface wetness | scene chronology and exterior/interior relation | instant day/night or weather change without source support |
| Ordinary text | exact source wording, speaker/role, box/balloon type, position, reading order | novel alignment and original page capture | guessed wording, wrong speaker, residue, overflow, unreadable contrast |
| Sound effect | action source, timing, wording, position, visual integration | depicted action and causal beat | effect without action, retained effect after action removal, obscured key art |

## Repair Decision Table

| Observation | Repair class | Text treatment | Promotion condition |
|---|---|---|---|
| Art and text are correct | `unchanged_copy` | Preserve exactly | Hash and order recorded |
| Art correct; one or more text blocks wrong | `text_only` | Capture all text; erase and rewrite every affected block | Full-size typography QA passes |
| Art wrong; source and wording are reliable | `visual_and_text` | Capture metadata; remove all ordinary text during visual repair; re-typeset locally | Art, text, and continuity QA pass |
| Local visual defect | `localized_inpaint` or `localized_regeneration` | Keep text and visual masks separate; composite only the masked visual region onto the original page | Mask/hash, changed-area ratio, style references, and style review pass |
| Page-wide scene/composition defect that cannot be repaired locally | `full_page_regeneration` | Reset text separately and rebuild only after page-wide art approval | Page-wide extent, full mask, reason, comparison evidence, and explicit exception approval pass |
| Ordinary text recognition unreliable; novel location reliable | `page_text_rebuild` | Remove all ordinary text without discrimination; reconstruct from source | Every reconstructed block and speaker verified |
| Novel location or dialogue ownership unreliable | `no_text_blocked` | Produce only a no-text candidate | Hard-stop pending authoritative evidence |
| Identity/reference conflict | `identity_blocked` | Do not finalize | Hard-stop pending resolution |

Do not treat sound effects as ordinary text automatically. First identify the depicted action; then preserve, remove, or rebuild the effect according to that action.

## Text Reset Evidence Matrix

| Condition | `repair_scope` | `had_ordinary_text` | `text_recognition` | Reset | `text_reset_reason` |
|---|---|---:|---|---|---|
| Correct page | `unchanged` | observed boolean | `not_applicable` | `none` | `unchanged_page` |
| Visual repair with ordinary text | `visual` | `true` | `reliable` or `unreliable` | `page` | `visual_repair_requires_page_reset` |
| Visual repair without ordinary text | `visual` | `false` | `not_applicable` | `none` | `no_ordinary_text` |
| Text-only repair; affected block reliably recognized | `text_only` | `true` | `reliable` | `block` | `reliable_block_match` |
| Text-only repair; ordinary text not reliably recognized | `text_only` | `true` | `unreliable` | `page` | `unreliable_text_page_reset` |

Record all four evidence fields even when reset is `none`. Treat `block` as invalid for unreliable recognition and treat `none` as invalid when visual repair contains ordinary text.

## Visual Decision Rules

1. Preserve the original page as the base. Repair local defects with a local visual mask and composite only that region back; never redraw the whole page for convenience.
2. Preserve composition and story beat unless the source proves they are wrong.
3. Correct skin tone, face shape, hair, headwear, clothing, props, limbs, hands, and facial features as one coherent identity repair; avoid partial fixes that leave contradictory cues.
4. Treat character references as `identity_only`; take linework, coloring, texture, facial simplification, and detail density from the comic.
5. Keep the ordinary-text mask separate from the visual-defect mask. Page-wide text reset does not permit page-wide art regeneration.
6. Keep unnamed bystanders generic and stable. Never assign a named-character face merely because it is visually convenient.
7. Maintain axis, entrances, furniture, weather, time, and lighting continuity across the batch boundary.
8. Track every prop from introduction through transfer, use, loss, or destruction. Do not infer ownership from proximity alone.
9. Keep dialogue and captions phone-readable. Aim for ≤25 Chinese characters per balloon; do not split a grammatical unit merely to hit the target.
10. Do not add actions, emotions, objects, people, dialogue, or sound effects absent from the located source and established continuity.
11. Interpret chronological order as the novel's story sequence. Do not use filesystem timestamps or candidate-run timestamps to infer story order.
12. Reserve timestamps for external candidate run directories. For discovered input count `N`, name accepted output contiguously from `0001.jpg` through the zero-padded `N`th JPG.

## Excellent Example

Page `252（1）` shows 林岚 exiting a rain-soaked alley. The novel explicitly says she still carries 周野's red umbrella in her left hand; the approved reference fixes her warm skin tone, oval face, black shoulder-length hair, and silver hairpin. Pages `251` and `252` show a navy coat and wet pavement. Page `253` shows the umbrella returned to 周野.

The candidate instead gives 林岚 pale skin, brown long hair, no hairpin, a beige coat, and the red umbrella in her right hand. A dialogue balloon contains one confirmed typo.

Apply these decisions:

- Rank the novel statement first for umbrella ownership and left-hand carry.
- Use the character reference for skin, face, hair, and hairpin.
- Use adjacent stable pages for the navy coat, rain state, alley axis, and wet pavement.
- Record the planned transfer on page `253`; do not give 周野 a second umbrella on `252（1）`.
- Create confirmed character, clothing, prop, and scene locks with unique IDs; mark `extra` as `not_applicable` with the rationale that no bystanders appear in this page scope.
- Put the same applicable character/clothing/prop/scene lock-ID set in page `252（1）`'s `continuity_lock_ids` in both `comic_run_manifest.json` and `repair_log.json`.
- Save the original dialogue text, geometry, speaker, and style.
- Because the art needs repair, remove all ordinary text with a separate text mask while repairing only the visual-defect mask; composite the local art repair onto the original page.
- Rewrite the complete affected dialogue block from the aligned novel passage, not only the typo.
- Review at full size for hairpin visibility, correct left-hand contact, umbrella uniqueness, readable contrast, no residue, and natural order between `252` and `253`.
- Store page `252（1）`'s `page_qa` only in its page record. Put it in exactly one top-level batch's ordered `member_pages`; record the exact preceding/following page IDs in that batch's context lists and store `batch_qa` only on the batch.
- If its candidate came from the external text tool, record the exact direct-child run directory, the source inside its `final`, matching external/project candidate hashes, and `supervised_import` reviewer/time. Otherwise set `used_external_text_resource: false` and `external_candidate: null`.
- Promote only after page QA and boundary-aware batch QA both pass; ensure the batch timestamp is later and its `pass_id` is globally distinct; otherwise rework the candidate.
