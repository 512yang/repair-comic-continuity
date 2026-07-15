# Continuity V4 acceptance evidence

- Acceptance timestamp: `2026-07-15T14:47:44+08:00`
- Verified commit: `c52025cc6f91d642823e7d663c55c538b6f318dd`
- Branch: `agent/continuity-v4-implementation`
- Scope: Skill code, synthetic fixtures, documentation, and read-only validators only. No project comic image or copyrighted novel content was read, modified, generated, or committed during this acceptance run.

## Full regression

Command:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Result: exit code `0`; `495` tests passed in `32.607s`. Two optional real-project read-only tests were skipped because `REPAIR_COMIC_REAL_PROJECT_ROOT` was not set. No V4 gate test was skipped.

The first full run exposed five missing reference-pack state terms in public documentation and exited `1`. The documentation contract was restored, its original failing test passed, and the complete command above was rerun from the start to the successful result recorded here.

## Skill structure

The first invocation inherited Windows GBK and exited `1` with `UnicodeDecodeError` while reading the UTF-8 Skill document:

```powershell
python "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .
```

The same validator was rerun in Python UTF-8 mode:

```powershell
python -X utf8 "$env:USERPROFILE\.codex\skills\.system\skill-creator\scripts\quick_validate.py" .
```

Result: exit code `0`; `Skill is valid!`

## Failed-run integrity fixture

Command:

```powershell
python -m unittest discover -s tests -p "test_evidence_integrity.py" -v
```

Result: exit code `0`; `5` tests passed. The known failed run is rejected for:

- `OUTPUT_NAME_SET_MISMATCH`
- `ALIGNMENT_UNCONFIRMED`
- `PAGE_QA_PENDING`
- `CLUSTER_QA_MISSING`
- `STABLE_STYLE_ANCHOR_MISSING`
- `REFERENCE_CAST_COVERAGE_UNPROVEN`
- `FINAL_STATUS_NOT_PASSED`

The separate forged full-size case is rejected as `FULL_SIZE_ARTIFACT_MISSING`. V4 mutation tests also reject repeated alignment, invalid time order, self-review, early cluster QA, pending registries, broken event chains, and renamed output.

## Audit-only acceptance

Command:

```powershell
python -m unittest discover -s tests -p "test_validate_audit.py" -v
```

Result: exit code `0`; `3` tests passed. The complete synthetic audit returns:

```json
{
  "status": "audit_passed",
  "candidate_count": 0,
  "promoted_output_count": 0
}
```

The audit validator leaves the synthetic tree byte-for-byte unchanged and rejects either a candidate image or a final output image.
