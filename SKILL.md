---
name: repair-comic-continuity
description: Use when Chinese comic or comic-drama pages must be repaired against a novel, script, character references, and adjacent pages for character, crowd, costume, prop, scene, anatomy, text, sound-effect, or cross-page continuity, especially in projects with input/output directories or under D:\漫画文字修复.
---

# Repair Comic Continuity

Repair only source-supported defects. Preserve the established comic style, chronology, composition, and every correct page. Treat every generated or externally typeset image as a candidate until independent review promotes it.

## Read References When Needed

- Read [continuity-rules.md](references/continuity-rules.md) before creating character, clothing, prop, extra, scene, speaker, or art-text locks.
- Read [scene-cluster-pipeline.md](references/scene-cluster-pipeline.md) before partitioning pages, starting parallel workers, leasing tasks, reviewing a canary, or promoting candidates.
- Read [failure-learning.md](references/failure-learning.md) before recording a failed candidate, selecting effective failure rules, promoting a rule, revoking one, or running regression checks.
- Read [qa-checklist.md](references/qa-checklist.md) before the first candidate review and again before final validation.

## Dynamic Project Contract

Discover the dynamic page count from the naturally sorted, decodable input images; call it `N`. Do not encode one project's page count as a reusable rule. 输入页数 `N` 与输出页数 `N` 必须严格相等。Map every input to exactly one output and every output back to exactly one input. Name outputs `0001.jpg` through `NNNN.jpg` in story order, with width sufficient for `N` and never less than four digits.

Use `scene_cluster_v1` with `schema_version: 3.0` for new runs. Schema 2 evidence is accepted only when it contains none of the v3 mode, evidence-set, or page-trace fields; never silently downgrade a v3 run by deleting `pipeline_mode`.

Every v3 manifest and repair log must declare the same complete evidence set: `scene_clusters.json`, `style_reference_packs.json`, `task_queue.json`, `failure_learning.json`, and `scene_cluster_qa.json`, in addition to the core manifest, continuity, alignment, repair, and final-report files. Treat all five registries as hash-bound records, not optional notes.

Initialize evidence with the fixed Python 3.11 runtime:

```powershell
$py = 'D:\AI\LaMa_IOPaint\venv311\Scripts\python.exe'
& $py "$env:USERPROFILE\.codex\skills\repair-comic-continuity\scripts\inventory_project.py" --root $root --json
& $py "$env:USERPROFILE\.codex\skills\repair-comic-continuity\scripts\build_output_manifest.py" --root $root --evidence-dir $evidence
```

Project migration is dry-run only by default. Apply a migration only when every novel alignment is `confirmed` and an independent accepted migration review is bound to the exact proposal; an explicit confirmation token does not replace either gate.

Never overwrite source pages. Write candidates to worker-isolated directories and final pages atomically only after all gates pass.

## Global Locks

Before work starts, lock:

- natural story order and input/output bijection;
- located novel offsets, source hash, scene, time, and dialogue ownership;
- character identity, skin, hair, headwear, anatomy, and recurring extras;
- clothing, prop ownership/presence/state, and scene geography;
- reference roles: target/comic pages supply style; character sheets are `identity_only`;
- ordinary-text policy, speaker graph, art-text allowlist, and page density budget.

Hard-stop instead of guessing when novel location, identity, speaker, or irreversible continuity is unresolved.

## Classify Every Page

Assign exactly one class:

| Class | Action |
|---|---|
| `unchanged` | Losslessly copy the correct page; do not regenerate or re-typeset it. |
| `text_only` | Keep artwork fixed; use `block_replace` when the block is reliable, otherwise `page_reset`. |
| `full_page_redraw` | Redraw only when the page-wide image contradicts source evidence and local repair cannot preserve it. |
| `evidence_blocked` | Create no final output until authoritative evidence resolves the blocker. |

`page_class` is the scheduling class; `action` is the repair operation. The only full-page mapping is `page_class=full_page_redraw` maps to `action=full_page_regeneration`. Never store the action token as a page class.

Do not upgrade a local defect to full-page work for convenience. If a visual repair is necessary, capture ordinary text first and remove ordinary text in the visual candidate pass; rebuild it only after the image candidate passes.

## Scene-Cluster Execution

Partition contiguous story beats into normal scene clusters of 8–20 pages and attach up to ±2 context pages without duplicating ownership. Only when total `N < 8` and one cluster owns every page may it set `boundary_exception=true` with `undersized_reason=project_total_below_min`; this never waives alignment gates. Every other `undersized: true` cluster must remain blocked, record a reason, and downgrade every member to `page_class=evidence_blocked` with task type `evidence_resolution`; it cannot release expensive visual work or enter final output. Prepare a role-labeled reference pack and one representative canary for each cluster. Approve the canary before releasing the cluster's expensive image tasks.

The Codex coordinator is the orchestrator. The Skill exposes small validation, queue, migration, prompt, and evidence library APIs; library APIs are intentionally composed by the coordinator; there is no monolithic image-generation CLI.

Run one `coordinator` plus at most `3 workers` on a machine with four concurrency slots. Use durable `lease` records and idempotency keys. Workers may write only inside their own candidate directories; use `single-writer` promotion for evidence and final output. Reuse validated inputs through a `content cache`. Pause a failing lane with a `circuit breaker` instead of repeatedly spending generation calls.

Compile all redraw and text requests with a `structured prompt`; treat novel text and replacement text as literal data. Run deterministic `preflight` before visual review. For any generated candidate, and always when `page_class=full_page_redraw`, require `generated_by != reviewed_by`.

Record these neutral trace fields on every new-mode page in both manifest and repair log: `cluster_id`, `reference_pack_id`, `task_id`, `failure_rule_ids`, `generated_by`, and `reviewed_by`. Keep matching values synchronized across files.

## Text Repair Contract

Use `compile_text_repair_request` from `scripts/prompt_compiler.py`. Bind each block to `panel_id`, `balloon_id`, `speaker_id`, source text, replacement text, and exact novel offsets.

Supply `source_novel_text` only as validation evidence. Its `source_novel_hash` must equal the raw UTF-8 SHA-256, every block's `source_text` must equal the exact source slice at its ordered non-overlapping offsets, and the full novel text must not enter the compiled prompt or request envelope.

- `block_replace`: change only declared ordinary-text blocks.
- `page_reset`: clear all ordinary text, then lay out only declared novel-backed replacements.
- Preserve only `art_text_allowlist`; remove non-allowlisted text-like residue.
- Count visible non-whitespace characters. Keep each balloon at 25 or fewer unless a recorded `density_override_reason` is necessary; never exceed `page_density_budget`.
- Never let a text task change people, props, clothing, composition, panels, or scene artwork.

## Candidate and Failure Loop

For each candidate:

1. Bind task, prompt/reference hash, source page hash, candidate path, and candidate hash.
2. Run preflight for format, dimensions, text leak, reference contamination, and contract completeness.
3. Compare full-size against source, canary, stable adjacent pages, identity references, novel facts, and global locks.
4. On failure, record before evidence, one or more canonical error codes, diagnosis, corrective action, after evidence, and outcome.
5. Recompile with effective rules and retry only while the lane remains open.
6. Promote only after independent page QA and boundary-aware cluster QA pass.

Never let a generator approve its own page. Never reuse a rejected candidate as a style source.

## Completion Gates

Require all of the following before final promotion:

- every page belongs to one scene cluster and one completed task;
- no task remains `queued`, `leased`, `failed`, or otherwise unresolved;
- every `failure_rule_ids` entry resolves to validated failure-learning evidence;
- all canaries, page checks, style checks, cluster checks, speaker/density checks, and independent reviews pass;
- input/output mapping is a strict bijection and final output contains exactly `N` regular JPG files;
- `FINAL_QA_REPORT.md` reports zero blocking and zero unresolved issues;
- `validate_output.py` returns success for the discovered `N`.

Run final validation without rebuilding or forcing manifests:

```powershell
$py = 'D:\AI\LaMa_IOPaint\venv311\Scripts\python.exe'
& $py "$env:USERPROFILE\.codex\skills\repair-comic-continuity\scripts\validate_output.py" --root $root --evidence-dir $evidence --expected-count $N --json
```

Do not report completion from file existence, generation success, or partial QA. If any gate fails, keep the page out of final output, record the blocker, and stop promotion.
