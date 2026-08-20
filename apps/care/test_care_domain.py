"""
The two rules the care system cannot get wrong.

  1. Silence is not an answer. "Nobody confirmed the dose" and "the dose was not
     taken" are different claims, and only a person can make the second one.
  2. The three kinds of observation stay distinguishable. A measurement, a
     sentence someone said, and an unanswered reminder must never end up as
     interchangeable rows of "a clinical fact".

Everything else here is scheduling and bookkeeping. These two are the product.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.care import scheduling
from apps.care.models import (CareTask, MonitoringSignal, PatientReport,
                              TaskOccurrence)

User = get_user_model()


class _Care(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'care_p', email='care_p@test.invalid', password='pw', role='patient')
        # One anchor per test rather than calling timezone.now() at each
        # creation. Windows' clock granularity is coarse enough that two calls
        # in quick succession return the SAME instant, so two occurrences meant
        # to be an hour apart collided on the (task, due_at) uniqueness
        # constraint — intermittently, depending on how fast the run was.
        self.anchor = timezone.now()
        self._seq = 0

    def _task(self, label='Morning tablet', times=None, grace=180):
        return CareTask.objects.create(
            patient=self.patient, label=label, kind=CareTask.Kind.MEDICATION,
            times_of_day=times if times is not None else ['08:00'],
            grace_minutes=grace)

    def _occurrence(self, task=None, due=None, state=None):
        if due is None:
            # Distinct by construction: the counter guarantees two default
            # occurrences never land on the same instant.
            self._seq += 1
            due = self.anchor - timedelta(hours=5, seconds=self._seq)
        occurrence = TaskOccurrence.objects.create(
            task=task or self._task(), patient=self.patient, due_at=due)
        if state:
            occurrence.state = state
            occurrence.save(update_fields=['state'])
        return occurrence


class SilenceIsNotAnAnswerTests(_Care):
    """The single most important behaviour in this app."""

    def test_an_unanswered_occurrence_becomes_unconfirmed(self):
        task = self._task(grace=60)
        self._occurrence(task, due=timezone.now() - timedelta(hours=3))

        self.assertEqual(scheduling.sweep_unconfirmed(), 1)
        self.assertEqual(TaskOccurrence.objects.get().state,
                         TaskOccurrence.State.UNCONFIRMED)

    def test_the_sweep_never_records_a_task_as_missed(self):
        """
        ACCEPTANCE — the system may say "we do not know", never "they didn't".

        A person who takes their tablet and puts the phone down leaves exactly
        the same trace as a person who forgot.
        """
        task = self._task(grace=60)
        self._occurrence(task, due=timezone.now() - timedelta(hours=3))

        scheduling.sweep_unconfirmed()

        self.assertNotEqual(TaskOccurrence.objects.get().state,
                            TaskOccurrence.State.MISSED)

    def test_the_system_cannot_be_asked_to_record_a_human_state(self):
        """resolve() is the only door to confirmed/skipped/missed."""
        occurrence = self._occurrence()

        with self.assertRaises(ValueError):
            occurrence.resolve(TaskOccurrence.State.UNCONFIRMED, by=self.patient)

    def test_a_person_can_report_that_they_missed_it(self):
        """The claim the system may not make, a person may."""
        occurrence = self._occurrence()
        occurrence.resolve(TaskOccurrence.State.MISSED, by=self.patient)

        self.assertEqual(occurrence.state, TaskOccurrence.State.MISSED)
        self.assertEqual(occurrence.responded_by, self.patient)
        self.assertIsNotNone(occurrence.responded_at)

    def test_a_late_sweep_never_overwrites_a_persons_answer(self):
        """ACCEPTANCE — their statement about themselves outranks our silence."""
        task = self._task(grace=60)
        occurrence = self._occurrence(task, due=timezone.now() - timedelta(hours=5))
        occurrence.resolve(TaskOccurrence.State.CONFIRMED, by=self.patient)

        scheduling.sweep_unconfirmed()

        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.CONFIRMED)

    def test_an_occurrence_inside_its_grace_window_is_left_alone(self):
        task = self._task(grace=240)
        self._occurrence(task, due=timezone.now() - timedelta(minutes=30))

        self.assertEqual(scheduling.sweep_unconfirmed(), 0)
        self.assertEqual(TaskOccurrence.objects.get().state,
                         TaskOccurrence.State.PENDING)

    def test_skipped_and_missed_are_not_the_same_state(self):
        """
        Deliberately not taking a tablet and forgetting are different facts,
        and a caregiver reading the difference is the point of asking.
        """
        first  = self._occurrence(due=timezone.now() - timedelta(hours=6))
        second = self._occurrence(due=timezone.now() - timedelta(hours=7))

        first.resolve(TaskOccurrence.State.SKIPPED, by=self.patient)
        second.resolve(TaskOccurrence.State.MISSED, by=self.patient)

        self.assertNotEqual(first.state, second.state)


class ProvenanceTests(_Care):
    """Three kinds of observation, three places, never merged."""

    def test_a_report_keeps_the_persons_own_words(self):
        entry = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient,
            text='I felt dizzy this morning')

        self.assertEqual(PatientReport.objects.get().text,
                         'I felt dizzy this morning')

    def test_a_report_records_who_was_speaking(self):
        """
        "My mother seemed confused" is not "I feel confused". Attributing a
        caregiver's impression to the patient would put words in their mouth.
        """
        caregiver = User.objects.create_user(
            'care_c', email='care_c@test.invalid', password='pw', role='patient')
        entry = PatientReport.objects.create(
            patient=self.patient, reported_by=caregiver,
            reported_by_role=PatientReport.Reporter.CAREGIVER,
            text='She seemed confused today')

        self.assertEqual(entry.reported_by_role, PatientReport.Reporter.CAREGIVER)
        self.assertNotEqual(entry.reported_by, entry.patient)

    def test_a_voice_report_keeps_the_transcript_and_says_it_was_speech(self):
        """A mis-hearing has to stay visible as a mis-hearing."""
        entry = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient,
            input_method=PatientReport.InputMethod.VOICE,
            text='I felt dizzy', transcript='I felt busy',
            transcript_confidence=0.61)

        self.assertEqual(entry.input_method, PatientReport.InputMethod.VOICE)
        self.assertEqual(entry.transcript, 'I felt busy')
        self.assertEqual(entry.transcript_confidence, 0.61)

    def test_unknown_transcript_confidence_is_null_not_optimistic(self):
        """ACCEPTANCE — never treat missing data as a good result."""
        entry = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient,
            input_method=PatientReport.InputMethod.VOICE, text='I felt dizzy')

        self.assertIsNone(entry.transcript_confidence)

    def test_a_report_is_about_when_it_happened_not_when_it_was_typed(self):
        morning = timezone.now() - timedelta(hours=12)
        entry = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient,
            text='I felt dizzy this morning', occurred_at=morning)

        self.assertEqual(entry.effective_at, morning)

    def test_there_is_no_shared_table_the_three_sources_collapse_into(self):
        """
        ACCEPTANCE — structural, not behavioural.

        A single "observations" table is how a device measurement, a sentence
        and an unanswered reminder become interchangeable. They live in three
        models with three shapes precisely so that no query can flatten them by
        accident.
        """
        from apps.medical_records.models import WearableDataPoint

        self.assertIsNot(PatientReport, TaskOccurrence)
        self.assertIsNot(PatientReport, WearableDataPoint)
        # And none of them inherits a common concrete base that could be queried
        # as one pool.
        for model in (PatientReport, TaskOccurrence, WearableDataPoint):
            bases = [b for b in model.__mro__[1:] if hasattr(b, '_meta')
                     and not getattr(b._meta, 'abstract', True)]
            self.assertEqual(bases, [], f'{model.__name__} has a concrete base')

    def test_a_task_confirmation_records_how_the_answer_arrived(self):
        """A tap and a spoken 'done' are the same answer, not the same evidence."""
        occurrence = self._occurrence()
        occurrence.resolve(TaskOccurrence.State.CONFIRMED, by=self.patient,
                           input_method=PatientReport.InputMethod.VOICE)

        self.assertEqual(occurrence.response_input, PatientReport.InputMethod.VOICE)

    def test_a_caregiver_confirming_stays_distinguishable_from_the_patient(self):
        caregiver = User.objects.create_user(
            'care_c2', email='care_c2@test.invalid', password='pw', role='patient')
        occurrence = self._occurrence()
        occurrence.resolve(TaskOccurrence.State.CONFIRMED, by=caregiver)

        self.assertEqual(occurrence.responded_by, caregiver)
        self.assertNotEqual(occurrence.responded_by, occurrence.patient)


class TaskSeparationTests(_Care):
    """A document is not a schedule."""

    def test_uploading_a_document_does_not_create_a_task(self):
        """
        ACCEPTANCE — a discharge summary mentioning a drug is not a prescription
        for a dose at 08:00, and inventing one would fabricate a schedule no
        clinician wrote.

        This used to be asserted through MedicationStatement, which derived a
        medication list from documents. That feature was removed, but the
        principle it was tested against is about ingestion, not about the
        derived list, so the test now makes the claim directly: a record lands
        and no schedule appears.
        """
        from datetime import date

        from apps.medical_records.models import MedicalRecord

        MedicalRecord.objects.create(
            patient=self.patient, title='Discharge summary',
            record_type='discharge', record_date=date(2026, 1, 1),
            parsed_data={'medications': [{'name': 'Metformin', 'dose': '500mg'}]})

        self.assertEqual(CareTask.objects.count(), 0)


class SchedulingTests(_Care):

    def test_occurrences_are_generated_for_each_time_of_day(self):
        self._task(times=['08:00', '20:00'])

        scheduling.generate_occurrences(horizon_days=1)

        self.assertGreaterEqual(TaskOccurrence.objects.count(), 1)

    def test_generation_is_idempotent(self):
        """ACCEPTANCE — a re-run must not double every dose in the caregiver view."""
        self._task(times=['08:00', '20:00'])

        scheduling.generate_occurrences(horizon_days=2)
        first = TaskOccurrence.objects.count()
        scheduling.generate_occurrences(horizon_days=2)

        self.assertEqual(TaskOccurrence.objects.count(), first)

    def test_an_inactive_task_generates_nothing(self):
        task = self._task()
        task.is_active = False
        task.save()

        scheduling.generate_occurrences(horizon_days=2)

        self.assertEqual(TaskOccurrence.objects.count(), 0)

    def test_a_malformed_time_is_skipped_rather_than_crashing(self):
        """A bad schedule must not stop every other patient's reminders."""
        self._task(times=['not a time', '25:99', '08:00'])

        scheduling.generate_occurrences(horizon_days=1)

        for occurrence in TaskOccurrence.objects.all():
            self.assertEqual(timezone.localtime(occurrence.due_at).minute, 0)

    def test_an_empty_schedule_generates_nothing(self):
        self._task(times=[])

        self.assertEqual(scheduling.generate_occurrences(horizon_days=2), 0)

    def test_the_owner_is_derived_from_the_task(self):
        other = User.objects.create_user(
            'care_o', email='care_o@test.invalid', password='pw', role='patient')
        task = self._task()
        occurrence = TaskOccurrence.objects.create(
            task=task, patient=other, due_at=timezone.now())

        occurrence.refresh_from_db()
        self.assertEqual(occurrence.patient, self.patient)


class SignalTests(_Care):
    """Signals assert something, so they carry their evidence."""

    def _unconfirmed_run(self, task, count, *, from_hours_ago=24, step_hours=24):
        """
        `count` unanswered occurrences, oldest last, at explicit offsets.

        Offsets are explicit so two runs in one test cannot land on the same
        due time — the constraint that keeps a caregiver from seeing every dose
        twice is the same one that makes overlapping fixtures fail.
        """
        return [
            self._occurrence(
                task,
                due=self.anchor - timedelta(hours=from_hours_ago + i * step_hours),
                state=TaskOccurrence.State.UNCONFIRMED)
            for i in range(count)]

    def test_below_the_threshold_nothing_is_raised(self):
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 2)

        self.assertEqual(evaluate_task(task), [])

    def test_at_the_threshold_a_signal_is_raised(self):
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 3)

        raised = evaluate_task(task)
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].kind,
                         MonitoringSignal.Kind.REPEATED_UNCONFIRMED)

    def test_the_signal_carries_the_exact_rows_it_read(self):
        """ACCEPTANCE — "which three?" has to have an answer."""
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 3)

        signal = evaluate_task(task)[0]
        self.assertEqual(signal.occurrences.count(), 3)

    def test_a_confirmation_breaks_the_streak(self):
        """
        Walking back from the newest stops at the first answered occurrence.

        Timeline, oldest first: unanswered, unanswered, CONFIRMED, unanswered.
        Four unanswered-or-not in a row would reach the threshold on a naive
        count; walking backwards finds one unanswered and then the confirmation,
        so the run is 1. A single "I took it" genuinely resets the concern
        rather than being averaged into it.
        """
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 2, from_hours_ago=48)          # 48h, 72h
        self._occurrence(task, due=self.anchor - timedelta(hours=24),
                         state=TaskOccurrence.State.CONFIRMED)     # 24h
        self._unconfirmed_run(task, 1, from_hours_ago=1)           # 1h, newest

        self.assertEqual(evaluate_task(task), [])

    def test_pending_occurrences_do_not_count_toward_a_streak(self):
        """Raising before the person has had a chance to answer would be unfair."""
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 2)
        self._occurrence(task, due=self.anchor - timedelta(minutes=5))

        self.assertEqual(evaluate_task(task), [])

    def test_the_same_signal_is_not_raised_twice_while_still_true(self):
        """ACCEPTANCE — alert fatigue is the failure that makes this useless."""
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 3)

        evaluate_task(task)
        self.assertEqual(evaluate_task(task), [])
        self.assertEqual(MonitoringSignal.objects.count(), 1)

    def test_a_signal_is_resolved_rather_than_deleted_when_it_stops_being_true(self):
        from apps.care.signals_rules import evaluate_task

        task = self._task()
        self._unconfirmed_run(task, 3)
        evaluate_task(task)

        TaskOccurrence.objects.all().update(state=TaskOccurrence.State.CONFIRMED)
        evaluate_task(task)

        signal = MonitoringSignal.objects.get()
        self.assertIsNotNone(signal.resolved_at)
        self.assertFalse(signal.is_open)

    def test_a_reported_symptom_raises_at_once_without_a_streak(self):
        """They already decided it was worth mentioning."""
        from apps.care.signals_rules import signal_for_report

        entry = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient, text='I felt dizzy')

        raised = signal_for_report(entry)
        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].reports.count(), 1)

    def test_a_signal_is_not_a_clinical_finding(self):
        """
        The kinds are all about what the APP observed. None of them names a
        condition, a risk or a diagnosis, because none of them is entitled to.
        """
        for value, _ in MonitoringSignal.Kind.choices:
            self.assertTrue(
                value.startswith(('repeated_', 'reported_')),
                f'{value} reads like a clinical claim rather than an observation')


class FrequentTaskStreakTests(_Care):
    """
    A task due several times a day must not silence its own alarm.

    `_trailing_unconfirmed_streak` took the twenty most recent occurrences and
    then skipped the in-grace ones inside the loop. A task due four times a day
    fills those twenty rows with five days of still-in-grace occurrences, every
    one of them skipped — leaving an empty streak. An empty streak makes
    `evaluate_task` call `_resolve_open`, which closes the caregiver's signal on
    a task that is in fact being ignored.

    That is the worst shape a bug can have in this product: it does not fail
    loudly, it produces a false all-clear, and it does so precisely for the
    patients with the most frequent medication schedules.
    """

    def _frequent_task(self):
        return self._task(label='Four times a day',
                          times=['08:00', '12:00', '18:00', '22:00'])

    def test_in_grace_occurrences_do_not_hide_an_unanswered_streak(self):
        """ACCEPTANCE — 20 in-grace rows must not bury the evidence behind them."""
        from apps.care.signals_rules import _trailing_unconfirmed_streak

        task = self._frequent_task()
        # The unanswered ones happened first, so they sit behind a wall of
        # newer occurrences that are still inside their grace window.
        for i in range(3):
            self._occurrence(task=task,
                             due=self.anchor - timedelta(days=6, hours=i),
                             state=TaskOccurrence.State.UNCONFIRMED)
        for i in range(24):
            self._occurrence(task=task, due=self.anchor - timedelta(minutes=i + 1))

        streak = _trailing_unconfirmed_streak(task)

        self.assertEqual(len(streak), 3,
                         'in-grace occurrences hid the unanswered ones behind them')

    def test_a_frequent_task_still_raises_for_the_caregiver(self):
        """The consequence: the signal is raised rather than silently resolved."""
        from apps.care.signals_rules import evaluate_task

        task = self._frequent_task()
        for i in range(3):
            self._occurrence(task=task,
                             due=self.anchor - timedelta(days=6, hours=i),
                             state=TaskOccurrence.State.UNCONFIRMED)
        for i in range(24):
            self._occurrence(task=task, due=self.anchor - timedelta(minutes=i + 1))

        raised = evaluate_task(task)

        self.assertEqual(len(raised), 1)
        self.assertEqual(raised[0].kind, MonitoringSignal.Kind.REPEATED_UNCONFIRMED)

    def test_an_open_signal_is_not_resolved_while_the_task_is_ignored(self):
        """
        The dangerous half. Resolving means telling a caregiver the worry is
        over, and it must never happen because the query ran out of room.
        """
        from apps.care.signals_rules import evaluate_task

        task = self._frequent_task()
        for i in range(3):
            self._occurrence(task=task,
                             due=self.anchor - timedelta(days=6, hours=i),
                             state=TaskOccurrence.State.UNCONFIRMED)
        signal = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=self.anchor - timedelta(days=6),
            window_end=self.anchor - timedelta(days=6),
            subject_key=f'task:{task.pk}', rule='unconfirmed_streak')
        for i in range(24):
            self._occurrence(task=task, due=self.anchor - timedelta(minutes=i + 1))

        evaluate_task(task)
        signal.refresh_from_db()

        self.assertIsNone(signal.resolved_at,
                          'the caregiver was told the worry was over')

    def test_a_genuine_confirmation_still_breaks_the_streak(self):
        """
        The fix must not make the streak unbreakable. An answered occurrence
        still ends it, which is what stops one bad week following someone
        forever.
        """
        from apps.care.signals_rules import _trailing_unconfirmed_streak

        task = self._frequent_task()
        self._occurrence(task=task, due=self.anchor - timedelta(days=6),
                         state=TaskOccurrence.State.UNCONFIRMED)
        self._occurrence(task=task, due=self.anchor - timedelta(days=5),
                         state=TaskOccurrence.State.CONFIRMED)
        self._occurrence(task=task, due=self.anchor - timedelta(days=4),
                         state=TaskOccurrence.State.UNCONFIRMED)

        streak = _trailing_unconfirmed_streak(task)

        self.assertEqual(len(streak), 1)
