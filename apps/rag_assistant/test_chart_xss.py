"""
REGRESSION — NEW-01/02: untrusted lab text must never reach innerHTML.

`renderChart()` in templates/rag_assistant/chat.html built the card header with

    card.innerHTML = `<span class="chart-card-title">📈 ${data.display_name} · ${data.unit}</span>`

`display_name` and `unit` originate in ParsedLabValue rows, which are populated
from uploaded PDFs, Kanta XML and LLM parsing output. None of that chain escapes
HTML. Uploading a lab document whose unit column reads

    mg/dL<img src=x onerror=...>

and then asking a trend question executed script on a page that holds the CSRF
token in scope. `escapeHtml()` was defined 250 lines above and applied only to
the markdown path.

What these tests pin, and what they deliberately do NOT pin
-----------------------------------------------------------
`get_chart_data()` is a data function. It is CORRECT for it to return the raw
stored text — escaping belongs at the rendering boundary, and a service that
pre-escapes would corrupt values for every non-HTML consumer (the API, the
export, the CSV). So these tests assert the payload arrives *intact*, and then
assert the template renders it without an HTML-parsing path.

That second assertion is the load-bearing one: it fails if anyone reintroduces
innerHTML into renderChart() or renderSources().
"""
import pathlib
import re
from datetime import date

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.rag_assistant.services.trajectory_service import TrajectoryService

XSS = '<img src=x onerror=alert(1)>'

CHAT_TEMPLATE = (pathlib.Path(__file__).resolve().parents[2]
                 / 'templates' / 'rag_assistant' / 'chat.html')


class ChartDataCarriesRawTextTests(TestCase):
    """The service must not silently alter clinical text."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='xss-chart', password='pw-test-only', email='x@example.com')
        for i, value in enumerate((90.0, 95.0, 100.0), start=1):
            record = MedicalRecord.objects.create(
                patient=self.user, title=f'Panel {i}',
                record_type='lab_result', record_date=date(2026, i, 1))
            ParsedLabValue.objects.create(
                record=record, parameter_name='Glucose', value=str(value),
                unit=f'mg/dL{XSS}', canonical_value=value,
                original_unit=f'mg/dL{XSS}', unit_known=True)

    def test_unit_reaches_chart_data_unescaped(self):
        """
        Documents the threat model: the payload is still live at this boundary,
        so the template is the only thing standing between it and the DOM.
        """
        chart = TrajectoryService().get_chart_data(self.user, 'glucose trend')
        if chart is None:
            self.skipTest('no chart produced for this fixture')
        self.assertIn('<img', chart.get('unit', ''),
                      'get_chart_data should return stored text verbatim; '
                      'escaping is the rendering layer’s responsibility')


class ChatTemplateHasNoInnerHtmlSinkTests(SimpleTestCase):
    """
    Structural. These are the assertions that actually prevent the bug from
    returning — a behavioural test cannot run template JavaScript.
    """

    def setUp(self):
        self.source = CHAT_TEMPLATE.read_text(encoding='utf-8')

    def _function_body(self, name: str) -> str:
        """
        From `function name(` to the next top-level `\\nfunction `, with `//`
        comments stripped.

        Stripping matters: the comments explaining *why* innerHTML was removed
        naturally contain the word "innerHTML", and a raw text scan would flag
        the explanation as the offence. The check must look at code.
        """
        start = self.source.index(f'function {name}(')
        nxt = self.source.find('\nfunction ', start + 1)
        body = self.source[start:nxt if nxt != -1 else len(self.source)]
        return '\n'.join(re.sub(r'//.*$', '', line) for line in body.splitlines())

    def test_render_chart_does_not_use_innerhtml(self):
        """ACCEPTANCE — NEW-01."""
        body = self._function_body('renderChart')
        self.assertNotIn('innerHTML', body,
                         'renderChart must build nodes with textContent; '
                         'innerHTML reintroduces the stored-XSS sink')

    def test_render_chart_sets_title_via_textcontent(self):
        body = self._function_body('renderChart')
        self.assertIn('titleEl.textContent', body)

    def test_render_sources_does_not_use_innerhtml_for_labels(self):
        """
        ACCEPTANCE — NEW-02. `row.innerHTML = ''` is permitted (clearing a
        container introduces no markup); interpolating a value is not.
        """
        body = self._function_body('renderSources')
        offenders = [
            line.strip() for line in body.splitlines()
            if 'innerHTML' in line and not re.search(r"innerHTML\s*=\s*''", line)
        ]
        self.assertEqual(offenders, [],
                         f'renderSources must not interpolate into innerHTML: {offenders}')

    def test_general_source_urls_are_protocol_checked(self):
        """A javascript: or data: URL must never become a clickable href."""
        body = self._function_body('renderSources')
        self.assertIn("parsed.protocol === 'http:'", body)
        self.assertIn("parsed.protocol === 'https:'", body)

    def test_escape_html_still_guards_the_markdown_path(self):
        """The original protection must not be lost while fixing the new one."""
        self.assertIn('marked.parse(escapeHtml(text))', self.source)
