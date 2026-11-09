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

#: Section heading in the menu. A string entry in a menu list renders as one.
AI_TOOLS = 'AI tools'
#: A horizontal rule.
DIVIDER = '---'


@dataclass(frozen=True)
class Item:
    href: str
    label: str
    icon: str  # Font Awesome name without the "fa-" prefix


MY_HEALTH = Item('/dashboard/health/', 'My Health', 'heart-pulse')
RECORDS = Item('/records/', 'Records', 'file-medical')
APPOINTMENTS = Item('/appointments/', 'Appointments', 'calendar-check')
COMPANIONS = Item('/care/', 'Companions', 'hand-holding-heart')
ASSISTANT = Item('/assistant/', 'Assistant', 'robot')
ANALYTICS = Item('/insights/', 'Analytics', 'brain')
SCRIBE = Item('/scribe/', 'Scribe', 'microphone')
MONITORING = Item('/dashboard/monitoring/', 'Monitoring', 'chart-line')
SETTINGS = Item('/dashboard/settings/', 'Settings', 'gear')

#: Each role's home page. /dashboard/ renders the right one per role.
MY_PATIENTS = Item('/dashboard/', 'My Patients', 'user-doctor')
WORKSPACE = Item('/dashboard/', 'Workspace', 'flask')
HOSPITAL = Item('/dashboard/', 'Hospital', 'hospital')
ADMIN = Item('/dashboard/', 'Admin', 'shield-halved')

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


def nav_menu(request):
    """Context processor: `nav_menu` and `nav_home` for the signed-in layout."""
    user = getattr(request, 'user', None)
    if not (user and user.is_authenticated):
        return {}
    return {'nav_menu': menu_for(user), 'nav_home': home_url(user)}
