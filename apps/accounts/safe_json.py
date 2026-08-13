"""
JSON that is safe to embed directly inside an inline <script> block.

`json.dumps` produces valid JSON, but valid JSON is not automatically safe HTML:
it leaves `<`, `>` and `&` untouched, so a string containing `</script>` ends the
script element early and everything after it is parsed as markup. Lab parameter
names, record titles and document metadata all originate from user uploads and
all end up in chart payloads, which made that a live stored-XSS path.

Django's `json_script` filter solves the same problem by emitting a separate
`<script type="application/json">` element. This helper exists because the
dashboards already embed their payloads as JavaScript expressions
(`const X = {{ x_json|safe }};`), and escaping at the point of serialisation is a
far smaller, lower-risk change than restructuring every chart initialiser.

Also escapes U+2028/U+2029, which are valid in JSON strings but are line
terminators in JavaScript and would otherwise produce a syntax error.
"""
import json

_ESCAPES = {
    ord('<'): '\\u003C',
    ord('>'): '\\u003E',
    ord('&'): '\\u0026',
    0x2028:   '\\u2028',
    0x2029:   '\\u2029',
}


def script_safe_json(payload) -> str:
    """Serialise *payload* for embedding in an inline <script> block."""
    return json.dumps(payload, default=str).translate(_ESCAPES)
