"""
Consent service — the single place that decides whether a processing activity
is permitted for a user.

Everything that needs to know "may I do this with this person's health data?"
asks has_consent(). Keeping that in one function means the version rule and the
default-deny rule cannot drift between callers.

Logging policy: this module never logs which purposes a user has or has not
consented to. Consent choices are themselves sensitive (declining research
participation, for instance, is personal information), and PLAN_STATUS §19
requires that no such content reaches ordinary logs. Only anonymous counters or
user ids without purposes may be logged, and currently nothing is.
"""
from typing import Dict, List

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Consent, ConsentPurpose


class ConsentRequired(Exception):
    """
    Raised when an operation needs a consent the user has not granted.

    Carries the machine-readable purpose so callers can render an actionable
    prompt rather than a generic denial.
    """

    def __init__(self, purpose: str, message: str = ''):
        self.purpose = purpose
        self.message = message or (
            'This feature needs your consent before it can run.'
        )
        super().__init__(self.message)


def current_version(purpose: str) -> str:
    """Version of the consent text currently in force for *purpose*."""
    return settings.CONSENT_VERSIONS.get(purpose, 'v1')


def required_purposes() -> List[str]:
    """Purposes whose absence blocks the corresponding feature."""
    return list(getattr(settings, 'CONSENT_REQUIRED_PURPOSES', []))


def has_consent(user, purpose: str) -> bool:
    """
    True only when *user* has an active consent for *purpose* at the current
    version.

    Default-deny: no record means no consent. Consent granted against a
    superseded version does not carry over — that is the whole reason the
    version is stored.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return False

    return Consent.objects.filter(
        user=user,
        purpose=purpose,
        status=Consent.Status.GRANTED,
        revoked_at__isnull=True,
        version=current_version(purpose),
    ).exists()


def require_consent(user, purpose: str) -> None:
    """Raise ConsentRequired unless *user* has granted *purpose*."""
    if not has_consent(user, purpose):
        raise ConsentRequired(purpose, _denial_message(purpose))


def enforce_for_ai(user) -> None:
    """
    Gate for any operation that transmits health data to an external AI provider.

    Checks every purpose listed in CONSENT_REQUIRED_PURPOSES so the set of gates
    is configuration, not scattered call sites.
    """
    for purpose in required_purposes():
        require_consent(user, purpose)


@transaction.atomic
def grant_consent(user, purpose: str) -> Consent:
    """
    Record consent for *purpose* at the current version.

    Re-granting when an active consent already exists at the current version is
    idempotent. If an active consent exists at an OLD version it is revoked
    first, so the audit trail shows the older agreement ending and the newer one
    beginning rather than a row silently changing version.
    """
    if purpose not in ConsentPurpose.values:
        raise ValueError(f'Unknown consent purpose: {purpose}')

    version = current_version(purpose)
    active = (Consent.objects
              .select_for_update()
              .filter(user=user, purpose=purpose,
                      status=Consent.Status.GRANTED, revoked_at__isnull=True)
              .first())

    if active is not None:
        if active.version == version:
            return active
        _revoke(active)

    now = timezone.now()
    return Consent.objects.create(
        user=user, purpose=purpose, version=version,
        status=Consent.Status.GRANTED, granted_at=now,
    )


@transaction.atomic
def revoke_consent(user, purpose: str) -> bool:
    """
    Revoke an active consent. Returns True if something was revoked.

    The row is kept and stamped, never deleted: a revoked consent is evidence
    that consent once existed, which is exactly what an audit needs.
    """
    if purpose not in ConsentPurpose.values:
        raise ValueError(f'Unknown consent purpose: {purpose}')

    active = (Consent.objects
              .select_for_update()
              .filter(user=user, purpose=purpose,
                      status=Consent.Status.GRANTED, revoked_at__isnull=True)
              .first())
    if active is None:
        return False
    _revoke(active)
    return True


def consent_status(user) -> List[Dict]:
    """
    Current state of every purpose for *user*, for display in settings UI.

    Always returns one entry per defined purpose — including purposes never
    acted on — so the UI shows the full set of choices rather than only the
    ones already decided.
    """
    active = {
        c.purpose: c
        for c in Consent.objects.filter(
            user=user, status=Consent.Status.GRANTED, revoked_at__isnull=True,
        )
    }

    rows = []
    for purpose, label in ConsentPurpose.choices:
        version = current_version(purpose)
        record  = active.get(purpose)
        granted = record is not None and record.version == version
        rows.append({
            'purpose':          purpose,
            'label':            label,
            'description':      PURPOSE_DESCRIPTIONS.get(purpose, ''),
            'granted':          granted,
            'current_version':  version,
            'granted_version':  record.version if record else None,
            'granted_at':       record.granted_at if record else None,
            # True when an old-version consent exists: the user did agree once,
            # but to different wording, so the UI should ask again rather than
            # present this as a fresh decision.
            'needs_reconsent':  record is not None and record.version != version,
            'required':         purpose in required_purposes(),
        })
    return rows


def consent_history(user) -> List[Consent]:
    """Full append-only trail for *user*, newest first."""
    return list(Consent.objects.filter(user=user).order_by('-created_at'))


# ── Internal ──────────────────────────────────────────────────────────────────

def _revoke(consent: Consent) -> None:
    consent.status     = Consent.Status.REVOKED
    consent.revoked_at = timezone.now()
    consent.save(update_fields=['status', 'revoked_at', 'updated_at'])


def _denial_message(purpose: str) -> str:
    return DENIAL_MESSAGES.get(
        purpose,
        'This feature needs your consent before it can run. '
        'You can grant it in Privacy & Consent settings.',
    )


PURPOSE_DESCRIPTIONS = {
    ConsentPurpose.AI_PROCESSING: (
        'Lets HealthCompass analyse your records to answer your questions, '
        'summarise documents and show trends.'
    ),
    ConsentPurpose.EXTERNAL_LLM: (
        'The AI assistant is powered by external providers (Groq, Google, '
        'Anthropic, OpenAI). Answering your questions means sending the '
        'relevant parts of your health records to them for processing. '
        'Without this, the AI assistant cannot run.'
    ),
    ConsentPurpose.DOCUMENT_PROCESSING: (
        'Lets HealthCompass read uploaded documents and images to extract lab '
        'values, medications and dates automatically.'
    ),
    ConsentPurpose.DATA_SHARING: (
        'Lets clinicians linked to your account view your records. You can see '
        'every access in your account activity.'
    ),
    ConsentPurpose.RESEARCH: (
        'Optional. Allows anonymised data to be used for research and to '
        'improve the service. Declining changes nothing about your care.'
    ),
}

DENIAL_MESSAGES = {
    ConsentPurpose.EXTERNAL_LLM: (
        'The AI assistant needs your consent to send the relevant parts of your '
        'health records to our external AI providers for processing. '
        'You can grant or withdraw this at any time in Privacy & Consent settings.'
    ),
}
