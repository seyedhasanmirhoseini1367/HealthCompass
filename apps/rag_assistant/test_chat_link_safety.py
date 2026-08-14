"""
REGRESSION — markdown links in assistant output could carry javascript: URLs.

`renderMarkdown` was `marked.parse(escapeHtml(text))`. escapeHtml runs BEFORE
parsing, so it neutralises literal `<a>` tags the model echoes out of a
document — but `[click](javascript:alert(1))` contains no markup to escape. The
anchor does not exist yet; marked builds it afterwards from text that passed
through escaping untouched. More escaping could not have helped: escapeHtml
cannot see a link that has not been constructed.

Verified against the bundled marked 12.0.2 before the fix:

    [x](javascript:alert(1))        -> <a href="javascript:alert(1)">x</a>
    [x](JaVaScRiPt:alert(1))        -> <a href="JaVaScRiPt:alert(1)">x</a>
    ![alt](javascript:alert(1))     -> <img src="javascript:alert(1)" alt="alt">
    ![alt](https://evil/p.png)      -> <img src="https://evil/p.png" alt="alt">

Assistant text is model output derived from retrieved document content, so an
uploaded record is an input to it — this is reachable by anyone who can get a
document into a patient's file.

These tests execute the real code rather than inspecting it: the block between
the LINK SAFETY sentinels in chat.html is sliced out verbatim and run in node
against the same `static/vendor/marked.min.js` the browser loads. A test that
only grepped for "safeUrl" would pass against a broken implementation.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

CHAT = Path('templates/rag_assistant/chat.html')
MARKED = Path('static/vendor/marked.min.js')
BEGIN = '// ── LINK SAFETY ─ BEGIN'
END = '// ── LINK SAFETY ─ END'

ORIGIN = 'https://app.example'


def _extract_block() -> str:
    source = CHAT.read_text(encoding='utf-8')
    start = source.index(BEGIN)
    end = source.index(END)
    return source[start:end]


def _run(cases, block=None):
    """Render each markdown string through the real block, in node."""
    js_block = block if block is not None else _extract_block()

    harness = (
        'const marked = require(%s);\n'
        'globalThis.location = { origin: %s };\n'
        '%s\n'
        'const out = {};\n'
        'for (const [k, v] of Object.entries(%s)) {\n'
        '  try { out[k] = marked.parse(escapeHtml(v)).trim(); }\n'
        '  catch (e) { out[k] = "THREW: " + e.message; }\n'
        '}\n'
        'console.log(JSON.stringify(out));\n'
    ) % (json.dumps(str(MARKED.resolve())), json.dumps(ORIGIN),
         js_block, json.dumps(cases))

    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / 'harness.cjs'
        script.write_text(harness, encoding='utf-8')
        result = subprocess.run(['node', str(script)], capture_output=True,
                                text=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError(f'node harness failed:\n{result.stderr}')
    return json.loads(result.stdout)


class _NodeBacked(SimpleTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if shutil.which('node') is None:
            raise cls.skipException('node is not installed; cannot execute the renderer')
        if not MARKED.exists():
            raise cls.skipException(f'{MARKED} not present')


class BlockExtractionTests(_NodeBacked):
    """The slice the other tests execute has to be the real thing."""

    def test_the_sentinels_are_present(self):
        source = CHAT.read_text(encoding='utf-8')
        self.assertIn(BEGIN, source)
        self.assertIn(END, source)
        self.assertLess(source.index(BEGIN), source.index(END))

    def test_the_block_is_plain_javascript(self):
        """
        Django tags inside it would make the extracted slice unparseable, and
        the tests would skip or fail for a reason unrelated to link safety.
        """
        block = _extract_block()
        for tag in ('{%', '{{'):
            self.assertNotIn(tag, block, f'Django tag {tag} inside the extracted block')

    def test_the_block_defines_what_the_tests_rely_on(self):
        block = _extract_block()
        for name in ('function escapeHtml', 'function safeUrl', 'function renderMarkdown',
                     '_mdRenderer.link', '_mdRenderer.image'):
            self.assertIn(name, block)


class LinkProtocolTests(_NodeBacked):

    def test_a_javascript_url_produces_no_anchor(self):
        """ACCEPTANCE — this rendered <a href="javascript:alert(1)">."""
        out = _run({'case': '[x](javascript:alert(1))'})['case']
        self.assertNotIn('<a', out)
        self.assertNotIn('javascript:', out)
        self.assertIn('x', out)

    def test_casing_does_not_get_it_through(self):
        for raw in ('JaVaScRiPt:alert(1)', 'JAVASCRIPT:alert(1)', '\tjavascript:alert(1)'):
            with self.subTest(raw=raw):
                out = _run({'case': f'[x]({raw})'})['case']
                self.assertNotIn('<a', out)

    def test_other_dangerous_schemes_produce_no_anchor(self):
        for raw in ('data:text/html,y', 'vbscript:msgbox', 'file:///etc/passwd'):
            with self.subTest(raw=raw):
                out = _run({'case': f'[x]({raw})'})['case']
                self.assertNotIn('<a', out)

    def test_the_label_survives_as_plain_text(self):
        """Refusing the link must not silently delete what it said."""
        out = _run({'case': '[Important note](javascript:alert(1))'})['case']
        self.assertIn('Important note', out)

    def test_an_https_link_is_still_a_link(self):
        out = _run({'case': '[x](https://example.com/guide)'})['case']
        self.assertIn('<a', out)
        self.assertIn('https://example.com/guide', out)

    def test_an_http_link_is_still_a_link(self):
        self.assertIn('<a', _run({'case': '[x](http://example.com/)'})['case'])

    def test_an_external_link_is_opened_safely(self):
        out = _run({'case': '[x](https://example.com/)'})['case']
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertIn('target="_blank"', out)


class UrlShapeTests(_NodeBacked):
    """Decisions about the two shapes that are neither plainly safe nor unsafe."""

    def test_a_relative_url_stays_a_link_on_our_own_origin(self):
        """
        ALLOWED. Resolved against location.origin, so it is judged as the
        same-origin URL the browser would navigate to. It is also how the
        assistant cites a record.
        """
        out = _run({'case': '[my record](/records/1)'})['case']
        self.assertIn('<a', out)
        self.assertIn(f'{ORIGIN}/records/1', out)

    def test_a_relative_url_is_not_opened_in_a_new_tab(self):
        out = _run({'case': '[my record](/records/1)'})['case']
        self.assertNotIn('target="_blank"', out)

    def test_a_protocol_relative_url_is_treated_as_the_external_link_it_is(self):
        """
        ALLOWED, and deliberately so: `//evil.example` resolves to
        `https://evil.example/`, which is an ordinary external https link and no
        more dangerous than writing it out. It is judged by what the browser
        would fetch, not by how it looks — and it is NOT mistaken for a
        same-origin path.
        """
        out = _run({'case': '[x](//evil.example/p)'})['case']
        self.assertIn('<a', out)
        self.assertIn('https://evil.example/p', out)
        self.assertIn('rel="noopener noreferrer"', out)
        self.assertNotIn(f'{ORIGIN}/', out)

    def test_a_quote_in_the_url_cannot_break_out_of_the_attribute(self):
        """
        The payload staying in the href is fine — it is inert there. What must
        not happen is the quote closing the attribute early and turning the rest
        into an event handler, so the assertion is about the quote, not about
        the word "onmouseover" appearing somewhere in the string.
        """
        out = _run({'case': '[x](https://e.com/a"onmouseover=alert(1))'})['case']

        self.assertIn('%22', out, 'the quote was not encoded')
        self.assertNotIn('" onmouseover', out, 'the href attribute was terminated early')
        self.assertNotIn('"onmouseover=alert(1)"', out)


class ImageTests(_NodeBacked):
    """
    Images are refused outright rather than validated.

    Nothing in an assistant answer needs one, and an <img> is fetched with no
    action from the reader, so an attacker-chosen src leaks the fact and time of
    reading — and anything the model was induced to encode into the URL —
    whatever protocol it uses.
    """

    def test_a_javascript_image_renders_no_img(self):
        out = _run({'case': '![alt](javascript:alert(1))'})['case']
        self.assertNotIn('<img', out)
        self.assertNotIn('javascript:', out)

    def test_an_https_image_also_renders_no_img(self):
        """ACCEPTANCE — the exfiltration case, which protocol checks would pass."""
        out = _run({'case': '![alt](https://evil.example/pixel.png)'})['case']
        self.assertNotIn('<img', out)
        self.assertNotIn('evil.example', out)

    def test_the_alt_text_is_kept_as_plain_text(self):
        self.assertIn('a chart', _run({'case': '![a chart](https://x.example/i.png)'})['case'])


class OrdinaryMarkdownTests(_NodeBacked):
    """The fix must not cost the formatting the answers rely on."""

    def test_emphasis_lists_and_code_still_render(self):
        out = _run({
            'bold':  'a **strong** point',
            'italic': 'an *emphasised* word',
            'list':  '- one\n- two',
            'code':  'use `metformin` daily',
            'head':  '## Results',
        })
        self.assertIn('<strong>', out['bold'])
        self.assertIn('<em>', out['italic'])
        self.assertIn('<li>', out['list'])
        self.assertIn('<code>', out['code'])
        self.assertIn('<h2', out['head'])

    def test_literal_html_is_still_defanged(self):
        out = _run({'case': '<img src=x onerror=alert(1)>'})['case']
        self.assertNotIn('<img', out)

    def test_quotes_in_ordinary_text_are_unchanged(self):
        """
        escapeHtml was left exactly as it was; widening it for the attribute
        case would have changed how every answer renders.
        """
        self.assertIn('&quot;', _run({'case': 'He said "hello"'})['case'])


class CallSiteTests(SimpleTestCase):
    """
    Behaviour is proven once, above. This proves every path reaches it.

    All assistant text is rendered through renderMarkdown, so the override
    covers each call site by construction — but only while no call site starts
    calling marked.parse directly.
    """

    def test_no_call_site_bypasses_render_markdown(self):
        source = CHAT.read_text(encoding='utf-8')
        block = _extract_block()
        outside = source.replace(block, '')
        self.assertNotIn('marked.parse', outside,
                         'a call site parses markdown without the safe renderer')

    def test_every_assistant_render_goes_through_render_markdown(self):
        source = CHAT.read_text(encoding='utf-8')
        calls = source.count('renderMarkdown(')
        # One definition inside the block, plus every call site.
        self.assertGreaterEqual(calls, 5, 'call sites disappeared; recheck this test')

    def test_the_stored_history_path_uses_it(self):
        """
        QueryLog text rendered on page load. It is the same untrusted content,
        stored — the answer was already saved before anyone read it.
        """
        source = CHAT.read_text(encoding='utf-8')
        self.assertIn("renderMarkdown(bubble.getAttribute('data-raw'))", source)

    def test_the_renderer_is_registered_before_any_call_site_runs(self):
        source = CHAT.read_text(encoding='utf-8')
        self.assertLess(source.index('marked.use({ renderer: _mdRenderer })'),
                        source.index("renderMarkdown(bubble.getAttribute('data-raw'))"))

    def test_link_safety_has_one_implementation(self):
        """
        renderSources and renderConsentAction previously carried their own
        copies of the protocol check. Two rules for one question is how they
        drift into disagreeing.
        """
        source = CHAT.read_text(encoding='utf-8')
        self.assertEqual(source.count('new URL('), 1,
                         'a second URL-validation rule has appeared')
        self.assertIn('safeUrl(s.source_url)', source)
        self.assertIn('safeUrl(url, true)', source)
