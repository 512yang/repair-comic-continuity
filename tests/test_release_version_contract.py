import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionContractTests(unittest.TestCase):
    def test_one_module_separates_v5_orchestration_from_v4_evidence_schema(self):
        version_file = ROOT / "scripts" / "pipeline_version.py"
        self.assertTrue(version_file.is_file(), "pipeline_version.py must be the version source of truth")
        version = version_file.read_text(encoding="utf-8")
        self.assertIn('ORCHESTRATION_PIPELINE_ID = "continuity_v5_unified"', version)
        self.assertIn('EVIDENCE_PIPELINE_ID = "continuity_v4"', version)
        self.assertIn('EVIDENCE_SCHEMA_VERSION = "4.0"', version)

        builder = (ROOT / "scripts" / "build_output_manifest.py").read_text(encoding="utf-8")
        validator = (ROOT / "scripts" / "validate_output.py").read_text(encoding="utf-8")
        self.assertIn("from pipeline_version import EVIDENCE_PIPELINE_ID, EVIDENCE_SCHEMA_VERSION", builder)
        self.assertIn("PIPELINE_MODE = EVIDENCE_PIPELINE_ID", builder)
        self.assertIn("SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION", builder)
        self.assertIn("from pipeline_version import EVIDENCE_PIPELINE_ID, EVIDENCE_SCHEMA_VERSION", validator)
        self.assertIn('run.get("pipeline_mode") != EVIDENCE_PIPELINE_ID', validator)
        self.assertIn('run.get("schema_version") != EVIDENCE_SCHEMA_VERSION', validator)

        metadata = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("continuity_v5_unified", metadata)
        self.assertIn("continuity_v5_unified", skill)


if __name__ == "__main__":
    unittest.main()
