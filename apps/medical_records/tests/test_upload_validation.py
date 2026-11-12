"""
REGRESSION — NEW-18: magic-byte validation was bypassable.

Three separate holes in `validate_upload`:

1. **RIFF is not WebP.** The webp signature was `RIFF` at offset 0, which is the
   generic RIFF container header shared by WAV and AVI. A `.wav` renamed to
   `.webp` passed validation and was stored and served from our own origin.
   WebP additionally carries `WEBP` at offset 8.

2. **Text formats were unchecked.** Any extension outside `_MAGIC` — csv, json,
   txt — skipped content validation entirely, so arbitrary binary could be
   stored under a `.csv` name. There is no signature for text, but NUL bytes are
   a cheap structural test: a text document has none, a binary payload almost
   always does.

3. **A file object without `.size` skipped the size limit entirely.** The check
   read `getattr(file_obj, 'size', None)` and, finding nothing, simply moved on.
   An unbounded upload is the exact thing the limit exists to prevent, so the
   size is now measured rather than assumed.

These tests use synthetic bytes only — no patient data, no real documents.
"""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.medical_records.services import (
    IMAGE_EXTS, validate_image_upload, validate_upload,
)

PDF = b'%PDF-1.4\n1 0 obj\n'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 28
GIF = b'GIF89a' + b'\x00' * 26
# RIFF <size> WEBP — the real thing needs both anchors.
WEBP = b'RIFF' + b'\x24\x00\x00\x00' + b'WEBP' + b'VP8 ' + b'\x00' * 16
# RIFF <size> WAVE — same container family, different format.
WAV = b'RIFF' + b'\x24\x00\x00\x00' + b'WAVEfmt ' + b'\x00' * 16

ALL_DOC_EXTS = ['pdf', 'xml', 'csv', 'json', 'txt', 'tsv', 'xlsx']


def _upload(name, content):
    return SimpleUploadedFile(name, content)


class MagicByteTests(TestCase):

    def test_genuine_formats_are_accepted(self):
        for name, content, exts in [
            ('report.pdf', PDF, ['pdf']),
            ('scan.png', PNG, IMAGE_EXTS),
            ('scan.jpg', JPG, IMAGE_EXTS),
            ('scan.gif', GIF, IMAGE_EXTS),
            ('scan.webp', WEBP, IMAGE_EXTS),
        ]:
            with self.subTest(name=name):
                ok, payload = validate_upload(_upload(name, content), exts)
                self.assertTrue(ok, payload)

    def test_a_renamed_wav_is_not_a_webp(self):
        """ACCEPTANCE — NEW-18(1). RIFF alone used to be enough."""
        ok, message = validate_upload(_upload('payload.webp', WAV), IMAGE_EXTS)
        self.assertFalse(ok)
        self.assertIn('does not match its declared type', message)

    def test_a_renamed_avi_is_not_a_webp(self):
        avi = b'RIFF' + b'\x24\x00\x00\x00' + b'AVI LIST' + b'\x00' * 16
        ok, _ = validate_upload(_upload('clip.webp', avi), IMAGE_EXTS)
        self.assertFalse(ok)

    def test_webp_needs_the_riff_anchor_too(self):
        """WEBP at offset 8 without RIFF at 0 is not a WebP either."""
        forged = b'XXXX' + b'\x24\x00\x00\x00' + b'WEBP' + b'\x00' * 16
        ok, _ = validate_upload(_upload('x.webp', forged), IMAGE_EXTS)
        self.assertFalse(ok)

    def test_an_executable_renamed_to_pdf_is_rejected(self):
        ok, message = validate_upload(_upload('invoice.pdf', b'MZ\x90\x00' + b'\x00' * 28), ['pdf'])
        self.assertFalse(ok)
        self.assertIn('.pdf', message)

    def test_a_php_payload_renamed_to_png_is_rejected(self):
        ok, _ = validate_upload(_upload('avatar.png', b'<?php system($_GET[0]); ?>'), IMAGE_EXTS)
        self.assertFalse(ok)

    def test_svg_is_not_an_accepted_image_extension(self):
        """SVG is XML that can carry <script>; served first-party it would run."""
        self.assertNotIn('svg', IMAGE_EXTS)
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        ok, message = validate_image_upload(_upload('logo.svg', svg))
        self.assertFalse(ok)
        self.assertIn('not allowed', message)

    def test_xml_is_accepted_with_and_without_a_bom(self):
        bare = b'<?xml version="1.0"?><ClinicalDocument/>'
        bom = b'\xef\xbb\xbf<?xml version="1.0"?><ClinicalDocument/>'
        for label, content in [('bare', bare), ('bom', bom)]:
            with self.subTest(label):
                ok, payload = validate_upload(_upload('kanta.xml', content), ['xml'])
                self.assertTrue(ok, payload)

    def test_html_renamed_to_xml_is_rejected(self):
        ok, _ = validate_upload(_upload('doc.xml', b'<html><body>hi</body></html>'), ['xml'])
        self.assertFalse(ok)

    def test_a_non_zip_is_not_an_xlsx(self):
        ok, _ = validate_upload(_upload('labs.xlsx', b'not a zip at all, just text'), ['xlsx'])
        self.assertFalse(ok)

    def test_the_stream_is_rewound_for_the_caller(self):
        """Validation must not consume the bytes the caller then needs to parse."""
        upload = _upload('report.pdf', PDF)
        ok, _ = validate_upload(upload, ['pdf'])
        self.assertTrue(ok)
        self.assertEqual(upload.read(5), b'%PDF-')


class TextFormatTests(TestCase):
    """NEW-18(2) — formats with no signature were waved through unexamined."""

    def test_real_text_formats_are_accepted(self):
        for name, content in [
            ('labs.csv', b'analyte,value,unit\nGlucose,5.2,mmol/L\n'),
            ('data.json', b'{"analyte": "Glucose", "value": 5.2}'),
            ('notes.txt', b'Follow-up scheduled.\n'),
            ('labs.tsv', b'analyte\tvalue\nGlucose\t5.2\n'),
        ]:
            with self.subTest(name=name):
                ok, payload = validate_upload(_upload(name, content), ALL_DOC_EXTS)
                self.assertTrue(ok, payload)

    def test_binary_renamed_to_csv_is_rejected(self):
        """ACCEPTANCE — NEW-18(2). Previously stored without any check."""
        ok, message = validate_upload(_upload('labs.csv', PNG), ALL_DOC_EXTS)
        self.assertFalse(ok)
        self.assertIn('binary', message.lower())

    def test_an_executable_renamed_to_txt_is_rejected(self):
        ok, _ = validate_upload(_upload('readme.txt', b'MZ\x90\x00\x03\x00\x00\x00\x04\x00'), ALL_DOC_EXTS)
        self.assertFalse(ok)

    def test_utf8_clinical_text_survives(self):
        """Finnish and Persian text is not binary. A NUL check must not reject it."""
        content = 'Verenkuva\nهموگلوبین: ۱۴٫۲ g/dL\nB-Hb 142 g/l\n'.encode('utf-8')
        ok, payload = validate_upload(_upload('kertomus.txt', content), ALL_DOC_EXTS)
        self.assertTrue(ok, payload)

    def test_utf16_text_is_rejected_and_that_is_the_intended_trade_off(self):
        """
        UTF-16 text legitimately contains NUL bytes, so it is refused. That is a
        deliberate false positive: the app's ingestion paths decode UTF-8, and
        refusing a rare encoding is safer than accepting arbitrary binary.
        """
        ok, _ = validate_upload(_upload('notes.txt', 'hello'.encode('utf-16')), ALL_DOC_EXTS)
        self.assertFalse(ok)


class SizeLimitTests(TestCase):
    """NEW-18(3) — the limit was skippable by omitting `.size`."""

    @override_settings(MAX_UPLOAD_BYTES=1024)
    def test_an_oversized_upload_is_rejected(self):
        ok, message = validate_upload(_upload('big.pdf', PDF + b'\x00' * 2048), ['pdf'])
        self.assertFalse(ok)
        self.assertIn('too large', message)

    @override_settings(MAX_UPLOAD_BYTES=1024)
    def test_a_file_object_without_size_is_measured_not_waved_through(self):
        """ACCEPTANCE — NEW-18(3). `getattr(f, 'size', None)` returning None
        used to skip the limit entirely."""
        stream = io.BytesIO(PDF + b'\x00' * 4096)
        stream.name = 'big.pdf'
        self.assertFalse(hasattr(stream, 'size'))

        ok, message = validate_upload(stream, ['pdf'])
        self.assertFalse(ok)
        self.assertIn('too large', message)

    @override_settings(MAX_UPLOAD_BYTES=1024 * 1024)
    def test_a_sizeless_stream_within_the_limit_still_validates(self):
        stream = io.BytesIO(PDF)
        stream.name = 'small.pdf'
        ok, payload = validate_upload(stream, ['pdf'])
        self.assertTrue(ok, payload)
        # And it was rewound after being measured.
        self.assertEqual(stream.read(5), b'%PDF-')

    def test_an_unmeasurable_stream_is_refused_rather_than_assumed_small(self):
        class Unmeasurable(io.BytesIO):
            name = 'mystery.pdf'

            def seek(self, *args, **kwargs):
                raise OSError('stream is not seekable')

        ok, message = validate_upload(Unmeasurable(PDF), ['pdf'])
        self.assertFalse(ok)
        self.assertIn('size', message.lower())


class FilenameTests(TestCase):

    def test_path_traversal_is_stripped_from_the_stored_name(self):
        for hostile in ['../../../etc/passwd.pdf', r'..\..\windows\system32\x.pdf']:
            with self.subTest(hostile):
                ok, safe_name = validate_upload(_upload(hostile, PDF), ['pdf'])
                self.assertTrue(ok, safe_name)
                self.assertNotIn('/', safe_name)
                self.assertNotIn('\\', safe_name)
                self.assertNotIn('..', safe_name.replace('.pdf', ''))

    def test_shell_and_html_characters_do_not_survive(self):
        ok, safe_name = validate_upload(_upload('a;rm -rf<b>.pdf', PDF), ['pdf'])
        self.assertTrue(ok, safe_name)
        for char in ';<> ':
            self.assertNotIn(char, safe_name)

    def test_an_absurdly_long_name_is_truncated_but_keeps_its_extension(self):
        """
        Truncating the whole name cut the extension off, so a legitimate long
        filename was rejected as typeless.
        """
        ok, safe_name = validate_upload(_upload('x' * 500 + '.pdf', PDF), ['pdf'])
        self.assertTrue(ok, safe_name)
        self.assertLessEqual(len(safe_name), 200)
        self.assertTrue(safe_name.endswith('.pdf'), safe_name)

    def test_an_extensionless_file_is_rejected_when_extensions_are_required(self):
        ok, message = validate_upload(_upload('noextension', PDF), ['pdf'])
        self.assertFalse(ok)
        self.assertIn('not allowed', message)

    def test_a_double_extension_is_judged_by_the_last_one(self):
        """`report.pdf.exe` is an .exe, and .exe is not in any allow-list."""
        ok, _ = validate_upload(_upload('report.pdf.exe', PDF), ['pdf'])
        self.assertFalse(ok)
