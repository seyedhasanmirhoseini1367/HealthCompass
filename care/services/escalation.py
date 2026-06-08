"""
Escalation logic for missed check-ins.

Tier thresholds (hours overdue after grace window ends):
  2 →  6 h  — email to caregivers
  3 → 12 h  — email + SMS
  4 → 24 h  — email + SMS (urgent subject)

Run check_patient_escalation() hourly; each tier fires once per patient per day.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


# ── Tier helpers ──────────────────────────────────────────────────────────────

def _hours_overdue(circle):
    """
    Return float hours overdue (after grace window) or None if still in window.
    Uses server's local timezone for comparing usual_checkin_hour.
    """
    now_local = timezone.localtime(timezone.now())

    deadline = now_local.replace(
        hour=circle.usual_checkin_hour,
        minute=0, second=0, microsecond=0
    )
    grace_end = deadline + timedelta(hours=circle.checkin_window_hours)

    if now_local < grace_end:
        return None  # Still inside the grace window — no alert yet

    return (now_local - grace_end).total_seconds() / 3600.0


def _tier_for_hours(h):
    """Map hours overdue → tier (None if below first threshold)."""
    if h >= 24:  return 4
    if h >= 12:  return 3
    if h >= 6:   return 2
    return None


# ── Main per-patient check ────────────────────────────────────────────────────

def check_patient_escalation(patient):
    """
    Evaluate one patient and fire the appropriate escalation tier if not yet sent today.
    Returns a list of EscalationLog objects created.
    """
    from care.models import CareCircle, DailyCheckIn, EscalationLog
    from care.services.notifications import send_missed_checkin_email, send_missed_checkin_sms

    try:
        circle = CareCircle.objects.get(patient=patient, is_active=True)
    except CareCircle.DoesNotExist:
        return []

    today = timezone.localtime(timezone.now()).date()

    # Patient already checked in (even a quick "I'm fine")? → no escalation
    if DailyCheckIn.objects.filter(patient=patient, date=today).exists():
        return []

    hours = _hours_overdue(circle)
    if hours is None:
        return []

    tier = _tier_for_hours(hours)
    if tier is None:
        return []

    # Already sent this tier today?
    if EscalationLog.objects.filter(patient=patient, date=today, tier=tier).exists():
        return []

    created     = []
    now_local   = timezone.localtime(timezone.now())
    caregivers  = circle.caregivers.filter(is_active=True).order_by('notify_priority', 'name')

    for cg in caregivers:
        if cg.in_quiet_hours(now_local):
            logger.info('Skipping %s — quiet hours active', cg.name)
            continue

        # Tier 2+: email
        if tier >= 2 and cg.notify_email and cg.email:
            log = EscalationLog.objects.create(
                patient=patient, caregiver=cg,
                tier=tier, channel='email', date=today,
            )
            try:
                send_missed_checkin_email(cg, patient, circle, tier, today, log)
                created.append(log)
                logger.info('Tier-%s email → %s for %s', tier, cg.email, patient)
            except Exception as exc:
                logger.error('Email failed to %s: %s', cg.email, exc)
                log.delete()

        # Tier 3+: SMS (Twilio, if configured)
        if tier >= 3 and cg.notify_sms and cg.phone_number:
            log = EscalationLog.objects.create(
                patient=patient, caregiver=cg,
                tier=tier, channel='sms', date=today,
            )
            try:
                send_missed_checkin_sms(cg, patient, tier, today, log)
                created.append(log)
                logger.info('Tier-%s SMS → %s for %s', tier, cg.phone_number, patient)
            except Exception as exc:
                logger.error('SMS failed to %s: %s', cg.phone_number, exc)
                log.delete()

    return created


# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summaries():
    """
    Send a daily summary email to every active caregiver whose patient has
    daily_summary_enabled=True.  Fires only during DAILY_SUMMARY_HOUR (default 20).
    Each caregiver receives at most one summary per day.
    """
    from care.models import CareCircle, DailyCheckIn, EscalationLog
    from care.services.notifications import send_daily_summary_email

    cfg          = getattr(settings, 'CARE_ESCALATION', {})
    summary_hour = cfg.get('DAILY_SUMMARY_HOUR', 20)
    now_local    = timezone.localtime(timezone.now())

    if now_local.hour != summary_hour:
        return

    today = now_local.date()

    for circle in CareCircle.objects.filter(is_active=True, daily_summary_enabled=True):
        patient = circle.patient
        checkin = DailyCheckIn.objects.filter(patient=patient, date=today).first()

        for cg in circle.caregivers.filter(is_active=True, notify_email=True):
            if not cg.email:
                continue
            if EscalationLog.objects.filter(
                patient=patient, caregiver=cg,
                date=today, channel='summary_email'
            ).exists():
                continue

            log = EscalationLog.objects.create(
                patient=patient, caregiver=cg,
                tier=0, channel='summary_email', date=today,
            )
            try:
                send_daily_summary_email(cg, patient, checkin)
                logger.info('Daily summary → %s for %s', cg.email, patient)
            except Exception as exc:
                logger.error('Summary failed to %s: %s', cg.email, exc)
                log.delete()
