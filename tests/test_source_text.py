import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from source_text import decode_source_text  # noqa: E402


class SourceTextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_bytes(self, payload: bytes) -> Path:
        path = self.root / "小说.txt"
        path.write_bytes(payload)
        return path

    def test_decodes_strict_utf8_and_records_raw_and_decoded_hashes(self):
        text = "第一章\n测试剧情。"
        raw = text.encode("utf-8")

        decoded = decode_source_text(self.write_bytes(raw))

        self.assertEqual(text, decoded.text)
        self.assertEqual("utf-8", decoded.encoding)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), decoded.raw_sha256)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), decoded.decoded_sha256)

    def test_bom_uses_strict_utf8_sig_and_does_not_mutate_source(self):
        text = "带 BOM 的小说"
        raw = b"\xef\xbb\xbf" + text.encode("utf-8")
        path = self.write_bytes(raw)

        decoded = decode_source_text(path)

        self.assertEqual(text, decoded.text)
        self.assertEqual("utf-8", decoded.encoding)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), decoded.raw_sha256)
        self.assertEqual(
            hashlib.sha256(text.encode("utf-8")).hexdigest(), decoded.decoded_sha256
        )
        self.assertEqual(raw, path.read_bytes())

    def test_decodes_gb18030_and_hashes_the_decoded_utf8_text(self):
        text = "第八十章，正是闯的年纪！"
        raw = text.encode("gb18030")

        decoded = decode_source_text(self.write_bytes(raw))

        self.assertEqual(text, decoded.text)
        self.assertEqual("gb18030", decoded.encoding)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), decoded.raw_sha256)
        self.assertEqual(
            hashlib.sha256(text.encode("utf-8")).hexdigest(), decoded.decoded_sha256
        )

    def test_ascii_prefers_utf8_when_both_decoders_could_accept_it(self):
        decoded = decode_source_text(self.write_bytes(b"plain ascii"))

        self.assertEqual("utf-8", decoded.encoding)

    def test_invalid_bom_payload_is_not_reinterpreted_as_gb18030(self):
        with self.assertRaisesRegex(ValueError, "strict UTF-8 BOM"):
            decode_source_text(self.write_bytes(b"\xef\xbb\xbf\xff"))

    def test_blocks_when_neither_strict_decoder_accepts_the_bytes(self):
        with self.assertRaisesRegex(ValueError, "UTF-8 or GB18030"):
            decode_source_text(self.write_bytes(b"\xff"))

    def test_wraps_unreadable_source_as_a_value_error(self):
        with self.assertRaisesRegex(ValueError, "unable to read source text"):
            decode_source_text(self.root)


if __name__ == "__main__":
    unittest.main()
