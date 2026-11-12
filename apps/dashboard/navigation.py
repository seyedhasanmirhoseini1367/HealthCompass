"""
The signed-in navigation: one menu per role, defined once.

The sidebar and the phone menu both render from `menu_for(user)`, so they
cannot drift apart in order or wording. Each role sees only what it uses:
a patient never sees Scribe, a doctor's first entry is their patients.

The menu decides nothing about access. Every page still checks permissions
itself, so a URL left out of a menu is hidden, not forbidden.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

#: Section heading in the menu. A string entry in a menu list renders as one.
AI_TOOLS = _('AI tools')
#: A horizontal rule.
DIVIDER = '---'


@dataclass(frozen=True)
class Item:
    href: str
    label: object  # str or lazy translation
    icon: str  # Font Awesome name without the "fa-" prefix


MY_HEALTH = Item('/dashboard/health/', _('My Health'), 'heart-pulse')
RECORDS = Item('/records/', _('Records'), 'file-medical')
APPOINTMENTS = Item('/appointments/', _('Appointments'), 'calendar-check')
COMPANIONS = Item('/care/', _('Companions'), 'hand-holding-heart')
ASSISTANT = Item('/assistant/', _('Assistant'), 'robot')
ANALYTICS = Item('/insights/', _('Analytics'), 'brain')
SCRIBE = Item('/scribe/', _('Scribe'), 'microphone')
MONITORING = Item('/dashboard/monitoring/', _('Monitoring'), 'chart-line')
SETTINGS = Item('/dashboard/settings/', _('Settings'), 'gear')

#: Each role's home page. /dashboard/ renders the right one per role.
MY_PATIENTS = Item('/dashboard/', _('My Patients'), 'user-doctor')
WORKSPACE = Item('/dashboard/', _('Workspace'), 'flask')
HOSPITAL = Item('/dashboard/', _('Hospital'), 'hospital')
ADMIN = Item('/dashboard/', _('Admin'), 'shield-halved')

MENUS = {
    'patient': [MY_HEALTH, RECORDS, APPOINTMENTS, COMPANIONS,
                AI_TOOLS, ASSISTANT, ANALYTICS, DIVIDER, SETTINGS],
    # No Appointments: that page lists the signed-in user's OWN appointments
    # as a patient. There is no doctor-side calendar yet, and doctors have no
    # health data of their own here.
    'doctor': [MY_PATIENTS,
               AI_TOOLS, SCRIBE, ASSISTANT, DIVIDER, SETTINGS],
    'data_scientist': [WORKSPACE, ANALYTICS, MONITORING,
                       AI_TOOLS, ASSISTANT, DIVIDER, SETTINGS],
    'hospital_admin': [HOSPITAL, MONITORING, DIVIDER, SETTINGS],
    # Monitoring is gated on is_staff/is_superuser for this role, which the
    # role alone does not imply; the admin dashboard links it when allowed.
    'admin': [ADMIN, ANALYTICS, DIVIDER, SETTINGS],
}


def menu_key(user) -> str:
    """Same order of checks as dashboard.views.home, so menu and home page agree."""
    if user.is_patient:
        return 'patient'
    if user.is_doctor:
        return 'doctor'
    if user.is_data_scientist:
        return 'data_scientist'
    if user.is_hospital_admin:
        return 'hospital_admin'
    return 'admin'


def menu_for(user) -> list:
    return MENUS[menu_key(user)]


def home_url(user) -> str:
    """Where the logo and the bare site address take a signed-in user."""
    return MY_HEALTH.href if user.is_patient else '/dashboard/'


#: Research demos, listed only when LABS_ENABLED (see apps/ai_insights/labs.py).
LABS = Item('/insights/models/', _('Labs'), 'flask')
LABS_ROLES = {'patient', 'data_scientist', 'admin'}


def items_for(user) -> list:
    from apps.ai_insights.labs import labs_enabled

    items = list(menu_for(user))
    if labs_enabled() and menu_key(user) in LABS_ROLES:
        at = items.index(DIVIDER) if DIVIDER in items else len(items)
        items.insert(at, LABS)
    return items


def current_href(items, path: str):
    """The menu entry for this page: the longest href that prefixes the path."""
    best = None
    for item in items:
        href = getattr(item, 'href', None)
        if href and path.startswith(href) and (best is None or len(href) > len(best)):
            best = href
    return best


def nav_menu(request):
    """Context processor for the signed-in layout: `nav_menu` (entries),
    `nav_current` (the href to mark aria-current) and `nav_home`."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated):
        return {}
    items = items_for(user)
    return {'nav_menu': items, 'nav_home': home_url(user),
            'nav_current': current_href(items, request.path)}
