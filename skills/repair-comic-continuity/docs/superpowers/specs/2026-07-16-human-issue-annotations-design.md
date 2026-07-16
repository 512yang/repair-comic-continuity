# Human issue annotations and evidence-gated learning

## Goal

Extend `human_visual_auto_text` so a user can identify not only the visual-problem pages, but also the exact regions, affected subjects, observed error, and required correction. Treat those instructions as immutable repair evidence for the current run and as candidates for controlled future learning.

## Annotation contract

Store user-confirmed annotations at `evidence/human_issue_annotations.json`:

```json
{
  "version": 1,
  "status": "confirmed",
  "mode": "human_visual_auto_text",
  "confirmed_by": "user",
  "confirmed_at": "2026-07-16T10:00:00+08:00",
  "annotations": [
    {
      "annotation_id": "page-249-beard",
      "page": {"path": "249.jpg", "sha256": "<sealed-source-sha256>"},
      "regions": [
        {
          "region_id": "lower-face",
          "bbox_norm": [0.31, 0.18, 0.54, 0.41],
          "description": "lower face and beard"
        }
      ],
      "targets": ["character:邓正虎"],
      "defect_codes": ["identity_drift"],
      "observed_state": "beard is missing",
      "required_state": "restore the beard from the identity reference",
      "instruction": "change only the beard and related facial detail"
    }
  ],
  "learning_policy": "evidence_gated",
  "persistence_policy": "page_cluster_project_skill_candidate"
}
```

Paths are input-relative, unique, hash-current, and members of the confirmed visual selection. Normalized boxes use `[left, top, right, bottom]`, remain within `[0, 1]`, and have positive area. Annotation, region, and target identifiers are nonempty and unique where applicable. Defect codes reuse the existing failure-learning taxonomy.

## Audit binding

The selection manifest remains the only authority for visual redraw scope. The annotation manifest is optional for backward compatibility, but if present it requires `human_visual_auto_text` and every annotation page must be selected. Every annotated page must contain exactly one `human_issue_annotation` page-audit record whose `user_confirmed_issue_annotations` artifact binds the annotation manifest path and SHA-256. This prevents a worker from accepting the file while ignoring its instructions.

Selected pages still require full-resolution continuity evidence. Unselected pages remain prohibited from `full_page_redraw`. Text audit and `page_reset_preserve_style` still run on every input page.

## Learning bridge

Convert each validated `(annotation, target)` pair into one immutable observed failure in the durable failure store. Bind the source page path/hash, cluster, defect codes, observed state, required state, instruction, region geometry, prompt/reference hash, user identity, and confirmation timestamp.

An annotation never becomes an effective learned rule by itself. After repair, record different before/after hashes and independent outcome review. Promotion still requires all four existing gates: positive regression, clean control, varied-case evidence, and independent review. Promote without skipping `page -> cluster -> project -> skill_candidate`; only a reviewed Skill update and fresh signed release can make a Skill candidate permanent across installations.

## Failure handling

Reject unknown or unselected pages, duplicate IDs, unsafe paths, stale hashes, invalid timestamps, empty evidence text, unknown defect codes, malformed normalized boxes, audit artifact mismatches, and partial ingestion. Learning ingestion is transactional: if any annotation fails, the failure store remains byte-for-byte unchanged.

## Compatibility and output

Runs without `human_issue_annotations.json` keep V5.3 behavior. The feature does not change exact input/output bijection, text policy, candidate review, or promotion rules. It adds a deterministic annotation intake path and a controlled bridge into the existing learning engine.
