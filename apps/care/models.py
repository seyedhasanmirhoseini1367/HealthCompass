"""
Care monitoring: what was observed, what it means, and who needs to know.

Why this is its own app
-----------------------
The product assumption behind it is that an older person will NOT open the
application every day and type in how they are. Useful monitoring therefore has
to emerge from things that happen anyway — a device syncing, a reminder being
acknowledged, a reminder being ignored, someone saying "I felt dizzy".

Those are not the same kind of fact, and the central rule of this app is that
they never become the same kind of fact:

    a measured blood pressure          — a device produced a number
    "I felt dizzy this morning"        — a person said something
    a dose reminder nobody answered    — we know nothing about the dose

Collapsing those into one "clinical observation" table would make the third
indistinguishable from the first two, and a system that cannot tell "no data"
from "bad data" will eventually report the absence of an answer as an answer.

So there is deliberately no shared observations table. Passive measurements stay
in `medical_records` (WearableDataPoint, ParsedLabValue); interaction-derived
facts are TaskOccurrence; human-reported ones are PatientReport. The unifying
layer is a query that carries provenance with every row — `care.timeline` —
never a table that has already dropped it.

The chain, and why each arrow is separate
-----------------------------------------
    observed fact  ->  derived signal  ->  policy decision  ->  notification
                                                                    |
                                                       authorization check
                                                                    |
                                                            recipient/channel

Each arrow is a place where information is added by *us* rather than by the
patient or a device, and each one is somewhere a mistake could be laundered into
looking like a measurement. Keeping them as separate rows with links backwards
means every signal can be asked "what made you think that", and answered.

A CareTask is a schedule someone set up, and nothing else derives it. Documents
never create, change or end one: a discharge summary listing a drug is not a
claim that a dose is due at 08:00, and treating it as one would invent a
schedule no clinician wrote.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


# ── Human-reported ────────────────────────────────────────────────────────────

class PatientReport(models.Model):
    """
    Something a person said about how they are.

    Not a measurement and never promoted into one. "I felt dizzy" is evidence
    that the patient reported dizziness — which is genuinely useful — and is not
    evidence of a blood-pressure drop, an arrhythmia, or anything else. Any
    interpretation of it belongs somewhere else with a link back to this row.

    The original wording is kept verbatim. When a report arrives by voice, the
    transcript is what the system heard, which is not always what was said; both
    the audio-derived text and the fact that it came from speech are recorded so
    a reader can weigh it accordingly.
    """

    class Kind(models.TextChoices):
        SYMPTOM     = 'symptom',     'Symptom'
        WELLBEING   = 'wellbeing',   'General wellbeing'
        OBSERVATION = 'observation', 'Other observation'

    class InputMethod(models.TextChoices):
        WEB    = 'web',    'Typed in the app'
        VOICE  = 'voice',  'Spoken'
        PHONE  = 'phone',  'Telephone'
        API    = 'api',    'Mobile app'

    class Reporter(models.TextChoices):
        # Who is speaking matters clinically. "My mother seemed confused today"
        # is a different observation from "I feel confused", and merging them
        # would attribute a caregiver's interpretation to the patient.
        PATIENT   = 'patient',   'The patient'
        CAREGIVER = 'caregiver', 'A caregiver'

    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='care_reports')
    # Who actually said it. SET_NULL so the report survives account deletion as
    # evidence, with reported_by_role still saying whose perspective it was.
    reported_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='care_reports_made')
    reported_by_role = models.CharField(max_length=16, choices=Reporter.choices,
                                        default=Reporter.PATIENT)

    kind         = models.CharField(max_length=16, choices=Kind.choices, default=Kind.SYMPTOM)
    input_method = models.CharField(max_length=8, choices=InputMethod.choices,
                                    default=InputMethod.WEB)

    #: Exactly what was said or typed. Never rewritten — a cleaned-up version is
    #: an interpretation and belongs in its own row.
    text = models.TextField(
        help_text='The report in the reporter\'s own words. Not normalised.')

    #: Present only for voice input: what the recogniser produced, kept even
    #: when `text` was corrected by the speaker, so a mis-hearing is visible.
    transcript = models.TextField(
        blank=True, default='',
        help_text='Raw speech-to-text output, when the report arrived by voice.')
    transcript_confidence = models.FloatField(
        null=True, blank=True,
        help_text='Recogniser confidence, when the provider reports one. NULL '
                  'means unknown — never assume high confidence.')

    #: When the thing being described happened, as opposed to when it was told
    #: to us. "I felt dizzy this morning" reported at 21:00 is a morning event.
    occurred_at = models.DateTimeField(
        null=True, blank=True,
        help_text='When the reporter says it happened. NULL when they did not say.')
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', '-created_at']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} reported by {self.reported_by_role} @ {self.created_at:%Y-%m-%d}'

    @property
    def effective_at(self):
        """When this is about, falling back to when it was recorded."""
        return self.occurred_at or self.created_at


# ── Interaction-derived ───────────────────────────────────────────────────────

class CareTask(models.Model):
    """
    Something expected to happen regularly, that someone can confirm.

    Owned by the patient. A caregiver cannot create one here: setting up a
    medication schedule for someone else is a care decision, and this codebase
    already keeps administrative and caregiving authority separate from the
    patient's own. The seam for caregiver-proposed tasks is deliberately left
    unbuilt rather than half-built.

    `times_of_day` is a plain list of "HH:MM" strings rather than a cron
    expression: the schedules this needs to express are "08:00 and 20:00 every
    day", and a general recurrence language would be a large surface for the
    sake of cases nobody has asked for.
    """

    class Kind(models.TextChoices):
        MEDICATION  = 'medication',  'Medication'
        MEASUREMENT = 'measurement', 'Measurement'
        ACTIVITY    = 'activity',    'Activity'
        OTHER       = 'other',       'Other'

    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='care_tasks')
    kind    = models.CharField(max_length=16, choices=Kind.choices, default=Kind.MEDICATION)

    #: What the patient sees. Kept short and non-clinical where possible: this
    #: string is the one most likely to end up on a lock screen.
    label = models.CharField(max_length=120)

    times_of_day = models.JSONField(
        default=list,
        help_text='List of "HH:MM" local times this is due, e.g. ["08:00", "20:00"].')

    #: How long after `due_at` the system keeps waiting before recording that it
    #: does not know. A product setting, not a clinical one — see MonitoringPolicy.
    grace_minutes = models.PositiveIntegerField(
        default=180,
        help_text='Minutes after the due time before an unanswered occurrence is '
                  'recorded as unconfirmed (NOT as missed).')

    is_active  = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['label']
        indexes = [
            models.Index(fields=['patient', 'is_active']),
        ]

    def __str__(self):
        return f'{self.label} ({self.get_kind_display()})'


class TaskOccurrence(models.Model):
    """
    One scheduled instance, and what became of it.

    The state machine is the point of this whole app.

    UNCONFIRMED is not MISSED. The system knows that nobody answered; it does
    not know whether the dose was taken. A person who takes their tablet and
    puts the phone down produces exactly the same evidence as a person who
    forgot, and telling a worried son "your mother did not take her medication"
    on that evidence is a false statement about his mother's health.

    So the transitions are:

        PENDING       due, still inside the grace window
        UNCONFIRMED   grace elapsed with no answer  — SYSTEM-derived, means
                      "we do not know", and never means "not taken"
        CONFIRMED     the person said they did it
        SKIPPED       the person said they deliberately did not
        MISSED        the person said they forgot or could not

    Only the person can produce CONFIRMED, SKIPPED or MISSED. The system can
    only ever produce UNCONFIRMED. That asymmetry is enforced in `resolve()`
    and `mark_unconfirmed()` rather than left to callers.
    """

    class State(models.TextChoices):
        PENDING     = 'pending',     'Waiting'
        CONFIRMED   = 'confirmed',   'Confirmed done'
        SKIPPED     = 'skipped',     'Deliberately skipped'
        MISSED      = 'missed',      'Reported as missed'
        UNCONFIRMED = 'unconfirmed', 'No response — unknown'

    #: The states a human can put an occurrence into. The system may not.
    HUMAN_STATES = (State.CONFIRMED, State.SKIPPED, State.MISSED)

    #: States that mean the question was answered, whatever the answer was.
    ANSWERED = HUMAN_STATES

    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task    = models.ForeignKey(CareTask, on_delete=models.CASCADE, related_name='occurrences')
    # Denormalised owner, derived from the task in save(). Same reasoning as
    # ParsedLabValue.patient: isolation must not depend on every caller
    # remembering to join through the parent.
    patient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='care_occurrences')

    due_at = models.DateTimeField()
    state  = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)

    #: How the answer arrived. A tap and a spoken "done" are the same answer,
    #: but not the same evidence, and voice recognition can mishear.
    responded_at   = models.DateTimeField(null=True, blank=True)
    response_input = models.CharField(
        max_length=8, choices=PatientReport.InputMethod.choices, blank=True, default='')
    #: Who answered. A caregiver confirming on the patient's behalf is a weaker
    #: fact than the patient confirming, and must stay distinguishable.
    responded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='care_responses')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-due_at']
        constraints = [
            # One occurrence per task per due time. Without this a re-run of the
            # generator would double every dose in the caregiver's view and make
            # a compliant patient look erratic.
            models.UniqueConstraint(fields=['task', 'due_at'],
                                    name='unique_occurrence_per_task_time'),
        ]
        indexes = [
            models.Index(fields=['patient', '-due_at']),
            models.Index(fields=['state', 'due_at']),
        ]

    def __str__(self):
        return f'{self.task.label} due {self.due_at:%Y-%m-%d %H:%M} [{self.state}]'

    def save(self, *args, **kwargs):
        if self.task_id and self.patient_id != self.task.patient_id:
            self.patient_id = self.task.patient_id
        super().save(*args, **kwargs)

    # ── Transitions ───────────────────────────────────────────────────────────

    def resolve(self, state: str, *, by, input_method: str = PatientReport.InputMethod.WEB):
        """
        Record a human answer.

        Refuses anything outside HUMAN_STATES so that no caller can talk the
        system into asserting a person's behaviour on their behalf.
        """
        if state not in self.HUMAN_STATES:
            raise ValueError(
                f'{state!r} is not something a person can report. Only '
                f'{", ".join(self.HUMAN_STATES)} come from a human; the system '
                f'may only record "unconfirmed".')

        self.state          = state
        self.responded_at   = timezone.now()
        self.responded_by   = by
        self.response_input = input_method
        self.save(update_fields=['state', 'responded_at', 'responded_by',
                                 'response_input'])
        return self

    def mark_unconfirmed(self):
        """
        Record that the grace window closed with no answer.

        Only ever from PENDING. An occurrence someone already answered must not
        be overwritten by a sweep that ran late — the person's answer outranks
        the system's silence, always.
        """
        if self.state != self.State.PENDING:
            return False
        self.state = self.State.UNCONFIRMED
        self.save(update_fields=['state'])
        return True

    @property
    def is_answered(self) -> bool:
        return self.state in self.ANSWERED

    @property
    def is_overdue(self) -> bool:
        from datetime import timedelta
        deadline = self.due_at + timedelta(minutes=self.task.grace_minutes)
        return self.state == self.State.PENDING and timezone.now() > deadline


# ── Derived ───────────────────────────────────────────────────────────────────

class MonitoringSignal(models.Model):
    """
    A pattern worth someone's attention — derived, and traceable back.

    A signal is the first thing in this chain that the system asserts rather
    than observes, so it carries its evidence. `occurrences` is the actual set
    of rows the rule looked at, not a count copied out of them: a caregiver
    asking "which three?" gets the three, and a signal whose evidence has been
    deleted stops being able to justify itself, which is correct.

    Deliberately NOT a clinical finding. `REPEATED_UNCONFIRMED` says the app got
    no answer three times. It does not say the patient is unwell, non-adherent,
    or at risk — those are inferences that need a clinician, and stating them
    here would put a diagnosis in a row that only ever watched a button.
    """

    class Kind(models.TextChoices):
        REPEATED_UNCONFIRMED = 'repeated_unconfirmed', 'Repeated unconfirmed tasks'
        REPORTED_SYMPTOM     = 'reported_symptom',     'Patient reported a symptom'
        REPORTED_MISSED      = 'reported_missed',      'Patient reported missing a task'

    class Severity(models.TextChoices):
        # About how much attention it warrants, not about how ill anyone is.
        INFO      = 'info',      'For information'
        ATTENTION = 'attention', 'Worth a look'
        URGENT    = 'urgent',    'Needs attention now'

    id      = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='care_signals')
    kind     = models.CharField(max_length=32, choices=Kind.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices,
                                default=Severity.ATTENTION)

    #: The window the rule considered, so a reader can tell a signal about this
    #: week from one about last month without inferring it from created_at.
    window_start = models.DateTimeField()
    window_end   = models.DateTimeField()

    #: The evidence. CASCADE-free by design: these are the rows the rule read,
    #: and if they go, the signal's justification goes with them.
    occurrences = models.ManyToManyField(TaskOccurrence, blank=True,
                                         related_name='signals')
    reports     = models.ManyToManyField(PatientReport, blank=True,
                                         related_name='signals')

    #: Which task or subject this is about, for deduplication. Not a label shown
    #: to anyone — see the notification layer for what recipients actually read.
    subject_key = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Stable key for the thing this is about, used to avoid raising '
                  'the same signal twice while it is still true.')

    #: The rule that produced it, by name, so a signal raised under an old
    #: policy stays attributable after the policy changes.
    rule       = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    #: Set when the underlying situation stops being true, rather than deleting:
    #: "this was raised and then resolved" is itself worth keeping.
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['patient', '-created_at']),
            models.Index(fields=['patient', 'kind', 'subject_key', 'resolved_at']),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} [{self.severity}] for {self.patient_id}'

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def evidence_count(self) -> int:
        return self.occurrences.count() + self.reports.count()
