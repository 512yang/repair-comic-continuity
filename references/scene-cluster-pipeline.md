# Scene-Cluster Pipeline

Read this reference before creating `scene_cluster_v1` clusters, assigning parallel work, approving a canary, or promoting final pages.

Migration is dry-run only by default. Apply requires every novel alignment to be `confirmed`, a tamper-bound independent accepted migration review from someone other than the preparer, the exact proposal ID, and the explicit confirmation token. Any unresolved or provisional alignment blocks apply.

## Global Locks and Page Ownership

Freeze natural story order, novel offsets, source hashes, identity/state locks, reference roles, speaker graph, and output naming before task release. Let `N` be the discovered input count. Require a strict input/output bijection: each input owns one output index, and no output index has two inputs.

Partition contiguous story beats into normal clusters of 8–20 owned pages. Attach the exact available ±2 pages as read-only context; context never changes page ownership. Split on location, story-time, cast, costume, prop-state, or causal transitions. Only a whole-project cluster with total `N < 8` may set `boundary_exception=true` and `undersized_reason=project_total_below_min`; alignment gates still apply. Every other `undersized: true` cluster must remain blocked with `undersized_reason=scene_fragment_below_min`, and all of its pages must be `evidence_blocked` tasks of type `evidence_resolution`.

Keep scheduling class separate from repair operation: `page_class=full_page_redraw` maps to `action=full_page_regeneration`. Task queues and page-class fields use `full_page_redraw`; visual-edit and repair-action fields use `full_page_regeneration`.

## Reference Packs and Canary

Label every reference role explicitly:

| Role | Permitted use |
|---|---|
| `target_original` | Composition, correct pixels, panels, and target-page facts |
| `adjacent_style` | Established line, color, texture, simplification, and detail density |
| `primary_style` | Stable in-comic style anchor from an approved page |
| `identity_only` | Face, skin, hair, headwear, clothing structure, and signature props only |
| `composition_only` | Layout evidence only; never identity or style |

Reject reference contamination: rejected candidates, different scenes, identity sheets used as style, or candidate outputs used before promotion. Bind the ordered pack by hash.

`build_scene_clusters` must leave `reference_pack_id: null` and `reference_pack_state: unbound`; it must never invent a cluster-only pack identity. After `build_reference_pack`, call `bind_reference_pack(cluster, pack)` and persist only the returned copy with `reference_pack_state: bound`. The same pack is idempotent, while a different pack is a forbidden silent `rebind` and requires a separate audited workflow.

Persist the standard pack registry at `evidence/style_reference_packs.json` as an object with `schema_version`, `stable_pages`, `approved_hashes`, a `reference_packs` list, and `registry_hash`. Give every pack a unique deterministic `reference_pack_id`, its owning `cluster_id`, and a non-empty role-labeled `references` list whose paths, roles, and file hashes resolve through `approved_hashes`. Validate `primary_style` references against top-level `stable_pages`. Every new-mode page's `reference_pack_id` must resolve in this registry and the pack's cluster must equal the page's `cluster_id`.

## V3 Evidence Registries

Schema 3 requires exactly these five hash-bound registries: `scene_clusters.json`, `style_reference_packs.json`, `task_queue.json`, `failure_learning.json`, and `scene_cluster_qa.json`. Each stores a `registry_hash` computed from the canonical JSON object after omitting only the `registry_hash` field; any content change without recomputing it is tampering.

- `scene_clusters.json` must uniquely and exactly cover all output pages in story order, bind cluster size/undersized status, exact available ±2 context, canary membership, and `reference_pack_id`.
- `style_reference_packs.json` must bind stable-page eligibility, approved file hashes, deterministic pack identity, cluster ownership, and every reference path/hash/role.
- `task_queue.json` must contain one completed task per page and bind candidate hash, task page, cluster, prompt/reference hash, and completion actor.
- `failure_learning.json` must resolve every page's `failure_rule_ids` through validated rules.
- `scene_cluster_qa.json` must contain exactly one unique passed record per cluster, with identical members/canary, a zoned review time, and a reviewer independent of every generator in that cluster.

Choose one representative cluster canary containing the cluster's highest-risk cast, clothing, prop, scene, and style conditions. Run structured prompt compilation, generation, preflight, and independent review on the canary before releasing the remaining expensive image tasks. A failed canary pauses the lane and updates the cluster rule set; it is never silently accepted.

## Coordinator and Worker Contract

Use one coordinator and at most three workers on a four-slot machine. The coordinator is the only single-writer for shared evidence and final output. Each worker writes only inside its assigned candidate directory. Never let workers share a mutable candidate file.

Persist tasks with states `queued`, `leased`, `completed`, or `failed`. Require:

- deterministic `task_id` and idempotency key from page, task type, and payload hash;
- one active lease per page, with owner, leased time, and expiry;
- expired lease recovery back to `queued` through an atomic queue update;
- `completed` only with candidate path/hash and matched cluster/prompt/reference hashes;
- `failed` only with canonical failure code and traceable failure record;
- compare-and-swap revision plus temporary-file replacement for every atomic write.

The coordinator alone promotes a reviewed candidate. Re-running the same idempotency key must reuse the existing task or validated content cache entry, never create duplicate work.

For completed work, require `completed_by == generated_by`. For page QA, require `reviewed_by == page_qa.reviewer`. A full-page candidate also needs independent style and visual reviewers, and the cluster-QA reviewer must not be any `generated_by` actor from that cluster.

## Text Lane

Build a speaker graph before text work: nodes are speaker IDs; each balloon/caption is an owned utterance edge bound to page, panel, reading order, novel offsets, and source hash. Reject missing, ambiguous, or contradictory owners.

Set a positive integer `page_density_budget`. Count replacement characters after removing whitespace. Keep a balloon at 25 visible characters or fewer unless a non-empty density override explains why source-faithful shortening is impossible. The page total may never exceed the budget. Preserve only the explicit art-text allowlist.

## Preflight and Independent Review

Run deterministic preflight before any human/agent visual judgment:

- expected file type, dimensions, decodability, and candidate hash;
- page/task/cluster/reference-pack traceability;
- prompt/reference hash and effective failure rules;
- no crop, blank output, illegal text leak, watermark, or reference contamination;
- correct output index and no collision with another input;
- complete visual/text evidence required by the selected page class.

After preflight, require independent review against the original, canary, stable adjacent pages, novel facts, global locks, and full-size comparison. For generated pages, the generator and reviewer must differ. Record `generated_by` and `reviewed_by`; do not infer independence from different task labels.

Require non-empty `generated_by` and `reviewed_by` in both the manifest and repair log, with exact cross-file identity equality. For every non-`unchanged` page, reject `generated_by == reviewed_by`; full-page regeneration is always a hard rejection when the identities match.

## Promotion Gates

Promote only through the coordinator and only in this order:

`inventoried → located → locked → classified → queued → leased → candidate_ready → preflight_passed → independently_reviewed → page_qa_passed → cluster_qa_passed → promoted`

Never skip a state. A page in `failed`, `evidence_blocked`, or an expired/unresolved lease cannot enter final output. Rework invalidates later QA states and requires new candidate/hash evidence.

A non-boundary undersized cluster is a hard final-QA rejection. It cannot be approved by merely clearing `blocked` or renaming its tasks.

At finalization require:

- every owned page appears once in the manifest, repair log, queue, and output mapping;
- no duplicate/missing input or output mapping;
- no `queued`, `leased`, `failed`, or unknown task state;
- all rule IDs and review identities are traceable;
- exactly `N` final JPG files, ordered and hashed, with no extra entry.
