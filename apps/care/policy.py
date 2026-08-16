"""
When the system decides something is worth attention — and on whose authority.

Every number in this file is a PRODUCT decision about how much interruption a
family is willing to tolerate. None of them is a clinical threshold, and none of
them is derived from evidence about medication adherence, falls, or outcomes.
That distinction is not pedantry: a number that looks clinical gets quoted as
though a clinician chose it, and nobody here did.

Where a real clinical rule is eventually wanted — "three missed doses of THIS
drug matters more than three of that one" — it needs a source and a named person
who stands behind it. Until then the rules only count events and say so plainly:
"no answer three times" is a fact about the app, not about the patient.

Everything is overridable per deployment through settings, so a family that
wants to hear more or less can be accommodated without editing code and without
anyone re-deriving a threshold.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class CarePolicy:
    """
    The thresholds, with the reasoning for each default written down.

    Read through `current()` rather than constructed directly, so a deployment
    override applies everywhere at once.
    """

    #: How many consecutive unanswered occurrences of the SAME task before a
    #: caregiver is told.
    #:
    #: Three, because one is noise — people put the phone down — and two is
    #: still comfortably explained by a busy day. It is a guess about tolerance
    #: for interruption, not a claim that three is clinically meaningful, and
    #: the notification wording says exactly what happened rather than implying
    #: three has significance.
    unconfirmed_streak_for_caregiver: int = 3

    #: Before that, the patient themselves is reminded. Reminding the person who
    #: can actually act is both the least intrusive step and the one most likely
    #: to resolve it, so it comes first.
    unconfirmed_streak_for_patient: int = 1

    #: How long the same signal stays "already raised" before it may be raised
    #: again. Without this, a task unanswered for a week generates a caregiver
    #: notification every single day and the caregiver stops reading them —
    #: which is the failure mode that makes the whole feature useless.
    resignal_cooldown_hours: int = 24

    #: A patient reporting a symptom is surfaced to a caregiver only when the
    #: patient has chosen to share it. Defaults to ON because someone typing
    #: "I feel dizzy" into a care app is usually asking to be heard — but it is
    #: a setting, because that is an assumption about people, not a fact.
    notify_caregiver_on_reported_symptom: bool = True

    #: A patient explicitly saying "I missed it" is a stronger fact than silence
    #: and needs no streak: they have told us something happened.
    notify_caregiver_on_reported_missed: bool = True

    @classmethod
    def current(cls) -> 'CarePolicy':
        override = getattr(settings, 'CARE_POLICY', None) or {}
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(override) - known
        if unknown:
            # Loud rather than ignored: a misspelled key in settings would
            # otherwise silently leave the default in place, and the operator
            # would believe they had changed the behaviour.
            raise ValueError(
                f'CARE_POLICY has unknown key(s): {", ".join(sorted(unknown))}. '
                f'Known keys: {", ".join(sorted(known))}.')
        return cls(**override)


def policy() -> CarePolicy:
    return CarePolicy.current()
