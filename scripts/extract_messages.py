"""
Write locale/<lang>/LC_MESSAGES/django.po for the strings marked for translation.

`python manage.py makemessages -l fi -l sv` does this properly and should be
used wherever GNU gettext is installed. This script exists because it is not
always available (it is not on a stock Windows machine); it finds the same
markers: {% trans "..." %}, {% blocktrans %}...{% endblocktrans %} in templates
and _()/gettext()/gettext_lazy() in Python. Existing translations are kept.

Translations stay empty for a human translator. Compile with
`python manage.py compilemessages` (also needs gettext).
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
LANGS = ['fi', 'sv']

TRANS = re.compile(r"""\{%\s*trans\s+(['"])(.+?)\1(?:\s+[^%]*)?%\}""")
BLOCK = re.compile(r'\{%\s*blocktrans(?:late)?(?P<args>[^%]*)%\}(?P<body>.*?)\{%\s*endblocktrans(?:late)?\s*%\}', re.S)
PY = re.compile(r"""\b(?:_|gettext|gettext_lazy|pgettext_lazy)\(\s*(['"])(.+?)(?<!\\)\1\s*\)""")


def collect():
    found = {}
    for path in sorted((ROOT / 'templates').rglob('*.html')):
        text = path.read_text(encoding='utf-8')
        rel = path.relative_to(ROOT).as_posix()
        for m in TRANS.finditer(text):
            found.setdefault(m.group(2), set()).add(rel)
        for m in BLOCK.finditer(text):
            body = re.sub(r'\{\{\s*(\w+)\s*\}\}', r'%(\1)s', m.group('body')).strip()
            found.setdefault(body, set()).add(rel)
    for path in sorted((ROOT / 'apps').rglob('*.py')):
        if 'migrations' in path.parts or path.name.startswith('test'):
            continue
        rel = path.relative_to(ROOT).as_posix()
        for m in PY.finditer(path.read_text(encoding='utf-8')):
            found.setdefault(m.group(2), set()).add(rel)
    return found


def po_escape(text):
    return text.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def existing(po_path):
    if not po_path.exists():
        return {}
    out, msgid = {}, None
    for line in po_path.read_text(encoding='utf-8').splitlines():
        if line.startswith('msgid "'):
            msgid = line[7:-1]
        elif line.startswith('msgstr "') and msgid:
            out[msgid] = line[8:-1]
    return out


def main():
    messages = collect()
    for lang in LANGS:
        po = ROOT / 'locale' / lang / 'LC_MESSAGES' / 'django.po'
        po.parent.mkdir(parents=True, exist_ok=True)
        keep = existing(po)
        lines = ['# HealthCompass interface strings. Translations are left empty for a translator.',
                 'msgid ""', 'msgstr ""',
                 '"Content-Type: text/plain; charset=UTF-8\\n"',
                 f'"Language: {lang}\\n"', '']
        for msgid in sorted(messages):
            esc = po_escape(msgid)
            for ref in sorted(messages[msgid]):
                lines.append(f'#: {ref}')
            if '%(' in msgid:
                lines.append('#, python-format')
            lines += [f'msgid "{esc}"', f'msgstr "{keep.get(esc, "")}"', '']
        po.write_text('\n'.join(lines), encoding='utf-8')
        print(f'{po.relative_to(ROOT)}: {len(messages)} strings')


if __name__ == '__main__':
    main()
