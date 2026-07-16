# Failure learning

Record a failure only from real before and after evidence. Bind the page, cluster, task type, failure family, source candidate hash, failed candidate hash, prompt/reference hash, diagnosis, corrective action, reviewer, and timestamps.

Canonical families include `style_drift`, `identity_drift`, `costume_prop_drift`, `composition_drift`, `anatomy_error`, `scene_drift`, `text_leak`, and `over_rendering`. Keep facial-hair and malformed-glyph details in the diagnosis and affected entities.

After two failures in the same `(cluster_id, task_type, failure_family)`, pause that lane. Intervening failure families do not erase the count. Queued tasks remain unclaimable until an explicit reviewed diagnosis reopens the lane.

A rule is not effective merely because one retry looked better. Promotion requires four passed, hash-bound artifacts:

1. positive regression on the failed case;
2. clean control proving an already-correct page was not damaged;
3. variation proving the correction generalizes;
4. independent review by an actor other than the generator.

Promote through page, cluster, project, and skill-candidate scope without skipping levels. Preserve version history, source evidence, supersession, revocation, and dependent-rule rollback. A revoked or rejected rule cannot enter prompts. Rejected candidates never become reference images.

## Human annotation intake

Validate `human_issue_annotations.json` before learning intake. Bind each user-confirmed region, affected target, observed state, required correction, source page hash, prompt/reference hash, reviewer, timestamp, and scene cluster. Ingest each `(annotation, target)` pair transactionally through `scripts/human_issue_learning.py`; if any pair fails, leave the durable failure store unchanged.

An annotation never becomes an effective learned rule by itself. It creates only an observed failure. After repair, bind a different after-candidate hash and an independent outcome review. Then require positive regression, clean control, variation, and independent review before promotion.

Promote only through `page -> cluster -> project -> skill_candidate`. Do not use a page rule on another page, a cluster rule in another cluster, or a project rule in another project. A `skill_candidate` remains non-permanent until a reviewer updates the Skill, runs the full regression suite, issues a fresh signed release, and preserves the source evidence. Revoke failed rules and dependent rules without deleting their audit history.

Use `ingest_annotations_transactionally` to update `failure_learning.json`; a partial annotation batch must never reach disk. After independent candidate review, use `record_outcomes_transactionally` and complete the `learning_recorded` controller receipt. Do not advance the run from an in-memory result.

At the next run, call `load_effective_rules_for_page` for the current page, cluster, and annotated targets. It binds the durable registry revision and independent-review artifact to the prompt, maps approved failure families to controlled actions, and includes the reviewed corrective action. Apply `page > cluster > project > skill_candidate`. If two same-scope rules affect the same target and failure code but prescribe different corrections, report `conflicting effective rules` and stop until independent review revokes or supersedes one rule.

Use optional `trait_codes` for searchable details without replacing canonical failure families: `skin_tone`, `facial_hair`, `hair_style`, `hair_color`, `headwear`, `clothing`, `prop`, `body_build`, `face_shape`, `anatomy`, `scene`, `text_glyph`, `text_style`, `balloon_geometry`, and `sound_effect`.

## Output revision feedback

Store every output-review round as a new immutable
`evidence/human_revision_feedback/attempt-<N>.json`; never overwrite an earlier
attempt. Validate it with `scripts/validate_human_revision_feedback.py`. Every
record must bind the sealed source page, the exact reviewed candidate hash, its
attempt number, one of the five review origins, the original user note, and all
reviewed regions.

Ingest accepted feedback through `ingest_human_revision_feedback` or the
controller's transactional wrapper. The result is observed failure evidence
only. It must not become an effective prompt rule until the normal positive
regression, clean control, variation, and independent-review gates pass. A
revision run with feedback must complete the `revision_feedback_ingested`
receipt before releasing any revision task.
