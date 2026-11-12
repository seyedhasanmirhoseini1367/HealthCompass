"""
Getting an appointment into the user's own calendar.

Two routes, both started by the user:

* An .ics file (RFC 5545). Works with Apple Calendar, Outlook and Google, and
  carries the reminders the user picked as alarms. It is a download, so
  nothing leaves the site except to the user's own device; it includes notes.
* A Google Calendar "add event" link. That puts the event details in a URL
  sent to Google, so it carries title, doctor and place but never the notes,
  which may hold health details.

Appointments have no end time. Both routes assume one hour, which is what
calendar apps would otherwise guess, and which the user can change there.
"""
from __future__ import annotations

from datetime import timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from django.utils import timezone

DURATION = timedelta(hours=1)
REMINDERS = (('remind_24h', 24 * 60), ('remind_3h', 180),
             ('remind_2h', 120), ('remind_1h', 60))


def _utc(dt) -> str:
    return dt.astimezone(dt_timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _escape(text: str) -> str:
    """RFC 5545 TEXT escaping."""
    return (text.replace('\\', '\\\\').replace(';', '\\;')
                .replace(',', '\\,').replace('\r\n', '\\n').replace('\n', '\\n'))


def _fold(line: str) -> str:
    """Lines longer than 75 octets continue on the next line after a space."""
    raw = line.encode('utf-8')
    if len(raw) <= 75:
        return line
    parts, chunk = [], b''
    for ch in line:
        b = ch.encode('utf-8')
        if len(chunk) + len(b) > (75 if not parts else 74):
            parts.append(chunk.decode('utf-8'))
            chunk = b''
        chunk += b
    parts.append(chunk.decode('utf-8'))
    return '\r\n '.join(parts)


def _description(appt, *, include_notes: bool) -> str:
    lines = []
    if appt.doctor_name:
        lines.append(f'Doctor: {appt.doctor_name}')
    if include_notes and appt.notes:
        lines.append(appt.notes)
    return '\n'.join(lines)


def ics(appt, host: str) -> str:
    start = appt.appointment_datetime
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//HealthCompass//Appointments//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'BEGIN:VEVENT',
        f'UID:{appt.pk}@{host}',
        f'DTSTAMP:{_utc(timezone.now())}',
        f'DTSTART:{_utc(start)}',
        f'DTEND:{_utc(start + DURATION)}',
        f'SUMMARY:{_escape(appt.title)}',
    ]
    if appt.location:
        lines.append(f'LOCATION:{_escape(appt.location)}')
    desc = _description(appt, include_notes=True)
    if desc:
        lines.append(f'DESCRIPTION:{_escape(desc)}')
    if appt.is_cancelled:
        lines.append('STATUS:CANCELLED')
    for field, minutes in REMINDERS:
        if getattr(appt, field):
            lines += ['BEGIN:VALARM', 'ACTION:DISPLAY',
                      f'DESCRIPTION:{_escape(appt.title)}',
                      f'TRIGGER:-PT{minutes}M', 'END:VALARM']
    lines += ['END:VEVENT', 'END:VCALENDAR']
    return '\r\n'.join(_fold(line) for line in lines) + '\r\n'


def google_url(appt) -> str:
    start = appt.appointment_datetime
    params = {
        'action': 'TEMPLATE',
        'text': appt.title,
        'dates': f'{_utc(start)}/{_utc(start + DURATION)}',
    }
    details = _description(appt, include_notes=False)
    if details:
        params['details'] = details
    if appt.location:
        params['location'] = appt.location
    return 'https://calendar.google.com/calendar/render?' + urlencode(params)
