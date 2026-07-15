"""Single source of truth for orchestration and evidence contract versions."""

ORCHESTRATION_PIPELINE_ID = "continuity_v5_unified"
EVIDENCE_PIPELINE_ID = "continuity_v4"
EVIDENCE_SCHEMA_VERSION = "4.0"
RELEASE_CERTIFICATE_ID = "continuity-v5.1-signed-20260715"


def version_contract() -> dict[str, str]:
    return {
        "orchestration_pipeline_id": ORCHESTRATION_PIPELINE_ID,
        "evidence_pipeline_id": EVIDENCE_PIPELINE_ID,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
        "release_certificate_id": RELEASE_CERTIFICATE_ID,
    }
