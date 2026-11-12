"""
Deduplication under concurrency, and the invariants the review found were only
promised rather than held.

The dedupe layers in this pipeline are the whole defence against alert fatigue.
Both were filter-then-create: read, decide nothing exists, write. Two care
cycles running at once — a cron retry, an overlapping manual run, a worker that
restarted — both pass the check and both write. The result is two events for one
situation, and two events mean two rounds of delivery to the same caregiver.

`NotificationDelivery`'s unique constraint does not catch this. It is unique on
(event, recipient, channel), and in this race the events differ, so both rounds
look entirely legitimate to it.

These tests run the race for real, with threads and a shared database, because a
race that is only reasoned about is a race that is only sometimes fixed.
"""
from __future__ import annotations

import itertools
import threading
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts.models import SharingGrant
from apps.care.models import CareTask, MonitoringSignal, TaskOccurrence
from apps.notifications.dispatch import dispatch_signal, event_for_signal
from apps.notifications.events import NotificationDelivery, NotificationEvent

User = get_user_model()


#: Occurrences are unique on (task, due_at), and these tests deliberately build
#: several signals against ONE task — that is what makes them share a dedupe
#: key. A per-call offset keeps the fixture from colliding with itself; using
#: `now()` would collide outright, because two calls in the same millisecond
#: land on the same due_at on a Windows clock.
_occurrence_offset = itertools.count()


def _make_signal(patient, *, task=None, count=3):
    """A repeated-unconfirmed signal for one task, with its evidence attached."""
    task = task or CareTask.objects.create(
        patient=patient, label='A task', times_of_day=['08:00'])
    base = next(_occurrence_offset) * 1000
    occurrences = [
        TaskOccurrence.objects.create(
            task=task, patient=patient,
            due_at=timezone.now() - timedelta(minutes=base + i + 1),
            state=TaskOccurrence.State.UNCONFIRMED)
        for i in range(count)]
    signal = MonitoringSignal.objects.create(
        patient=patient,
        kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
        window_start=occurrences[-1].due_at, window_end=occurrences[0].due_at,
        subject_key=f'task:{task.pk}', rule='unconfirmed_streak')
    signal.occurrences.set(occurrences)
    return signal


class ConcurrentDispatchTests(TransactionTestCase):
    """
    The real race, run with threads against a real database.

    TransactionTestCase rather than TestCase: TestCase wraps each test in one
    transaction that is never committed, so a second thread would never see the
    first thread's writes and the race could not happen at all.
    """

    def setUp(self):
        self.patient = User.objects.create_user(
            'cc_patient', email='cc_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.caregiver = User.objects.create_user(
            'cc_care', email='cc_care@test.invalid', password='pw',
            role='patient', first_name='Mikko')
        SharingGrant.objects.create(
            patient=self.patient, recipient=self.caregiver,
            can_view_alerts=True, status=SharingGrant.Status.ACTIVE)
        self.task = CareTask.objects.create(
            patient=self.patient, label='A task', times_of_day=['08:00'])

    def _run_concurrently(self, work, threads=2):
        """
        Start every thread at once, so they overlap where it matters.

        Retries on SQLite's writer lock, and only on that. Django's SQLite test
        database is in-memory in shared-cache mode, where a second writer gets
        SQLITE_LOCKED ("database table is locked") immediately instead of
        waiting — `busy_timeout` does not cover shared-cache table locks. That
        is an artifact of the test backend, not a fact about the pipeline;
        production is PostgreSQL, which blocks and proceeds.

        Retrying is safe here because the lock aborts the statement and rolls
        its transaction back, so nothing was committed to double-count. It also
        cannot mask the bugs under test: a lost increment or a duplicate event
        is a committed outcome, and no number of retries turns one into the
        right answer.
        """
        from django.db import OperationalError

        start = threading.Barrier(threads)
        errors = []

        def runner(index):
            try:
                start.wait(timeout=10)
                for attempt in range(10):
                    try:
                        work(index)
                        break
                    except OperationalError as exc:
                        if 'lock' not in str(exc).lower() or attempt == 9:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except Exception as exc:          # recorded, not swallowed
                errors.append(exc)
            finally:
                connection.close()

        workers = [threading.Thread(target=runner, args=(i,)) for i in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        return errors

    def test_two_concurrent_dispatches_produce_one_event(self):
        """
        ACCEPTANCE — the race the review identified.

        Two cycles evaluate the same situation at the same moment. Exactly one
        event may exist for it, because the second event is a second round of
        notifications about a thing the caregiver has already been told.
        """
        signals = [_make_signal(self.patient, task=self.task) for _ in range(2)]

        errors = self._run_concurrently(lambda i: dispatch_signal(signals[i]))

        self.assertEqual(errors, [], f'a dispatch raised: {errors}')
        live = NotificationEvent.objects.filter(
            subject=self.patient, superseded_at__isnull=True)
        self.assertEqual(live.count(), 1,
                         'concurrent dispatches created more than one live event')

    def test_the_caregiver_is_not_told_twice(self):
        """
        The consequence, asserted where it is actually felt.

        One event is the mechanism; one message is the point. Counting
        deliveries catches a regression that keeps the event count right while
        still buzzing the phone twice.
        """
        signals = [_make_signal(self.patient, task=self.task) for _ in range(2)]

        self._run_concurrently(lambda i: dispatch_signal(signals[i]))

        in_app = NotificationDelivery.objects.filter(
            recipient=self.caregiver, channel='in_app')
        self.assertEqual(in_app.count(), 1,
                         'the caregiver received more than one notification')

    def test_the_folded_occurrence_is_still_counted(self):
        """
        Losing the race must not lose the fact. The situation recurred, and the
        surviving event has to say so — silently dropping it would make a
        persistent problem look like a one-off.
        """
        signals = [_make_signal(self.patient, task=self.task) for _ in range(2)]

        self._run_concurrently(lambda i: dispatch_signal(signals[i]))

        event = NotificationEvent.objects.filter(
            subject=self.patient, superseded_at__isnull=True).first()
        self.assertEqual(event.occurrence_count, 2)

    def test_concurrent_increments_do_not_lose_a_count(self):
        """
        The read-modify-write in `event_for_signal`: read the count, add one,
        write it back. Two cycles read the same value and one increment is lost.
        """
        first = _make_signal(self.patient, task=self.task)
        dispatch_signal(first)                      # creates the live event

        repeats = [_make_signal(self.patient, task=self.task) for _ in range(4)]
        errors = self._run_concurrently(
            lambda i: dispatch_signal(repeats[i]), threads=4)

        self.assertEqual(errors, [])
        event = NotificationEvent.objects.filter(
            subject=self.patient, superseded_at__isnull=True).first()
        self.assertEqual(event.occurrence_count, 5,
                         'an increment was lost to a read-modify-write race')


class LiveEventConstraintTests(TestCase):
    """The database-level guarantee, independent of the code path above."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'lc_patient', email='lc_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')

    def test_a_second_live_event_for_the_same_key_is_rejected(self):
        """
        ACCEPTANCE — asserted against the database, not against the function
        that is supposed to avoid it. If the check in `event_for_signal` is ever
        removed or reordered, this still fails.
        """
        from django.db import IntegrityError, transaction

        NotificationEvent.objects.create(
            kind=NotificationEvent.Kind.CARE_SIGNAL, subject=self.patient,
            dedupe_key='repeated_unconfirmed:task:1')

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NotificationEvent.objects.create(
                    kind=NotificationEvent.Kind.CARE_SIGNAL, subject=self.patient,
                    dedupe_key='repeated_unconfirmed:task:1')

    def test_a_superseded_event_frees_the_key(self):
        """The window reopening is the whole reason `superseded_at` exists."""
        first = NotificationEvent.objects.create(
            kind=NotificationEvent.Kind.CARE_SIGNAL, subject=self.patient,
            dedupe_key='repeated_unconfirmed:task:1')
        NotificationEvent.objects.filter(pk=first.pk).update(
            superseded_at=timezone.now())

        second = NotificationEvent.objects.create(
            kind=NotificationEvent.Kind.CARE_SIGNAL, subject=self.patient,
            dedupe_key='repeated_unconfirmed:task:1')

        self.assertNotEqual(first.pk, second.pk)

    def test_events_without_a_dedupe_key_do_not_collide(self):
        """
        `dedupe_key` defaults to '' for events that do not aggregate — an
        appointment reminder is a separate thing each time. Without the blank
        exclusion in the constraint, the second one would be rejected.
        """
        for _ in range(3):
            NotificationEvent.objects.create(
                kind=NotificationEvent.Kind.APPOINTMENT, subject=self.patient)

        self.assertEqual(NotificationEvent.objects.filter(
            subject=self.patient, dedupe_key='').count(), 3)

    def test_two_patients_may_hold_the_same_key(self):
        """The key is scoped to a subject, not global."""
        other = User.objects.create_user(
            'lc_other', email='lc_other@test.invalid', password='pw', role='patient')

        for subject in (self.patient, other):
            NotificationEvent.objects.create(
                kind=NotificationEvent.Kind.CARE_SIGNAL, subject=subject,
                dedupe_key='repeated_unconfirmed:task:1')

        self.assertEqual(NotificationEvent.objects.count(), 2)


class FoldedVersusUnheardTests(TestCase):
    """
    An empty result had two causes and no way to tell them apart.

    "Nobody was authorised" may be a revoked share worth looking at; "folded into
    a live event" is the system working as designed. Both returned `[]`.
    """

    def setUp(self):
        self.patient = User.objects.create_user(
            'fu_patient', email='fu_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.caregiver = User.objects.create_user(
            'fu_care', email='fu_care@test.invalid', password='pw',
            role='patient', first_name='Mikko')

    def _grant(self):
        return SharingGrant.objects.create(
            patient=self.patient, recipient=self.caregiver,
            can_view_alerts=True, status=SharingGrant.Status.ACTIVE)

    def test_no_authorised_recipient_is_not_reported_as_folded(self):
        result = dispatch_signal(_make_signal(self.patient))

        self.assertEqual(result, [])
        self.assertFalse(result.folded)
        self.assertTrue(result.reached_nobody)

    def test_a_folded_event_is_reported_as_folded(self):
        self._grant()
        task = CareTask.objects.create(
            patient=self.patient, label='A task', times_of_day=['08:00'])
        dispatch_signal(_make_signal(self.patient, task=task))

        result = dispatch_signal(_make_signal(self.patient, task=task))

        self.assertEqual(result, [])
        self.assertTrue(result.folded)
        self.assertFalse(result.reached_nobody)
        self.assertIsNotNone(result.event)

    def test_the_result_is_still_a_list(self):
        """
        Existing callers do `len(...)` and `== []`. The diagnostic is an
        addition, not a new contract.
        """
        self._grant()
        result = dispatch_signal(_make_signal(self.patient))

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), len(list(result)))


class RenderedTextIsRecipientIndependentTests(TestCase):
    """
    The module docstring used to promise per-recipient rendering, and
    `render_for_caregiver` has never taken a recipient.

    The promise is now the opposite and stronger: the text is minimised until
    there is nothing a sharing scope could vary. These tests hold it to that, so
    the single hoisted render stays correct.
    """

    def setUp(self):
        self.patient = User.objects.create_user(
            'ri_patient', email='ri_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.narrow = User.objects.create_user(
            'ri_narrow', email='ri_narrow@test.invalid', password='pw', role='patient')
        self.broad = User.objects.create_user(
            'ri_broad', email='ri_broad@test.invalid', password='pw', role='patient')

        SharingGrant.objects.create(
            patient=self.patient, recipient=self.narrow,
            can_view_alerts=True, status=SharingGrant.Status.ACTIVE)
        # Everything the model offers, to make the two readers as different as
        # the sharing system allows.
        SharingGrant.objects.create(
            patient=self.patient, recipient=self.broad,
            can_view_alerts=True, can_view_records=True,
            can_view_appointments=True, status=SharingGrant.Status.ACTIVE)

    def test_two_caregivers_with_different_scopes_receive_identical_text(self):
        """ACCEPTANCE — what makes one render for all recipients correct."""
        dispatch_signal(_make_signal(self.patient))

        bodies = {d.recipient_id: (d.title, d.body) for d in
                  NotificationDelivery.objects.filter(channel='in_app')}

        self.assertEqual(len(bodies), 2, 'both caregivers should have been told')
        self.assertEqual(bodies[self.narrow.pk], bodies[self.broad.pk])

    def test_the_event_is_rendered_once_per_dispatch(self):
        """
        Pins the hoist. Rendering inside the recipient loop produced N identical
        strings and implied a per-recipient rule that does not exist.
        """
        from unittest.mock import patch

        import apps.notifications.dispatch as dispatch_module

        event, _ = event_for_signal(_make_signal(self.patient))

        with patch.object(dispatch_module, 'deliver', wraps=dispatch_module.deliver):
            with patch('apps.notifications.recipients.render_for_caregiver',
                       wraps=__import__(
                           'apps.notifications.recipients',
                           fromlist=['render_for_caregiver']).render_for_caregiver
                       ) as render:
                dispatch_module.deliver(event)

        self.assertEqual(render.call_count, 1,
                         'render_for_caregiver ran once per recipient, not once')
