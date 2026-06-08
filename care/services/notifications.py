"""
Notification senders for the escalation system.
Email always available (Django's mail framework).
SMS via Twilio — silently skipped when credentials not configured.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _site_url():
    return getattr(settings, 'SITE_URL', 'http://localhost:8000').rstrip('/')


def _patient_name(patient):
    name = (patient.get_full_name() or '').strip()
    return name if name else patient.username


def _ack_url(log):
    return f'{_site_url()}/care/ack/{log.acknowledge_token}/'


def _dashboard_url(caregiver):
    return f'{_site_url()}/care/view/{caregiver.access_token}/'


def _quick_url(circle):
    return f'{_site_url()}/care/quick/{circle.quick_token}/'


TIER_CONFIG = {
    2: {'emoji': 'ℹ️',  'color': '#0ea5e9', 'label': '6+ hours with no check-in today'},
    3: {'emoji': '⚠️',  'color': '#f59e0b', 'label': '12+ hours with no check-in — please follow up'},
    4: {'emoji': '🚨',  'color': '#ef4444', 'label': '24+ hours — URGENT — immediate attention needed'},
}


# ── Email senders ─────────────────────────────────────────────────────────────

def send_missed_checkin_email(caregiver, patient, circle, tier, checkin_date, log):
    """Send a missed check-in alert email to a single caregiver."""
    cfg          = TIER_CONFIG.get(tier, TIER_CONFIG[2])
    patient_name = _patient_name(patient)

    if tier >= 4:
        subject = f"🚨 URGENT — {patient_name} — 24h+ with no check-in"
    else:
        subject = f"{cfg['emoji']} {patient_name} hasn't checked in today"

    context = {
        'caregiver_name': caregiver.name,
        'patient_name':   patient_name,
        'tier':           tier,
        'tier_emoji':     cfg['emoji'],
        'tier_color':     cfg['color'],
        'tier_label':     cfg['label'],
        'checkin_date':   checkin_date,
        'ack_url':        _ack_url(log),
        'dashboard_url':  _dashboard_url(caregiver),
        'quick_url':      _quick_url(circle),
    }

    send_mail(
        subject=subject,
        message=render_to_string('care/email/missed_checkin.txt',  context),
        html_message=render_to_string('care/email/missed_checkin.html', context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[caregiver.email],
        fail_silently=False,
    )


def send_missed_checkin_sms(caregiver, patient, tier, checkin_date, log):
    """Send a missed check-in alert SMS via Twilio."""
    try:
        from twilio.rest import Client
    except ImportError:
        raise RuntimeError('Twilio not installed — run: pip install twilio')

    sid      = getattr(settings, 'TWILIO_ACCOUNT_SID',  '')
    token    = getattr(settings, 'TWILIO_AUTH_TOKEN',   '')
    from_num = getattr(settings, 'TWILIO_FROM_NUMBER',  '')

    if not all([sid, token, from_num]):
        raise RuntimeError('Twilio credentials not configured (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM_NUMBER)')

    patient_name  = _patient_name(patient)
    dashboard_url = _dashboard_url(caregiver)
    hours_str     = {3: '12', 4: '24+'}.get(tier, '?')
    emoji         = '🚨' if tier >= 4 else '⚠️'

    body = (
        f"{emoji} {patient_name} hasn't checked in for {hours_str}+ hours.\n"
        f"View dashboard: {dashboard_url}"
    )

    Client(sid, token).messages.create(
        body=body,
        from_=from_num,
        to=caregiver.phone_number,
    )


def send_daily_summary_email(caregiver, patient, checkin):
    """Send daily summary — works whether or not the patient checked in."""
    patient_name = _patient_name(patient)
    has_checkin  = checkin is not None

    subject = (
        f"✅ {patient_name}'s daily summary — checked in today"
        if has_checkin else
        f"📋 {patient_name}'s daily summary — no check-in today"
    )

    context = {
        'caregiver_name': caregiver.name,
        'patient_name':   patient_name,
        'checkin':        checkin,
        'has_checkin':    has_checkin,
        'dashboard_url':  _dashboard_url(caregiver),
    }

    send_mail(
        subject=subject,
        message=render_to_string('care/email/daily_summary.txt',  context),
        html_message=render_to_string('care/email/daily_summary.html', context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[caregiver.email],
        fail_silently=False,
    )
