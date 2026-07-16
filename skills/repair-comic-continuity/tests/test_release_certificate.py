import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module():
    path = SCRIPTS / "validate_release_certificate.py"
    if not path.is_file():
        raise AssertionError("signed release certificate validator is missing")
    spec = importlib.util.spec_from_file_location("release_certificate_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def copy_certified_tree(source: Path, destination: Path) -> None:
    manifest_path = source / "assets" / "release_evidence" / "runtime_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    paths = [row["path"] for row in manifest["files"]]
    paths.extend(
        [
            "assets/release_certificate.json",
            "assets/release_public_key.json",
            "assets/release_evidence/runtime_manifest.json",
        ]
    )
    for relative in paths:
        target = destination / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / Path(relative), target)


class SignedReleaseCertificateTests(unittest.TestCase):
    def test_packaged_v5_certificate_verifies_benchmark_and_control_plane(self):
        module = load_module()
        result = module.validate_release_certificate(ROOT)
        self.assertEqual(result["status"], "certificate_valid")
        self.assertEqual(result["pipeline_id"], "continuity_v5_unified")
        self.assertEqual(result["missed_confirmed_defects"], 0)
        self.assertEqual(result["false_positive_defects"], 0)
        self.assertEqual(result["correct_page_false_positive_count"], 0)

    def test_tampered_benchmark_artifact_is_rejected(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy_certified_tree(ROOT, root)
            golden = root / "assets" / "release_evidence" / "golden_text_detection_v2.json"
            golden.write_text(golden.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "golden|hash|manifest"):
                module.validate_release_certificate(root)

    def test_tampered_signed_payload_is_rejected(self):
        module = load_module()
        certificate = json.loads(
            (ROOT / "assets" / "release_certificate.json").read_text(encoding="utf-8")
        )
        public_key = json.loads(
            (ROOT / "assets" / "release_public_key.json").read_text(encoding="utf-8")
        )
        certificate["benchmark"]["missed_confirmed_defects"] = 99
        with self.assertRaisesRegex(ValueError, "signature"):
            module.verify_certificate_signature(certificate, public_key)

    def test_certificate_survives_git_line_ending_conversion(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            copy_certified_tree(ROOT, root)
            for path in root.rglob("*"):
                if path.is_file():
                    data = path.read_bytes().replace(b"\r\n", b"\n")
                    path.write_bytes(data.replace(b"\n", b"\r\n"))
            result = module.validate_release_certificate(root)
            self.assertEqual(result["status"], "certificate_valid")


if __name__ == "__main__":
    unittest.main()
