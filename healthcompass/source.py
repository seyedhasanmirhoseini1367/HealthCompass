"""Read a template together with the page scripts it loads.

Page JavaScript moved out of templates into static/js/pages/ (audit prompt 7).
Tests that inspect a page's script (XSS guards, wiring checks) read the template
plus every static/js/pages/*.js file it references, so they keep checking the
code that actually runs on that page.
"""
import re
from pathlib import Path

from django.conf import settings


def template_with_scripts(template_path) -> str:
    path = Path(template_path)
    if not path.is_absolute():
        path = Path(settings.BASE_DIR) / path
    text = path.read_text(encoding='utf-8', errors='replace')
    parts = [text]
    for rel in re.findall(r"""\{%\s*static\s+['"](js/pages/[^'"]+\.js)['"]\s*%\}""", text):
        js = Path(settings.BASE_DIR) / 'static' / rel
        if js.exists():
            parts.append(js.read_text(encoding='utf-8', errors='replace'))
    return '\n'.join(parts)
