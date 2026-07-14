# Failure Learning

Read this reference after a candidate fails, before retrying, and before promoting or revoking a reusable rule.

## Canonical Error Codes

Use one or more of exactly these eight codes:

| Code | Meaning |
|---|---|
| `style_drift` | Line, color, texture, simplification, or density left the approved comic style |
| `identity_drift` | Face, skin, hair, headwear, or named identity changed |
| `costume_prop_drift` | Clothing, prop ownership, presence, hand, or state contradicted continuity |
| `composition_drift` | Panels, camera, reading order, or correct composition changed |
| `anatomy_error` | Limb, hand, pose, contact, proportion, or gaze is invalid |
| `scene_drift` | Location, axis, fixture, time, weather, or crowd state changed |
| `text_leak` | Ordinary text, residue, watermark, or non-allowlisted art text remains |
| `over_rendering` | The candidate upgraded detail/rendering beyond the established comic |

Do not encode a diagnosis as free-form pseudo-code. Select codes first, then write a source-backed diagnosis and corrective action.

## Evidence Record

For every failed candidate record immutable `before` evidence: page/cluster/character, codes, prompt/reference hash, candidate path/hash, diagnosis, and corrective action. After retry and independent review, append `after` evidence and an `outcome` stating pass/fail, reviewer, timestamp, and resulting candidate hash. Never overwrite the before record.

Rules must cite their source failure IDs and source outcome IDs. A rule without a successful corrective outcome may remain page-local guidance but cannot be promoted.

## Promotion Ladder

Promote conservatively through:

`page → cluster → project → skill_candidate`

- `page`: applies only to the failed page and its exact context.
- `cluster`: require repeated matching evidence inside one cluster.
- `project`: require the same semantic failure and successful correction in at least two clusters (`two clusters`), without contradicting global locks.
- `skill_candidate`: export only as a reviewed proposal; never mutate the installed Skill automatically.

Deduplicate by semantic key and canonical signature. Preserve all source rule IDs when merging. Higher scope may not broaden character, scene, or source facts beyond its evidence.

## Revoke, Circuit Breaker, and Regression

Use `revoke` when a rule causes a new contradiction, fails to reproduce, leaks across reference roles, or is superseded by stronger evidence. Record reviewer, reason, time, and the exact rule ID; keep the audit event append-only.

Open a lane `circuit breaker` after repeated matching failures for the same cluster/task or prompt/reference hash. While open, do not retry generation. Diagnose the canary/reference/prompt contract, revise evidence, and explicitly reopen the lane with review history.

Before accepting a project- or skill-level promotion, run the regression corpus containing prior style, identity, costume/prop, composition, anatomy, scene, text-leak, and over-rendering failures. A regression failure blocks promotion and may trigger revoke. Record corpus version, selected rules, pass/fail result, and reviewer.
