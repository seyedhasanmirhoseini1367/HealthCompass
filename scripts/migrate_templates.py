"""
One-off migration of templates/ to the design system (audit prompt 7, step 4).

For every template:
  * <style> blocks          -> static/css/pages/<dir>__<name>.css, linked in place
  * style="..." attributes  -> generated classes in static/css/generated.css
                               (display:none stays inline; templated values and
                               elements with an id also get data-inline-style,
                               applied through the CSSOM by static/js/styles.js)
  * inline <script> blocks  -> static/js/pages/<dir>__<name>[-n].js, loaded at the
                               same position; template values move into a JSON
                               data block the script reads (HC_PAGE)
  * on*="..." handlers      -> data-on-<event> / data-args and friends, handled by
                               the delegated listener in static/js/app.js

Run once; prints anything it could not convert. Idempotent on already-migrated
templates (they contain none of the patterns).
"""
import hashlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / 'templates'
CSS_PAGES = ROOT / 'static/css/pages'
JS_PAGES = ROOT / 'static/js/pages'
GENERATED = ROOT / 'static/css/generated.css'

TAGEXPR = r'(?:\{\{.*?\}\}|\{%.*?%\})'
TAG_RE = re.compile(r'<([a-zA-Z][\w-]*)((?:[^<>{]|' + TAGEXPR + r'|\{(?![{%]))*)>', re.S)
STYLE_ATTR = re.compile(r'\sstyle="([^"]*)"')
HANDLER_ATTR = re.compile(r'\son(click|change|input|submit|keydown)="([^"]*)"')

problems = []
generated = {}  # normalised style -> class


def page_key(path):
    rel = path.relative_to(TPL).with_suffix('')
    return '__'.join(rel.parts)


def split_decls(style):
    out, buf, depth, quote = [], '', 0, None
    for ch in style:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
            continue
        if ch in '\'"':
            quote = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        if ch == ';' and depth == 0:
            out.append(buf)
            buf = ''
        else:
            buf += ch
    out.append(buf)
    decls = []
    for d in out:
        if ':' in d and d.strip():
            prop, val = d.split(':', 1)
            decls.append((prop.strip().lower(), val.strip()))
    return decls


def class_for(decls):
    text = ';'.join(f'{p}:{v}' for p, v in decls)
    if text not in generated:
        generated[text] = 'x-' + hashlib.sha1(text.encode()).hexdigest()[:8]
    return generated[text]


def add_class(attrs, cls):
    m = re.search(r'\sclass="([^"]*)"', attrs)
    if m:
        return attrs[:m.end(1)] + ' ' + cls + attrs[m.end(1):]
    return attrs + f' class="{cls}"'


def convert_style(attrs):
    m = STYLE_ATTR.search(attrs)
    if not m:
        return attrs
    style = m.group(1).strip()
    has_id = re.search(r'\sid="', attrs) is not None
    rest = attrs[:m.start()] + attrs[m.end():]
    if re.search(TAGEXPR, style):
        return rest + f' data-inline-style="{style}"'
    decls = split_decls(style)
    hidden = [d for d in decls if d[0] == 'display' and d[1].replace(' ', '').lower().startswith('none')]
    others = [d for d in decls if d not in hidden]
    if others:
        rest = add_class(rest, class_for(others))
        if has_id:
            rest += ' data-inline-style="' + ';'.join(f'{p}:{v}' for p, v in others) + '"'
    if hidden:
        rest += ' style="display:none"'
    return rest


# ── handlers ─────────────────────────────────────────────────────────────────

def js_args(text):
    """JS literal argument list -> JSON list, or None if not simple literals."""
    args, buf, quote, depth = [], '', None, 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == '\\' and i + 1 < len(text):
                buf += ch + text[i + 1]
                i += 2
                continue
            buf += ch
            if ch == quote:
                quote = None
        elif ch in '\'"':
            quote = ch
            buf += ch
        elif ch in '([{':
            depth += 1
            buf += ch
        elif ch in ')]}':
            depth -= 1
            buf += ch
        elif ch == ',' and depth == 0:
            args.append(buf.strip())
            buf = ''
        else:
            buf += ch
        i += 1
    if buf.strip():
        args.append(buf.strip())
    out = []
    for a in args:
        if a in ('this', 'event'):
            out.append({'$': a})
        elif re.fullmatch(r'-?\d+(\.\d+)?', a):
            out.append(float(a) if '.' in a else int(a))
        elif a in ('true', 'false', 'null'):
            out.append({'true': True, 'false': False, 'null': None}[a])
        elif len(a) >= 2 and a[0] == a[-1] and a[0] in '\'"':
            body = a[1:-1].replace("\\'", "'").replace('\\"', '"')
            out.append(body)
        else:
            return None
    return out


def attr_json(value):
    # JSON in a single-quoted HTML attribute; template tags stay as they are.
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("'", '&#39;')


def _attr_text(text):
    """A JS string body moved into an HTML attribute: undo JS quote escapes and
    drop |escapejs, since attribute autoescaping now protects the value."""
    return re.sub(r'\|escapejs', '', text).replace("\\'", "'")


def convert_handler(event, code, path):
    code = code.strip().rstrip(';').strip()
    m = re.fullmatch(r"document\.getElementById\('([^']+)'\)\.click\(\)", code)
    if m:
        return f' data-click-target="{m.group(1)}"'
    m = re.fullmatch(r"location\.href\s*=\s*'([^']*)'", code)
    if m:
        return f' data-href="{m.group(1)}"'
    if code == 'event.stopPropagation()':
        return ' data-stop-propagation'
    if code == 'this.form.submit()':
        return ' data-autosubmit'
    m = re.fullmatch(r"return confirm\('((?:[^'\\]|\\.)*)'\)", code)
    if m and event == 'submit':
        return f' data-confirm="{_attr_text(m.group(1))}"'
    m = re.fullmatch(r"openDeleteModal\('((?:[^'\\]|\\.)*)',\s*\(\)\s*=>\s*document\.getElementById\('([^']+)'\)\.submit\(\),\s*'([^']*)'\)", code)
    if m:
        return (f' data-confirm="{_attr_text(m.group(1))}" data-confirm-title="{m.group(3)}"'
                f' data-submit-form="{m.group(2)}"')
    m = re.fullmatch(r"navigator\.clipboard\.writeText\('([^']*)'\)(?:\.then\(.*\))?", code)
    if m:
        return f' data-copy="{m.group(1)}"'
    m = re.fullmatch(r'(window\.)?([A-Za-z_$][\w$]*)\((.*)\)', code, re.S)
    if m:
        args = js_args(m.group(3))
        if args is not None:
            out = f' data-on-{event}="{m.group(2)}"'
            if args:
                out += f" data-args='{attr_json(args)}'"
            return out
    problems.append(f'{path}: on{event}="{code}"')
    return None


def convert_handlers(attrs, path):
    def rep(m):
        new = convert_handler(m.group(1), m.group(2), path)
        return m.group(0) if new is None else new
    return HANDLER_ATTR.sub(rep, attrs)


# ── scripts ──────────────────────────────────────────────────────────────────

def json_string_body(content):
    """Turn the source text of a JS string literal (JS escapes and template tags)
    into the body of a JSON string that parses to the same value."""
    out = []
    for piece in re.split('(' + TAGEXPR + ')', content):
        if not piece:
            continue
        if re.fullmatch(TAGEXPR, piece):
            # Output already escaped for JS is valid JSON-string text as is.
            out.append(piece if 'escapejs' in piece
                       else '{% filter escapejs %}' + piece + '{% endfilter %}')
        else:
            text = piece.replace("\\'", "'")
            text = re.sub(r'(?<!\\)"', '\\"', text)
            out.append(text)
    return ''.join(out)


def externalise_script(body, key, idx, path):
    """Replace template tags in a script body with HC_PAGE.kN lookups."""
    values = {}
    counter = [0]

    def new_key():
        counter[0] += 1
        return f'k{counter[0]}'

    name = f'{key}-{idx}' if idx else key
    var = 'HC_PAGE_' + re.sub(r'\W', '_', name)

    # 1. String literals containing template tags. Quotes: one line only, so an
    #    apostrophe in a comment cannot pair up across code. Backticks may span.
    def lit(whole, quote, content):
        if not re.search(TAGEXPR, content):
            return whole
        if quote == '`':
            # Template literal: tags become ${VAR.kN}, the rest stays JS.
            def inner(t):
                kk = new_key()
                values[kk] = ('str', t.group(0))
                return '${' + var + '.' + kk + '}'
            return '`' + re.sub(TAGEXPR, inner, content) + '`'
        k = new_key()
        values[k] = ('str', content)
        return f'{var}.{k}'

    def lit_any(m):
        if m.group(1):
            return lit(m.group(0), m.group(1), m.group(2))
        return lit(m.group(0), '`', m.group(4))
    body = re.sub(r"(['\"])((?:\\.|(?!\1)[^\n])*?)\1|(`)((?:\\.|[^`])*?)`", lit_any, body, flags=re.S)

    # 2. Remaining bare tags are raw JS values (JSON, numbers).
    def raw(m):
        k = new_key()
        values[k] = ('raw', m.group(0))
        return f'{var}.{k}'
    body = re.sub(TAGEXPR, raw, body)

    data_id = f'hc-page-data-{name}'
    js = ''
    if values:
        js += (f"var {var} = JSON.parse(document.getElementById('{data_id}').textContent);\n")
    js += body.strip('\n') + '\n'
    JS_PAGES.mkdir(parents=True, exist_ok=True)
    (JS_PAGES / f'{name}.js').write_text(js, encoding='utf-8')

    html = ''
    if values:
        parts = []
        for k, (kind, tmpl) in values.items():
            if kind == 'str':
                parts.append(f'"{k}": "{json_string_body(tmpl)}"')
            else:
                parts.append(f'"{k}": {tmpl}')
        html += f'<script type="application/json" id="{data_id}">{{' + ', '.join(parts) + '}</script>\n'
    html += f"<script src=\"{{% static 'js/pages/{name}.js' %}}\"></script>"
    return html


# ── per template ─────────────────────────────────────────────────────────────

def migrate(path):
    s = path.read_text(encoding='utf-8')
    original = s
    key = page_key(path)

    # <style> blocks
    styles = re.findall(r'<style[^>]*>(.*?)</style>', s, re.S)
    if styles:
        CSS_PAGES.mkdir(parents=True, exist_ok=True)
        css = '\n'.join(b.strip() for b in styles) + '\n'
        css = re.sub(r'outline\s*:\s*none\s*;?', '', css)
        (CSS_PAGES / f'{key}.css').write_text(f'/* Moved from templates/{path.relative_to(TPL).as_posix()} */\n' + css,
                                             encoding='utf-8')
        first = [True]

        def link(_m):
            if first[0]:
                first[0] = False
                return f"<link rel=\"stylesheet\" href=\"{{% static 'css/pages/{key}.css' %}}\">"
            return ''
        s = re.sub(r'<style[^>]*>.*?</style>', link, s, flags=re.S)

    # inline scripts (not JSON data, not src=)
    idx = [0]

    def script(m):
        attrs, body = m.group(1), m.group(2)
        if 'src=' in attrs or 'application/json' in attrs or 'application/ld+json' in attrs:
            return m.group(0)
        n = idx[0]
        idx[0] += 1
        return externalise_script(body, key, n, path)
    s = re.sub(r'<script((?:\s[^>]*)?)>(.*?)</script>', script, s, flags=re.S)

    # tags: style + handlers (skip inside JSON data blocks)
    def tag(m):
        name, attrs = m.group(1), m.group(2)
        if name.lower() in ('script', 'style', 'link', 'meta'):
            return m.group(0)
        attrs = convert_style(attrs)
        attrs = convert_handlers(attrs, path)
        return f'<{name}{attrs}>'
    s = TAG_RE.sub(tag, s)

    if s != original:
        if '{% load static %}' not in s and ("{% static" in s):
            if s.lstrip().startswith('{% extends'):
                s = re.sub(r'(\{% extends [^%]*%\})', r'\1\n{% load static %}', s, count=1)
            else:
                s = '{% load static %}\n' + s
        path.write_text(s, encoding='utf-8')
        return True
    return False


def main():
    changed = 0
    for path in sorted(TPL.rglob('*.html')):
        if migrate(path):
            changed += 1
    rules = '\n'.join(f'.{cls}.{cls}.{cls}{{{text}}}' for text, cls in sorted(generated.items(), key=lambda x: x[1]))
    GENERATED.write_text(
        '/* Generated by scripts/migrate_templates.py from former inline style attributes.\n'
        '   The repeated class gives inline-style precedence over page rules; replace\n'
        '   these with tokens and components as templates are reworked. */\n' + rules + '\n',
        encoding='utf-8')
    print(f'{changed} templates changed, {len(generated)} generated classes')
    for p in problems:
        print('MANUAL:', p)


if __name__ == '__main__':
    sys.exit(main())
