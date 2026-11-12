"""{% icon %}: an icon from the SVG sprite (static/icons.svg).

    {% icon 'bell' %}                      decorative: hidden from screen readers
    {% icon 'bell' cls='icon-spin' %}      extra classes
    {% icon 'bell' label='Notifications' %} meaningful: exposed as an image

Registered as a builtin (settings TEMPLATES OPTIONS), so no {% load %} needed.
The sprite replaces Font Awesome's CSS and web fonts (audit PERF-01, UX-09).
"""
from django import template
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def icon(name, cls='', label=''):
    href = f"{static('icons.svg')}#i-{name}"
    classes = f'icon {cls}'.strip()
    if label:
        return format_html('<svg class="{}" role="img" aria-label="{}" focusable="false">'
                           '<use href="{}"></use></svg>', classes, label, href)
    return format_html('<svg class="{}" aria-hidden="true" focusable="false"><use href="{}"></use></svg>',
                       classes, href)
