"""
REGRESSION — Phase 1: XML hardening must be mandatory, not best-effort.

`parsers.py` used to degrade silently:

    try:
        import defusedxml.ElementTree as ET
    except ImportError:
        from xml.etree import ElementTree as ET  # fallback for local dev

Two facts made that dangerous rather than merely untidy:

  1. defusedxml was NOT installed in the environment where all 644 tests passed,
     so the fallback was the path actually exercised, and a green suite was
     compatible with the hardening being off.
  2. The fallback is genuinely weaker. Measured on this payload: stdlib
     ElementTree parses it and expands the body from 10 to 1000 characters. The
     standard nine-level form expands to gigabytes, so an uploaded Kanta file
     could exhaust process memory. defusedxml raises EntitiesForbidden.

No test covered any of it — a grep for XXE/billion/ENTITY across the suite
returned nothing.

These tests pin three separate properties, because any one alone can pass while
the protection is absent:

  * the parser binds defusedxml, not the stdlib module   (no silent fallback)
  * a missing defusedxml raises instead of falling back  (fails closed)
  * a malicious document is rejected through the normal error contract, so the
    upload fails cleanly rather than with a 500
"""
import builtins
import importlib
import sys
from unittest.mock import patch

from django.test import SimpleTestCase

from apps.medical_records import parsers
from apps.medical_records.parsers import KantaXMLParser

#: Three levels of nested entities. Deliberately small — it demonstrates
#: quadratic expansion without allocating gigabytes inside the test suite.
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY a "AAAAAAAAAA">
 <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
 <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]>
<root>&c;</root>"""

#: An external-entity document — the classic file-disclosure shape.
EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<root>&xxe;</root>"""

BENIGN = b"""<?xml version="1.0"?><ClinicalDocument><title>Benign</title></ClinicalDocument>"""


class ParserBindsDefusedXmlTests(SimpleTestCase):
    """The module must be using the hardened parser, not the stdlib one."""

    def test_et_is_defusedxml_not_stdlib(self):
        """ACCEPTANCE. Was `xml.etree.ElementTree` whenever defusedxml was absent."""
        self.assertTrue(
            parsers.ET.__name__.startswith('defusedxml'),
            f'parsers.ET is {parsers.ET.__name__!r} — the stdlib fallback is active',
        )

    def test_defused_exception_type_is_available(self):
        """The parser needs this symbol to convert rejections into clean errors."""
        self.assertTrue(hasattr(parsers, 'DefusedXmlException'))

    def test_no_silent_stdlib_fallback_remains_in_source(self):
        """
        Guards against the fallback being reintroduced. A future edit that adds
        `from xml.etree import ElementTree` back would pass every behavioural
        test above while quietly restoring the vulnerability.
        """
        import pathlib

        source = pathlib.Path(parsers.__file__).read_text(encoding='utf-8-sig')
        self.assertNotIn('from xml.etree import ElementTree as ET', source)


class FailsClosedTests(SimpleTestCase):
    """A missing security control must stop the app, not degrade it."""

    def test_missing_defusedxml_raises_instead_of_falling_back(self):
        """
        Reload the module with defusedxml unimportable. The old code swallowed
        this and continued with the stdlib parser; it must now raise.
        """
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name.startswith('defusedxml'):
                raise ImportError('simulated: defusedxml not installed')
            return real_import(name, *args, **kwargs)

        saved = sys.modules.pop('apps.medical_records.parsers')
        try:
            with patch.object(builtins, '__import__', side_effect=blocked):
                with self.assertRaises(ImportError) as ctx:
                    importlib.import_module('apps.medical_records.parsers')
            self.assertIn('defusedxml is required', str(ctx.exception))
        finally:
            # Restore the real module for every other test in the run.
            sys.modules['apps.medical_records.parsers'] = saved


class MaliciousDocumentRejectionTests(SimpleTestCase):
    """Rejection must happen, and must arrive through the normal error path."""

    def test_entity_expansion_is_rejected(self):
        """ACCEPTANCE. stdlib expands this to 1000 chars; defusedxml refuses."""
        result = KantaXMLParser().parse(BILLION_LAUGHS)
        self.assertIn('error', result)
        self.assertEqual(result['records'], [])

    def test_external_entity_is_rejected(self):
        result = KantaXMLParser().parse(EXTERNAL_ENTITY)
        self.assertIn('error', result)
        self.assertEqual(result['records'], [])

    def test_rejection_does_not_raise(self):
        """
        DefusedXmlException derives from ValueError, not ParseError, so without
        an explicit branch it escapes the parser and surfaces as a 500 instead
        of a clean upload validation failure.
        """
        try:
            KantaXMLParser().parse(BILLION_LAUGHS)
        except Exception as exc:                      # pragma: no cover
            self.fail(f'parse() raised {type(exc).__name__} instead of returning an error')

    def test_error_contract_matches_what_create_from_kanta_expects(self):
        """
        `create_from_kanta` branches on `'error' in parsed`. If rejection used a
        different shape the upload would proceed as though the file were empty.
        """
        result = KantaXMLParser().parse(BILLION_LAUGHS)
        self.assertTrue('error' in result)
        self.assertIsInstance(result['error'], str)
        self.assertTrue(result['error'])

    def test_benign_document_still_parses(self):
        """The hardening must not reject ordinary Kanta documents."""
        result = KantaXMLParser().parse(BENIGN)
        self.assertNotIn('error', result)
