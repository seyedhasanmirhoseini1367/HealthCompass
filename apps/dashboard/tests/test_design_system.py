"""Design system guard rails (audit prompt 7 acceptance).

Counts over templates/ that must not creep back, and WCAG AA contrast for every
text/background pair the tokens define.
"""
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ROOT = Path(settings.BASE_DIR)
TEMPLATES = list((ROOT / 'templates').rglob('*.html'))


def _all(pattern, flags=0):
    hits = []
    for path in TEMPLATES:
        for m in re.finditer(pattern, path.read_text(encoding='utf-8'), flags):
            hits.append(f'{path.relative_to(ROOT)}: {m.group(0)[:80]}')
    return hits


def _tokens():
    css = (ROOT / 'static/css/tokens.css').read_text(encoding='utf-8')
    return dict(re.findall(r'--([\w-]+):\s*(#[0-9a-fA-F]{6})', css))


def _luminance(hex_colour):
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


#: (foreground token or hex, background token or hex), all text pairs in use.
TEXT_PAIRS = [
    ('#ffffff', 'brand-600'), ('#ffffff', 'brand-700'), ('#ffffff', 'accent-600'),
    ('#ffffff', 'success-solid'), ('#ffffff', 'warning-solid'), ('#ffffff', 'danger-solid'),
    ('#ffffff', 'info-solid'),
    ('success-text', 'success-bg'), ('warning-text', 'warning-bg'),
    ('danger-text', 'danger-bg'), ('info-text', 'info-bg'),
    ('success-text', '#ffffff'), ('warning-text', '#ffffff'), ('danger-text', '#ffffff'),
    ('gray-900', '#ffffff'), ('gray-900', 'page-bg'), ('text-muted', '#ffffff'),
    ('text-muted', 'page-bg'), ('brand-600', '#ffffff'), ('brand-700', 'brand-50'),
    ('gray-700', 'gray-100'),
]


def ratios():
    tokens = _tokens()
    resolve = lambda v: v if v.startswith('#') else tokens[v]  # noqa: E731
    return {(f, b): round(contrast(resolve(f), resolve(b)), 2) for f, b in TEXT_PAIRS}


class TemplateHygieneTests(SimpleTestCase):
    def test_inline_style_attributes_stay_under_30(self):
        hits = _all(r'\sstyle="[^"]*"')
        self.assertLess(len(hits), 30, hits)

    def test_no_style_blocks(self):
        self.assertEqual(_all(r'<style[\s>]'), [])

    def test_no_inline_event_handlers(self):
        self.assertEqual(_all(r'\son(?:click|change|input|submit|keydown|keyup|load|focus|blur|mouse\w+)="'), [])

    def test_inline_scripts_are_only_json_data(self):
        scripts = _all(r'<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>')
        self.assertEqual(scripts, [])

    def test_no_outline_none(self):
        css = [p for p in (ROOT / 'static/css').rglob('*.css')]
        hits = [str(p) for p in css if re.search(r'outline\s*:\s*none', p.read_text(encoding='utf-8'))]
        self.assertEqual(hits + _all(r'outline\s*:\s*none'), [])

    def test_every_img_has_alt(self):
        self.assertEqual([h for h in _all(r'<img\s[^>]*>') if ' alt=' not in h], [])

    def test_font_awesome_is_gone(self):
        self.assertEqual(_all(r'fontawesome|class="fa[srb]? '), [])


class ContrastTests(SimpleTestCase):
    def test_every_text_pair_meets_wcag_aa(self):
        failing = {pair: r for pair, r in ratios().items() if r < 4.5}
        self.assertEqual(failing, {}, f'below 4.5:1: {failing}')
