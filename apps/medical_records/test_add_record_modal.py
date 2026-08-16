"""
The Add Record modal must actually reach step two.

The bug
-------
Choosing "PDF / Document" swapped the modal title and then showed an empty box.
No form, no back arrow. Every option behaved the same way, and the only route
into the product — adding a record — was dead.

The cause was a CSS/JS disagreement introduced when inline styles were removed
from the templates. The panels are hidden in the markup with `class="d-none"`,
and `.d-none` is declared `display:none !important`. `showPanel()` still tried
to reveal them with `element.style.display = 'block'`. An inline style loses to
`!important`, so the assignment ran, succeeded, and changed nothing.

Nothing failed loudly. There is no exception to catch and no server-side
behaviour to assert, which is exactly why it survived a full green suite and had
to be found by clicking. These tests read the shipped template and stylesheet as
text, because that is where the contract lives.

They are deliberately about the *mechanism*, not the appearance: any element
hidden with `d-none` must be revealed by removing that class, never by an inline
display.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

_ROOT = Path(settings.BASE_DIR)
_LIST = _ROOT / 'templates' / 'medical_records' / 'list.html'
_CSS = _ROOT / 'static' / 'css' / 'main.css'


def _list_html() -> str:
    return _LIST.read_text(encoding='utf-8', errors='replace')


class DNoneIsImportantTests(TestCase):
    """The premise the other tests rest on."""

    def test_the_utility_class_is_important(self):
        """
        If `d-none` ever stops being `!important`, inline display would start
        working again and these tests would be guarding nothing. Assert the
        premise so the guard fails loudly instead of going quiet.
        """
        css = _CSS.read_text(encoding='utf-8', errors='replace')
        rule = re.search(r'\.d-none\s*\{[^}]*\}', css)

        self.assertIsNotNone(rule, '.d-none is no longer defined in main.css')
        self.assertIn('!important', rule.group(0))


class AddRecordModalTests(TestCase):

    def setUp(self):
        self.html = _list_html()

    def _body_of(self, function_name: str) -> str:
        start = self.html.index(f'function {function_name}(')
        end = self.html.index('\n}', start)
        return self.html[start:end]

    # ── the reported failure ────────────────────────────────────────────────

    def test_choosing_an_option_reveals_its_panel(self):
        """ACCEPTANCE — this is what was broken on screen."""
        body = self._body_of('showPanel')

        self.assertIn("classList.remove('d-none')", body,
                      'showPanel must reveal the panel by removing d-none')

    def test_choosing_an_option_does_not_fight_important_with_inline_style(self):
        """The specific dead assignment. It ran, and did nothing."""
        self.assertNotIn('style.display', self._body_of('showPanel'))

    def test_going_back_returns_to_the_option_list(self):
        body = self._body_of('showOptions')

        self.assertIn("classList.remove('d-none')", body)
        self.assertNotIn('style.display', body)

    def test_the_back_arrow_appears_on_step_two(self):
        """
        `modalBack` ships with d-none, so it had the same defect: there was no
        way back to the option list except closing the modal.
        """
        self.assertRegex(
            self._body_of('showPanel'),
            r"modalBack'\)\.classList\.remove\('d-none'\)")

    def test_every_option_maps_to_a_panel_that_exists(self):
        """
        A typo in an onclick would reproduce the same empty modal by a different
        route, so check the wiring rather than trusting it.
        """
        chosen = set(re.findall(r"showPanel\('([\w-]+)'\)", self.html))
        panels = set(re.findall(r'id="panel-([\w-]+)"', self.html))

        self.assertTrue(chosen, 'no options found — the picker markup moved')
        self.assertEqual(chosen - panels, set(),
                         'an option points at a panel that does not exist')

    def test_an_unknown_panel_name_leaves_the_modal_usable(self):
        """
        Defence in depth: showPanel returns before hiding anything, so a bad
        name cannot blank the modal the way the original bug did.
        """
        body = self._body_of('showPanel')
        guard = body.index('if (!panel)')

        self.assertLess(guard, body.index("classList.add('d-none')"),
                        'the guard must run before anything is hidden')

    # ── the same defect, one step further in ────────────────────────────────

    def test_a_scanned_photo_becomes_visible(self):
        """
        `scan-preview-wrap` is also hidden with d-none: you took a photo and the
        modal showed you nothing.
        """
        self.assertIn("getElementById('scan-preview-wrap').classList.remove('d-none')",
                      self.html)
        self.assertNotIn("getElementById('scan-preview-wrap').style.display", self.html)


class NoInlineDisplayAgainstDNoneTests(TestCase):
    """
    The general rule, across every template.

    The bug arrived as a sweep ("remove all inline styles") that changed markup
    without changing the JS that drove it. A sweep can happen again; this finds
    it in any template rather than only the two files fixed today.
    """

    def test_no_template_reveals_a_d_none_element_with_an_inline_style(self):
        offenders = []

        for path in (_ROOT / 'templates').rglob('*.html'):
            src = path.read_text(encoding='utf-8', errors='replace')

            hidden = set(re.findall(r'id="([\w-]+)"[^>]*class="[^"]*\bd-none\b', src))
            hidden |= set(re.findall(r'class="[^"]*\bd-none\b[^"]*"[^>]*id="([\w-]+)"', src))

            for match in re.finditer(
                    r"getElementById\(\s*['\"]([\w-]+)['\"]\s*\)\s*\.style\.display", src):
                if match.group(1) in hidden:
                    line = src[:match.start()].count('\n') + 1
                    offenders.append(
                        f'{path.relative_to(_ROOT)}:{line} sets style.display on '
                        f'#{match.group(1)}, which is hidden by d-none')

        self.assertEqual(offenders, [], '\n' + '\n'.join(offenders))
