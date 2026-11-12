"""Darken text colours that fail WCAG AA on light backgrounds (audit UX-02).

Only `color:` declarations change; backgrounds, borders and fills keep their
colour. Rules for the dark footer (.ft-*) are skipped, where light text is right.
Each replacement is the nearest shade of the same hue that reaches 4.5:1 on white.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DARKER = {
    '#94a3b8': '#475569', '#cbd5e1': '#475569', '#64748b': '#475569', '#a0aec0': '#64748b', '#9ca3af': '#6b7280',
    '#0ea5e9': '#0369a1', '#38bdf8': '#0369a1', '#0284c7': '#0369a1',
    '#6366f1': '#4f46e5', '#818cf8': '#4f46e5', '#8b5cf6': '#6d28d9', '#a78bfa': '#6d28d9',
    '#22c55e': '#15803d', '#16a34a': '#15803d', '#10b981': '#047857', '#34d399': '#047857',
    '#f59e0b': '#b45309', '#fbbf24': '#b45309', '#d97706': '#b45309', '#f97316': '#c2410c',
    '#ef4444': '#b91c1c', '#f87171': '#b91c1c', '#dc2626': '#b91c1c',
    '#ec4899': '#be185d', '#14b8a6': '#0f766e', '#06b6d4': '#0e7490',
}
COLOR = re.compile(r'(?<![-\w])(color\s*:\s*)(#[0-9a-fA-F]{6})\b')


FILL = {'#6366f1': '#4f46e5', '#818cf8': '#4f46e5', '#8b5cf6': '#6d28d9', '#0ea5e9': '#0369a1',
        '#38bdf8': '#0369a1', '#22c55e': '#15803d', '#10b981': '#047857', '#16a34a': '#15803d',
        '#ef4444': '#b91c1c', '#f59e0b': '#b45309', '#f97316': '#c2410c', '#ec4899': '#be185d',
        '#14b8a6': '#0f766e', '#06b6d4': '#0e7490', '#0284c7': '#0369a1'}
WHITE_TEXT = re.compile(r'(?<![-\w])color\s*:\s*(#fff(?![0-9a-f])|#ffffff|white)', re.I)
BG = re.compile(r'(background(?:-color)?\s*:[^;}]*)')


def fix(text):
    text = COLOR.sub(lambda m: m.group(1) + DARKER.get(m.group(2).lower(), m.group(2)), text)
    if WHITE_TEXT.search(text):
        # White text: darken the fill behind it instead.
        text = BG.sub(lambda m: re.sub(r'#[0-9a-fA-F]{6}', lambda c: FILL.get(c.group(0).lower(), c.group(0)), m.group(1)), text)
    return text


def fix_css(text):
    out = []
    for rule in re.split(r'(?<=\})', text):
        out.append(rule if re.search(r'\.ft-', rule.split('{')[0]) else fix(rule))
    return ''.join(out)


changed = 0
for path in list((ROOT / 'static/css').rglob('*.css')):
    s = path.read_text(encoding='utf-8')
    new = fix_css(s)
    if new != s:
        path.write_text(new, encoding='utf-8')
        changed += 1
for path in list((ROOT / 'templates').rglob('*.html')) + list((ROOT / 'static/js/pages').glob('*.js')):
    s = path.read_text(encoding='utf-8')
    new = re.sub(r'(data-inline-style="[^"]*"|style="[^"]*")', lambda m: fix(m.group(0)), s)
    if new != s:
        path.write_text(new, encoding='utf-8')
        changed += 1
print(changed, 'files')
