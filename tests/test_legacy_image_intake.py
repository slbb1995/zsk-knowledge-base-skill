from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills"))

from shared.contracts import BINDING_SCHEMA, ROOT_KEYS, Binding
from shared.converter_probe import MARKER, sample_document
from shared.fake_adapter import FakeAdapter
from shared.image_input import prepare_image
from shared.legacy_doc import LegacyConversion, LegacyConverterUnavailable, convert_legacy_doc, validate_legacy_word
from shared.markdown_converter import ConversionFailed, ConverterUnavailable, MarkdownConversion
from shared.obsidian_adapter import ObsidianAdapter
from shared.ocr_provider import AutoOcrProvider, OcrResult
from shared.stage5_intake import IntakeRequest, Stage5Intake
from shared.templates import TEMPLATE_VERSION

TASK = "01a124d0-f630-72b1-899f-6875e5dfecb5"
ASSETS = ROOT / "skills/shared/assets/intake-probe"


def binding(path="/private/tmp/zsk-intake-test"):
    return Binding(BINDING_SCHEMA, "CLT-1234567890ABCD", "验收", "知识库", "company",
                   "obsidian", path, {key: f"root:{key}" for key in ROOT_KEYS}, TEMPLATE_VERSION)


class Ocr:
    name = "fixture-local"
    def __init__(self, text="MODEL HYD100\nLOAD 10000 KG"):
        self.text = text
    def recognize(self, image):
        return OcrResult(self.text, 0.99, self.name)


class LegacyImageIntakeTests(unittest.TestCase):
    def setUp(self):
        self.binding = binding()
        self.adapter = FakeAdapter()
        self.adapter.resolve_binding(self.binding)
        self.adapter.create_skeleton(self.binding)

    def request(self, suffix="png", payload=None):
        if payload is None:
            payload = (ASSETS / f"image.{suffix}").read_bytes()
        return IntakeRequest(TASK, self.binding, f"资料.{suffix}", payload, "资料",
                             original_retention_approved=True)

    def intake(self, providers=None):
        return Stage5Intake(self.adapter, AutoOcrProvider(providers or (Ocr(), Ocr(), Ocr())))

    def test_legacy_preserves_original_identity_and_conversion_relationship(self):
        payload = (ASSETS / "legacy.doc").read_bytes()
        derived = sample_document("docx")
        provenance = LegacyConversion(derived, "fixture-local", hashlib.sha256(derived).hexdigest())
        with mock.patch("shared.stage5_intake.convert_legacy_doc", return_value=provenance), \
             mock.patch("shared.stage5_intake.convert_to_markdown", return_value=MarkdownConversion(MARKER, "markitdown", "fixture")) as convert:
            result = self.intake().execute(self.request("doc", payload))
        self.assertEqual(result.status, "registered")
        self.assertEqual(result.source_id, "SRC-" + hashlib.sha256(payload).hexdigest()[:24])
        self.assertEqual(result.record.original_sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(result.record.content_kind, "document")
        convert.assert_called_once_with(derived, ".docx")
        readable = self.adapter._objects[f"source:{result.source_id}:readable"]["payload"].decode()
        self.assertIn(provenance.sha256, readable)
        self.assertIn('"intermediate_format": "docx"', readable)
        self.assertEqual(self.adapter._objects[f"source:{result.source_id}:original"]["payload"], payload)

    def test_renaming_text_to_doc_is_not_accepted(self):
        result = self.intake().execute(self.request("doc", b"not a Word binary"))
        self.assertEqual(result.code, "conversion_failed")
        self.assertIsNone(result.record)

    def test_ole_spreadsheet_is_not_mistaken_for_word(self):
        fake = mock.MagicMock()
        fake.__enter__.return_value.exists.return_value = False
        with mock.patch("olefile.OleFileIO", return_value=fake):
            with self.assertRaises(ConversionFailed):
                validate_legacy_word(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 64)

    def test_missing_legacy_backend_has_actionable_distinct_reason(self):
        with mock.patch("shared.legacy_doc.legacy_backend", return_value=None):
            result = self.intake().execute(self.request("doc", (ASSETS / "legacy.doc").read_bytes()))
        self.assertEqual(result.code, "legacy_conversion_unavailable")
        self.assertIsNone(result.record)

    def test_missing_converter_is_not_an_unsupported_format(self):
        with mock.patch("shared.stage5_intake.convert_to_markdown", side_effect=ConverterUnavailable("missing")):
            result = self.intake().execute(self.request("docx", b"fixture"))
        self.assertEqual(result.code, "dependency_missing")
        self.assertIsNone(result.record)

    def test_real_png_and_jpeg_keep_image_and_verified_text(self):
        for suffix in ("png", "jpg"):
            with self.subTest(suffix=suffix):
                result = self.intake().execute(self.request(suffix))
                self.assertEqual(result.status, "registered")
                self.assertEqual(result.record.content_kind, "image")
                self.assertEqual(result.record.page_count, 1)
                self.assertEqual(result.record.page_artifacts[0].width_px, 1500)
                self.assertEqual(result.record.page_text_evidence[0].review_status, "auto_verified")
                readable = self.adapter._objects[f"source:{result.source_id}:readable"]["payload"].decode()
                self.assertIn("LOAD 10000 KG", readable)
                self.assertIn("![[页面证据/第001页.png]]", readable)

    def test_one_pass_and_disagreeing_numbers_do_not_write_any_objects(self):
        for providers in ((Ocr(),), (Ocr("MODEL HYD100 LOAD 10000 KG"), Ocr("MODEL HYD100 LOAD 10001 KG"))):
            with self.subTest(providers=len(providers)):
                provider = providers[0] if len(providers) == 1 else AutoOcrProvider(providers)
                before = dict(self.adapter._objects)
                result = Stage5Intake(self.adapter, provider).execute(self.request())
                self.assertEqual(result.code, "file_quality_insufficient")
                self.assertEqual(self.adapter._objects, before)

    def test_corrupt_and_mismatched_images_have_no_source_writes(self):
        for payload in (b"bad PNG", (ASSETS / "image.jpg").read_bytes()):
            with self.subTest(payload_size=len(payload)):
                result = self.intake().execute(self.request("png", payload))
                self.assertEqual(result.code, "source_unreadable")
                self.assertNotIn(f"source:{result.source_id}:original", self.adapter._objects)

    def test_ocr_sensitive_text_is_redacted(self):
        result = self.intake((Ocr("PHONE 13812345678"), Ocr("PHONE 13812345678"))).execute(self.request())
        self.assertEqual(result.record.privacy_status, "redacted")
        readable = self.adapter._objects[f"source:{result.source_id}:readable"]["payload"].decode()
        self.assertNotIn("13812345678", readable)
        self.assertNotIn("13812345678", result.record.page_text_evidence[0].ocr_text)

    def test_original_retention_preference_is_respected(self):
        request = replace(self.request(), original_retention_approved=False)
        with mock.patch("shared.stage5_intake.prepare_image") as prepare:
            result = self.intake().execute(request)
        self.assertEqual(result.code, "privacy_approval_required")
        prepare.assert_not_called()

    def test_batch_continues_after_unsupported_file(self):
        good = self.request("txt", b"Plain source text")
        bad = self.request("mp3", b"audio")
        results = self.intake().execute_batch((bad, good, self.request()))
        self.assertEqual([item.code for item in results], ["format_unsupported", None, None])
        self.assertEqual([item.status for item in results], ["exception", "registered", "registered"])

    def test_mixed_binding_batch_stops_before_any_writes(self):
        before = dict(self.adapter._objects)
        with self.assertRaises(ValueError):
            self.intake().execute_batch((self.request(), replace(self.request(), binding=replace(self.binding, client_id="CLT-OTHER"))))
        self.assertEqual(self.adapter._objects, before)

    def test_fresh_obsidian_task_reuses_image_without_running_ocr(self):
        with tempfile.TemporaryDirectory() as folder:
            active = binding(str(Path(folder).resolve()))
            adapter = ObsidianAdapter()
            adapter.resolve_binding(active)
            adapter.create_skeleton(active)
            request = replace(self.request(), binding=active)
            first = Stage5Intake(adapter, AutoOcrProvider((Ocr(), Ocr()))).execute(request)
            self.assertEqual(first.status, "registered")
            snapshot = {p.relative_to(folder).as_posix(): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()}
            fresh = ObsidianAdapter()
            fresh.resolve_binding(active)
            with mock.patch("shared.stage5_intake.prepare_image", side_effect=AssertionError("should reuse")):
                second = Stage5Intake(fresh).execute(request)
            self.assertEqual(second.status, "reused")
            self.assertEqual(second.record.content_kind, "image")
            self.assertEqual(snapshot, {p.relative_to(folder).as_posix(): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()})

    def test_windows_converter_passes_paths_as_arguments_and_validates_docx(self):
        payload = (ASSETS / "legacy.doc").read_bytes()
        def run(command, **kwargs):
            script_index = command.index("-File") + 1
            Path(command[script_index + 2]).write_bytes(sample_document("docx"))
            self.assertFalse(kwargs["shell"])
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with mock.patch("shared.legacy_doc.legacy_backend", return_value=("word-com", "powershell.exe")), \
             mock.patch("shared.legacy_doc.subprocess.run", side_effect=run):
            result = convert_legacy_doc(payload)
        self.assertEqual(result.engine, "word-com")
        self.assertEqual(result.sha256, hashlib.sha256(result.payload).hexdigest())

    def test_native_converter_failure_does_not_silently_change_backend(self):
        with mock.patch("shared.legacy_doc.legacy_backend", return_value=("libreoffice", "soffice")), \
             mock.patch("shared.legacy_doc.subprocess.run", return_value=subprocess.CompletedProcess([], 1, b"", b"failed")) as run:
            with self.assertRaises(ConversionFailed):
                convert_legacy_doc((ASSETS / "legacy.doc").read_bytes())
        self.assertEqual(run.call_count, 1)

    def test_windows_word_in_use_returns_unavailable(self):
        with mock.patch("shared.legacy_doc.legacy_backend", return_value=("word-com", "powershell.exe")), \
             mock.patch("shared.legacy_doc.subprocess.run", return_value=subprocess.CompletedProcess([], 20, b"", b"")):
            with self.assertRaises(LegacyConverterUnavailable):
                convert_legacy_doc((ASSETS / "legacy.doc").read_bytes())

    def test_converter_output_cannot_be_a_renamed_non_docx(self):
        def run(command, **kwargs):
            out = Path(command[command.index("--outdir") + 1])
            (out / "source.docx").write_bytes(b"not a zip")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        with mock.patch("shared.legacy_doc.legacy_backend", return_value=("libreoffice", "soffice")), \
             mock.patch("shared.legacy_doc.subprocess.run", side_effect=run):
            with self.assertRaises(ConversionFailed):
                convert_legacy_doc((ASSETS / "legacy.doc").read_bytes())


if __name__ == "__main__":
    unittest.main()
