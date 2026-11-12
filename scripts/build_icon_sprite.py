"""Build static/icons.svg from the vendored Font Awesome fonts: only icons used."""
import pathlib
import re

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont

ROOT = pathlib.Path('.')
css = (ROOT / 'scripts/assets/fontawesome/css/all.min.css').read_text(encoding='utf-8')

# selector list -> codepoint
codepoints = {}
for sel, cp in re.findall(r'([^{}]+)\{content:"\\([0-9a-f]+)"\}', css):
    for part in sel.split(','):
        m = re.match(r'\s*\.fa-([a-z0-9-]+):(?:before|after)\s*$', part)
        if m:
            codepoints.setdefault(m.group(1), int(cp, 16))

NOT_ICONS = {'solid', 'regular', 'brands', 'spin', 'fw', 'lg', 'xs', 'sm', '2x', '3x', '4x', 'pulse',
             'beat', 'fade', 'shake', 'flip', 'bounce', 'rotate-90', 'rotate-180', 'stack', 'inverse',
             'border', 'pull-left', 'pull-right', 'li', 'ul', 'xl', '2xl', '1x', '5x'}

used = {}  # name -> style


def note(name, style='solid'):
    if name in NOT_ICONS or name not in codepoints:
        return
    if style != 'solid' or name not in used:
        used[name] = style


files = list(ROOT.glob('templates/**/*.html')) + list(ROOT.glob('static/js/**/*.js')) + \
        [p for p in ROOT.glob('apps/**/*.py') if 'migrations' not in p.parts]
for f in files:
    text = f.read_text(encoding='utf-8', errors='ignore')
    for cls in re.findall(r'class="([^"]*\bfa[srb]?\b[^"]*|[^"]*\bfa-(?:solid|regular|brands)\b[^"]*)"', text):
        style = 'regular' if re.search(r'\bfar\b|fa-regular', cls) else ('brands' if re.search(r'\bfab\b|fa-brands', cls) else 'solid')
        for n in re.findall(r'\bfa-([a-z0-9-]+)', cls):
            note(n, style)
    # dynamic or python-defined names: 'icon': 'bell', icon='bell', fa-xxx in strings
    for n in re.findall(r"""['"]icon['"]\s*:\s*['"]([a-z0-9-]+)['"]|icon\s*=\s*['"]([a-z0-9-]+)['"]""", text):
        note(n[0] or n[1])
    for n in re.findall(r'\bfa-([a-z0-9-]+)', text):
        note(n)
    # navigation.py: Item(href, label, 'icon-name'); {% icon 'name' %} in templates
    for n in re.findall(r"Item\('[^']*',\s*'[^']*',\s*'([a-z0-9-]+)'", text):
        note(n)
    for n in re.findall(r"""\{%\s*icon\s+['"]([a-z0-9-]+)['"]""", text):
        note(n)
    for n in re.findall(r"""hcIcon\(\s*['"]([a-z0-9-]+)['"]""", text):
        note(n)
    # names chosen in JS expressions: hcIcon(cond ? 'a' : 'b'), '#i-name'
    for n in re.findall(r"""\?\s*'([a-z0-9-]+)'\s*:\s*'([a-z0-9-]+)'""", text):
        note(n[0]); note(n[1])
    for n in re.findall(r'#i-([a-z0-9-]+)', text):
        note(n)

fonts = {s: TTFont(ROOT / f'scripts/assets/fontawesome/webfonts/fa-{s}-{w}.ttf')
         for s, w in (('solid', '900'), ('regular', '400'), ('brands', '400'))}

symbols, missing = [], []
for name in sorted(used):
    style = used[name]
    cp = codepoints[name]
    font = fonts[style]
    gname = font.getBestCmap().get(cp)
    if gname is None:
        for alt in ('solid', 'regular', 'brands'):
            gname = fonts[alt].getBestCmap().get(cp)
            if gname:
                font = fonts[alt]
                break
    if gname is None:
        missing.append(name)
        continue
    glyphs = font.getGlyphSet()
    pen = SVGPathPen(glyphs)
    glyphs[gname].draw(pen)
    width = font['hmtx'][gname][0]
    symbols.append(f'<symbol id="i-{name}" viewBox="0 0 {width} 512">'
                   f'<path transform="matrix(1 0 0 -1 0 448)" d="{pen.getCommands()}"/></symbol>')

out = ('<svg xmlns="http://www.w3.org/2000/svg" style="display:none">\n'
       '<!-- Generated from the vendored Font Awesome Free fonts (icons: CC BY 4.0). '
       'Only icons used by the app. Rebuild: scripts/build_icon_sprite.py -->\n'
       + '\n'.join(symbols) + '\n</svg>\n')
(ROOT / 'static/icons.svg').write_text(out, encoding='utf-8')
print(len(symbols), 'icons; missing:', missing, '; bytes:', len(out))
