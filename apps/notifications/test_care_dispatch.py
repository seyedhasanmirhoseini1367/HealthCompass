"""
Who gets told, and what the message is allowed to contain.

A notification is the least controlled surface in the product. It lands on a
lock screen in a waiting room, in an inbox synced to a shared family tablet, on
a watch face turned towards whoever is sitting opposite. The person holding the
device is often not the person the message is for.

So there are two separate questions, and this file tests both:

  * authorization — is this recipient entitled to hear anything at all?
  * minimisation  — given they are, does the message say more than it needs to?

The second is not implied by the first. A caregiver may be fully entitled to
read a diagnosis in the app and still have no business receiving it as a push
notification, because the app is behind a login and the lock screen is not.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import SharingGrant
from apps.care.models import CareTask, MonitoringSignal, PatientReport, TaskOccurrence
from apps.notifications.dispatch import deliver, dispatch_signal, event_for_signal
from apps.notifications.events import NotificationDelivery, NotificationEvent
from apps.notifications.models import Notification

User = get_user_model()

#: Distinctive strings that must never reach a notification body.
DRUG      = 'Zidovudine'
SYMPTOM   = 'I have been coughing blood'
CONDITION = 'HIV infection'


class _Pipeline(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'nd_patient', email='nd_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.caregiver = User.objects.create_user(
            'nd_care', email='nd_care@test.invalid', password='pw',
            role='patient', first_name='Mikko')
        self.stranger = User.objects.create_user(
            'nd_stranger', email='nd_stranger@test.invalid', password='pw',
            role='patient')

    def _grant(self, recipient=None, **kwargs):
        options = dict(can_view_alerts=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        return SharingGrant.objects.create(
            patient=self.patient, recipient=recipient or self.caregiver, **options)

    def _unconfirmed_signal(self, label=DRUG, count=3):
        task = CareTask.objects.create(
            patient=self.patient, label=label, times_of_day=['08:00'])
        occurrences = [
            TaskOccurrence.objects.create(
                task=task, patient=self.patient,
                due_at=timezone.now() - timedelta(days=i + 1),
                state=TaskOccurrence.State.UNCONFIRMED)
            for i in range(count)]
        signal = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=occurrences[-1].due_at, window_end=occurrences[0].due_at,
            subject_key=f'task:{task.pk}', rule='unconfirmed_streak')
        signal.occurrences.set(occurrences)
        return signal

    def _symptom_signal(self, text=SYMPTOM):
        report = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient, text=text)
        signal = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPORTED_SYMPTOM,
            window_start=report.effective_at, window_end=report.effective_at,
            subject_key=f'report:{report.pk}', rule='reported_symptom')
        signal.reports.set([report])
        return signal


class AuthorizationTests(_Pipeline):

    def test_a_caregiver_with_the_scope_is_told(self):
        self._grant()
        deliveries = dispatch_signal(self._unconfirmed_signal())

        self.assertTrue(any(d.recipient == self.caregiver for d in deliveries))

    def test_nobody_without_a_grant_is_told(self):
        """ACCEPTANCE — no grant, no delivery. Not even a suppressed row."""
        deliveries = dispatch_signal(self._unconfirmed_signal())

        self.assertEqual(deliveries, [])
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    def test_a_revoked_grant_stops_the_next_notification(self):
        grant = self._grant()
        grant.revoke(by=self.patient, reason='no longer needed')

        self.assertEqual(dispatch_signal(self._unconfirmed_signal()), [])

    def test_an_expired_grant_stops_the_next_notification(self):
        self._grant(expires_at=timezone.now() - timedelta(days=1))

        self.assertEqual(dispatch_signal(self._unconfirmed_signal()), [])

    def test_a_records_only_grant_does_not_receive_care_alerts(self):
        """
        The scopes are separate promises. Someone given documents but not the
        "tell me if something is wrong" scope has not asked to be interrupted.
        """
        self._grant(can_view_alerts=False, can_view_records=True)

        self.assertEqual(dispatch_signal(self._unconfirmed_signal()), [])

    def test_a_third_partys_grant_does_not_deliver_to_a_stranger(self):
        self._grant()
        deliveries = dispatch_signal(self._unconfirmed_signal())

        for delivery in deliveries:
            self.assertNotEqual(delivery.recipient, self.stranger)

    def test_authorization_is_asked_fresh_on_every_dispatch(self):
        """A cached recipient list would keep messaging after a revocation."""
        grant = self._grant()
        dispatch_signal(self._unconfirmed_signal())

        grant.revoke(by=self.patient, reason='changed my mind')
        second = self._unconfirmed_signal(label='Another task')

        self.assertEqual(dispatch_signal(second), [])


class MinimisationTests(_Pipeline):
    """What the message may contain, tested against the delivered text."""

    def test_a_notification_never_names_the_medication(self):
        """ACCEPTANCE — the task label is the string most likely to be a drug."""
        self._grant()
        dispatch_signal(self._unconfirmed_signal(label=DRUG))

        for delivery in NotificationDelivery.objects.all():
            self.assertNotIn(DRUG, delivery.title)
            self.assertNotIn(DRUG, delivery.body)

    def test_a_notification_never_repeats_what_the_patient_said(self):
        """
        Their own words are the thing they most plausibly wanted heard — and a
        sentence about their health arriving unprompted on someone else's phone.
        It stays in the app, behind the login.
        """
        self._grant()
        dispatch_signal(self._symptom_signal(text=SYMPTOM))

        for delivery in NotificationDelivery.objects.all():
            self.assertNotIn('coughing blood', delivery.body)
            self.assertNotIn(SYMPTOM, delivery.body)

    def test_a_notification_still_says_what_needs_doing(self):
        """Minimisation must not become uselessness."""
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        delivery = NotificationDelivery.objects.filter(channel='in_app').first()
        self.assertIn('Aino', delivery.body)
        self.assertIn('HealthCompass', delivery.body)

    def test_the_notification_does_not_assert_that_a_task_was_missed(self):
        """
        ACCEPTANCE — the evidence is silence, so the wording must be about
        silence. "Has not confirmed" is true; "did not take" is a false
        statement about someone's health sent to their family.
        """
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        for delivery in NotificationDelivery.objects.all():
            body = delivery.body.lower()
            self.assertNotIn('did not take', body)
            self.assertNotIn("didn't take", body)
            self.assertNotIn('missed', body)
            self.assertIn('not confirmed', body)

    def test_no_clinical_vocabulary_reaches_a_notification_body(self):
        self._grant()
        dispatch_signal(self._unconfirmed_signal(label=f'{DRUG} for {CONDITION}'))

        for delivery in NotificationDelivery.objects.all():
            for term in (DRUG, CONDITION, 'mg', 'diagnosis'):
                self.assertNotIn(term.lower(), delivery.body.lower())

    def test_a_username_is_never_used_as_the_display_name(self):
        """Usernames here are often the email local-part — more identifying."""
        self.patient.first_name = ''
        self.patient.last_name = ''
        self.patient.save()
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        for delivery in NotificationDelivery.objects.all():
            self.assertNotIn('nd_patient', delivery.body)

    def test_the_delivered_text_is_stored_as_sent(self):
        """
        Answering "did we disclose too much?" later has to use what was sent,
        not a re-render under today's rules and today's sharing scope.
        """
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        delivery = NotificationDelivery.objects.filter(channel='in_app').first()
        self.assertTrue(delivery.body)
        self.assertEqual(Notification.objects.get(user=self.caregiver).message,
                         delivery.body)


class AggregationTests(_Pipeline):
    """Alert fatigue is the failure mode that makes the feature worthless."""

    def test_the_same_situation_does_not_notify_twice(self):
        """ACCEPTANCE — a task unanswered for a week is one thing, not seven."""
        self._grant()
        signal = self._unconfirmed_signal()
        dispatch_signal(signal)

        second = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=timezone.now(), window_end=timezone.now(),
            subject_key=signal.subject_key, rule='unconfirmed_streak')

        self.assertEqual(dispatch_signal(second), [])

    def test_a_repeat_bumps_the_count_on_the_live_event(self):
        self._grant()
        signal = self._unconfirmed_signal()
        dispatch_signal(signal)

        second = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=timezone.now(), window_end=timezone.now(),
            subject_key=signal.subject_key, rule='unconfirmed_streak')
        dispatch_signal(second)

        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(NotificationEvent.objects.get().occurrence_count, 2)

    def test_two_different_symptoms_are_two_notifications(self):
        """Deduplicating these would drop one of them entirely."""
        self._grant()
        dispatch_signal(self._symptom_signal(text='I felt dizzy'))
        dispatch_signal(self._symptom_signal(text='My ankle is swollen'))

        self.assertEqual(NotificationEvent.objects.count(), 2)

    def test_a_retry_does_not_deliver_the_same_worry_twice(self):
        self._grant()
        event, _ = event_for_signal(self._unconfirmed_signal())
        deliver(event)
        deliver(event)

        self.assertEqual(
            NotificationDelivery.objects.filter(channel='in_app').count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.caregiver).count(), 1)


class ChannelTests(_Pipeline):

    def test_in_app_always_works(self):
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        delivery = NotificationDelivery.objects.get(channel='in_app')
        self.assertEqual(delivery.status, NotificationDelivery.Status.SENT)
        self.assertTrue(Notification.objects.filter(user=self.caregiver).exists())

    def test_an_unconfigured_channel_records_unavailable_rather_than_vanishing(self):
        """
        ACCEPTANCE — the silent no-op is the bug this replaces. A deployment
        with no Firebase looked identical to one that was delivering.
        """
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        push = NotificationDelivery.objects.filter(channel='push').first()
        self.assertIsNotNone(push, 'the push attempt left no trace at all')
        self.assertEqual(push.status, NotificationDelivery.Status.UNAVAILABLE)

    def test_sms_and_voice_are_declared_but_not_pretended(self):
        from apps.notifications.channels import get_channel

        for name in ('sms', 'voice'):
            channel = get_channel(name)
            self.assertIsNotNone(channel, f'{name} is not declared at all')
            self.assertFalse(channel.is_available())

    def test_voice_records_that_it_needs_recipient_verification(self):
        """
        A phone call reaches whoever picks up. Any future implementation has to
        establish who answered before saying anything about someone's health,
        and that requirement is recorded where it will be read.
        """
        from apps.notifications.channels import get_channel

        _, detail = get_channel('voice').deliver(
            recipient=self.caregiver, title='t', body='b', event=None)
        self.assertIn('verification', detail)

    def test_a_broken_channel_does_not_stop_the_others(self):
        from unittest.mock import patch

        from apps.notifications.channels import InAppChannel

        self._grant()
        with patch.object(InAppChannel, 'deliver', side_effect=RuntimeError('boom')):
            dispatch_signal(self._unconfirmed_signal())

        in_app = NotificationDelivery.objects.get(channel='in_app')
        self.assertEqual(in_app.status, NotificationDelivery.Status.FAILED)
        # The push attempt was still made and still recorded.
        self.assertTrue(NotificationDelivery.objects.filter(channel='push').exists())

    def test_a_channel_failure_detail_never_carries_the_message(self):
        """Provider errors routinely quote the message they rejected."""
        from unittest.mock import patch

        from apps.notifications.channels import InAppChannel

        self._grant()
        with patch.object(InAppChannel, 'deliver',
                          side_effect=RuntimeError(f'rejected: {DRUG}')):
            dispatch_signal(self._unconfirmed_signal(label=DRUG))

        self.assertNotIn(DRUG, NotificationDelivery.objects.get(channel='in_app').detail)


class SeparationTests(_Pipeline):
    """The event knows nothing about recipients or channels."""

    def test_an_event_carries_no_rendered_message(self):
        """
        ACCEPTANCE — text is built per recipient, after authorization. An event
        holding one message would mean one disclosure decision for everyone.
        """
        event, _ = event_for_signal(self._unconfirmed_signal())

        self.assertFalse(hasattr(event, 'body'))
        self.assertFalse(hasattr(event, 'message'))

    def test_an_event_exists_even_when_nobody_is_authorised(self):
        """
        The thing still happened. Only the delivery did not, and the difference
        matters when a patient later asks what was noticed about them.
        """
        dispatch_signal(self._unconfirmed_signal())

        self.assertEqual(NotificationEvent.objects.count(), 1)
        self.assertEqual(NotificationDelivery.objects.count(), 0)

    def test_an_event_links_back_to_the_signal_that_caused_it(self):
        signal = self._unconfirmed_signal()
        event, _ = event_for_signal(signal)

        self.assertEqual(event.signal, signal)
        self.assertEqual(event.signal.occurrences.count(), 3)

    @override_settings(CARE_NOTIFICATION_CHANNELS=['in_app'])
    def test_the_channel_set_is_configurable_without_touching_domain_code(self):
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        self.assertEqual(
            set(NotificationDelivery.objects.values_list('channel', flat=True)),
            {'in_app'})


class NoDoublePushTests(_Pipeline):
    """
    One worry, one buzz.

    A post_save receiver on Notification pushes for any row written directly,
    which is the convenience path older code uses. The delivery pipeline treats
    push as its own channel with its own availability check and its own row, so
    the two must not both fire — the recipient would be told twice and only one
    of the sends would be recorded.
    """

    def test_the_in_app_channel_does_not_also_fire_the_blanket_push(self):
        """ACCEPTANCE — otherwise every care message arrives twice."""
        from unittest.mock import patch

        self._grant()
        with patch('apps.notifications.firebase.send_push') as spy:
            dispatch_signal(self._unconfirmed_signal())

        self.assertEqual(spy.call_count, 0,
                         'the in-app channel triggered an unrecorded push')

    def test_a_notification_written_directly_still_pushes(self):
        """The convenience path older code relies on is untouched."""
        from unittest.mock import patch

        with patch('apps.notifications.firebase.send_push') as spy:
            Notification.objects.create(
                user=self.caregiver, type=Notification.Type.SYSTEM,
                title='Direct', message='Written without the pipeline')

        self.assertEqual(spy.call_count, 1)

    def test_an_appointment_reminder_sends_exactly_one_push(self):
        """
        ACCEPTANCE — it used to send two: the receiver fired on the Notification
        row, and the command called send_push again on the next line.
        """
        from unittest.mock import patch

        from django.core.management import call_command

        from apps.appointments.models import Appointment

        Appointment.objects.create(
            patient=self.patient, title='Cardiology',
            appointment_datetime=timezone.now() + timedelta(hours=1),
            remind_1h=True)

        with patch('apps.notifications.firebase.send_push') as spy:
            call_command('send_appointment_reminders', verbosity=0)

        self.assertEqual(spy.call_count, 1)
        self.assertEqual(Notification.objects.filter(user=self.patient).count(), 1)

    def test_a_failing_push_is_logged_rather_than_swallowed(self):
        """It was `except Exception: pass` — an undeliverable push left no trace."""
        from unittest.mock import patch

        with patch('apps.notifications.firebase.send_push',
                   side_effect=RuntimeError('boom')):
            with self.assertLogs('apps.notifications.apps', level='WARNING') as logs:
                Notification.objects.create(
                    user=self.caregiver, type=Notification.Type.SYSTEM,
                    title='Direct', message='body')

        self.assertIn('RuntimeError', '\n'.join(logs.output))


class ActionableLinkTests(_Pipeline):
    """
    "What can I do?" — the fourth question a notification has to answer.

    The in-app row hardcoded '/care/', which is the RECIPIENT's own care page.
    A caregiver tapping a notification about their mother landed on their own
    medication list, which is both useless and briefly alarming.
    """

    def test_a_caregiver_notification_opens_the_page_about_that_person(self):
        """ACCEPTANCE — the link must name the subject, not the reader."""
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        notification = Notification.objects.get(user=self.caregiver)
        self.assertEqual(notification.link,
                         f'/care/person/{self.patient.pk}/')

    def test_the_link_is_not_the_recipients_own_care_page(self):
        self._grant()
        dispatch_signal(self._unconfirmed_signal())

        self.assertNotEqual(Notification.objects.get(user=self.caregiver).link,
                            '/care/')

    def test_a_patient_notification_opens_their_own_care_page(self):
        from apps.notifications.dispatch import deliver_to_patient

        event, _ = event_for_signal(self._unconfirmed_signal())
        deliver_to_patient(event)

        self.assertEqual(Notification.objects.get(user=self.patient).link,
                         '/care/')

    def test_the_link_still_carries_no_clinical_detail(self):
        """A URL is text on a screen too — no drug name in the path."""
        self._grant()
        dispatch_signal(self._unconfirmed_signal(label=DRUG))

        self.assertNotIn(DRUG.lower(),
                         Notification.objects.get(user=self.caregiver).link.lower())
