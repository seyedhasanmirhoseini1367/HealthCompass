"""
API-1 — functional tests for the mobile API surface.

`apps/api/tests.py` contained a single comment. Forty-odd endpoints, all of them
serving PHI to a mobile client over JWT, had no functional coverage at all:
nothing asserted that they require authentication, and nothing asserted that one
patient's token cannot reach another patient's records. `test_security.py`
covers registration and privilege escalation; this file covers the endpoints
themselves.

Three properties are checked here, in order of how badly they fail:

1. **Every endpoint requires authentication.** Enumerated from the URLconf, so a
   route added later without a permission class fails this test rather than
   shipping open. That is the point of driving the list from `urlpatterns`
   instead of a hand-written list that would go stale.

2. **Object endpoints are patient-scoped.** A patient with a valid token asks
   for another patient's record, prediction, alert, notification, appointment
   and chat session, and gets nothing — and a delete attempt leaves the object
   intact.

3. **The read endpoints work.** A 500 on the dashboard is not a security
   problem, but it is the one every mobile session hits first.

No external model or embedding call is made: tests either use endpoints that do
not generate, or patch the generation boundary.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.ai_insights.models import AIModel, HealthAlert, ModelPrediction
from apps.medical_records.models import MedicalRecord

User = get_user_model()


# Endpoints that must serve unauthenticated callers by definition.
PUBLIC = {'register', 'login', 'token_refresh', 'forgot_password'}

# Sample arguments for routes that take one. The objects need not exist: an
# unauthenticated caller must be refused before anything is looked up.
SAMPLE_ARGS = {
    'record_detail':           ['00000000-0000-0000-0000-000000000001'],
    'record_delete':           ['00000000-0000-0000-0000-000000000001'],
    'alert_read':              ['1'],
    'prediction_detail':       ['00000000-0000-0000-0000-000000000001'],
    'notification_read':       ['1'],
    'ai_model_detail':         ['some-model'],
    'run_model':               ['some-model'],
    'appointment_detail':      ['00000000-0000-0000-0000-000000000001'],
    'assistant_session_detail': ['session-1'],
    'revoke_share':            ['1'],
    'shared_patient_detail':   ['1'],
    'care_task_stop':          ['00000000-0000-0000-0000-000000000001'],
    'occurrence_respond':      ['00000000-0000-0000-0000-000000000001'],
}


def _api_routes():
    """(name, url) for every route in the API URLconf."""
    from apps.api import urls as api_urls

    routes = []
    for pattern in api_urls.urlpatterns:
        name = pattern.name
        if not name:
            continue
        try:
            url = reverse(f'api:{name}', args=SAMPLE_ARGS.get(name, []))
        except Exception:
            continue
        routes.append((name, url))
    return routes


class AuthenticationRequiredTests(TestCase):
    """Property 1 — no endpoint may serve an anonymous caller."""

    def setUp(self):
        self.client = APIClient()

    def test_the_route_list_is_not_empty(self):
        """A silently empty sweep would pass while testing nothing."""
        self.assertGreater(len(_api_routes()), 30)

    def test_every_non_public_endpoint_refuses_anonymous_callers(self):
        for name, url in _api_routes():
            if name in PUBLIC:
                continue
            with self.subTest(endpoint=name):
                # Throttle state lives in the cache; clear it so a long sweep
                # cannot turn a 401 into a 429 and hide a missing check.
                cache.clear()
                response = self.client.get(url)
                self.assertIn(
                    response.status_code, (401, 403),
                    f'{name} answered {response.status_code} to an anonymous GET',
                )

    def test_public_endpoints_are_reachable_without_a_token(self):
        for name in ('login', 'register'):
            with self.subTest(endpoint=name):
                cache.clear()
                response = self.client.post(reverse(f'api:{name}'), {}, format='json')
                self.assertNotIn(response.status_code, (401, 403))


class _PatientFixture(TestCase):
    """Two patients, each with one of everything."""

    def setUp(self):
        cache.clear()
        self.owner = User.objects.create_user(
            'api_owner', email='api_owner@test.invalid', password='pw', role='patient')
        self.intruder = User.objects.create_user(
            'api_intruder', email='api_intruder@test.invalid', password='pw', role='patient')

        self.client = APIClient()
        self.client.force_authenticate(user=self.intruder)

        self.record = MedicalRecord.objects.create(
            patient=self.owner, title='Owner panel', record_type='lab_result',
            raw_text='Glucose: 5.2 mmol/L')
        self.alert = HealthAlert.objects.create(
            patient=self.owner, title='High glucose', message='See your doctor',
            severity='warning')
        model = AIModel.objects.create(
            data_scientist=self.owner, name='M', description='d')
        self.prediction = ModelPrediction.objects.create(
            model=model, patient=self.owner)


class PatientIsolationTests(_PatientFixture):
    """Property 2 — a valid token is not a key to everyone's data."""

    def test_another_patients_record_is_not_readable(self):
        response = self.client.get(
            reverse('api:record_detail', args=[str(self.record.pk)]))
        self.assertIn(response.status_code, (403, 404))

    def test_another_patients_record_content_is_not_leaked_in_the_body(self):
        response = self.client.get(
            reverse('api:record_detail', args=[str(self.record.pk)]))
        self.assertNotIn(b'Glucose', response.content)

    def test_another_patients_record_cannot_be_deleted(self):
        response = self.client.delete(
            reverse('api:record_delete', args=[str(self.record.pk)]))
        self.assertIn(response.status_code, (403, 404, 405))
        self.assertTrue(MedicalRecord.objects.filter(pk=self.record.pk).exists())

    def test_the_record_list_shows_only_your_own(self):
        MedicalRecord.objects.create(
            patient=self.intruder, title='Mine', record_type='lab_result')
        response = self.client.get(reverse('api:records_list'))
        self.assertEqual(response.status_code, 200)

        body = response.content.decode()
        self.assertIn('Mine', body)
        self.assertNotIn('Owner panel', body)

    def test_another_patients_prediction_is_not_readable(self):
        response = self.client.get(
            reverse('api:prediction_detail', args=[str(self.prediction.pk)]))
        self.assertIn(response.status_code, (403, 404))

    def test_the_prediction_list_shows_only_your_own(self):
        response = self.client.get(reverse('api:my_predictions'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(self.prediction.pk).encode(), response.content)

    def test_another_patients_alert_cannot_be_marked_read(self):
        response = self.client.patch(
            reverse('api:alert_read', args=[str(self.alert.pk)]))
        self.assertIn(response.status_code, (403, 404))

        self.alert.refresh_from_db()
        self.assertFalse(self.alert.is_read)

    def test_the_alert_list_shows_only_your_own(self):
        response = self.client.get(reverse('api:alerts_list'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'High glucose', response.content)

    def test_the_dashboard_counts_only_your_own_records(self):
        response = self.client.get(reverse('api:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get('total_records'), 0)

    def test_another_patients_chat_session_is_not_readable(self):
        from apps.rag_assistant.models import ChatSession

        session = ChatSession.objects.create(patient=self.owner, title='Private chat')
        response = self.client.get(
            reverse('api:assistant_session_detail', args=[str(session.pk)]))
        self.assertIn(response.status_code, (403, 404))

    def test_the_session_list_shows_only_your_own(self):
        from apps.rag_assistant.models import ChatSession

        ChatSession.objects.create(patient=self.owner, title='Private chat')
        response = self.client.get(reverse('api:assistant_sessions'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Private chat', response.content)

    def test_another_patients_appointment_is_not_readable(self):
        from apps.appointments.models import Appointment

        appointment = Appointment.objects.create(
            patient=self.owner, title='Cardiology follow-up',
            appointment_datetime=timezone.now() + timedelta(days=3))
        response = self.client.get(
            reverse('api:appointment_detail', args=[str(appointment.pk)]))
        self.assertIn(response.status_code, (403, 404))

    def test_the_appointment_list_shows_only_your_own(self):
        from apps.appointments.models import Appointment

        Appointment.objects.create(
            patient=self.owner, title='Cardiology follow-up',
            appointment_datetime=timezone.now() + timedelta(days=3))
        response = self.client.get(reverse('api:appointments'))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'Cardiology follow-up', response.content)

    def test_another_patients_notification_cannot_be_marked_read(self):
        from apps.notifications.models import Notification

        note = Notification.objects.create(
            user=self.owner, title='Result ready', message='Your results are in')
        response = self.client.patch(
            reverse('api:notification_read', args=[str(note.pk)]))
        self.assertIn(response.status_code, (403, 404))

        note.refresh_from_db()
        self.assertFalse(note.is_read)

    def test_shared_patient_detail_requires_a_grant(self):
        """No SharingGrant between these two users at all: a 404, not a 403 —
        the endpoint must not confirm the subject id exists to a caller with
        zero access."""
        response = self.client.get(
            reverse('api:shared_patient_detail', args=[self.owner.pk]))
        self.assertEqual(response.status_code, 404)

    def test_another_patients_share_cannot_be_revoked(self):
        from apps.accounts.models import SharingGrant

        third_party = User.objects.create_user(
            'api_share_recipient', email='api_share_recipient@test.invalid',
            password='pw', role='patient')
        grant = SharingGrant.objects.create(
            patient=self.owner, recipient=third_party, can_view_records=True)

        response = self.client.post(
            reverse('api:revoke_share', args=[grant.pk]))
        self.assertIn(response.status_code, (403, 404))

        grant.refresh_from_db()
        self.assertEqual(grant.status, SharingGrant.Status.ACTIVE)

    def test_another_patients_care_task_cannot_be_stopped(self):
        from apps.care.models import CareTask

        task = CareTask.objects.create(
            patient=self.owner, label='Owner medication', times_of_day=['08:00'])

        response = self.client.post(
            reverse('api:care_task_stop', args=[task.pk]))
        self.assertIn(response.status_code, (403, 404))

        task.refresh_from_db()
        self.assertTrue(task.is_active)

    def test_another_patients_occurrence_cannot_be_responded_to(self):
        from apps.care.models import CareTask, TaskOccurrence

        task = CareTask.objects.create(
            patient=self.owner, label='Owner medication', times_of_day=['08:00'])
        occurrence = TaskOccurrence.objects.create(
            task=task, patient=self.owner, due_at=timezone.now())

        response = self.client.post(
            reverse('api:occurrence_respond', args=[occurrence.pk]),
            {'state': 'confirmed'}, format='json')
        self.assertIn(response.status_code, (403, 404))

        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.PENDING)

    def test_the_care_tasks_list_shows_only_your_own(self):
        from apps.care.models import CareTask

        CareTask.objects.create(
            patient=self.owner, label='Owner medication', times_of_day=['08:00'])
        CareTask.objects.create(
            patient=self.intruder, label='Mine', times_of_day=['09:00'])

        response = self.client.get(reverse('api:care_tasks'))
        self.assertEqual(response.status_code, 200)

        body = response.content.decode()
        self.assertIn('Mine', body)
        self.assertNotIn('Owner medication', body)


class ReadEndpointTests(TestCase):
    """Property 3 — the endpoints a mobile session opens with actually work."""

    def setUp(self):
        cache.clear()
        self.patient = User.objects.create_user(
            'api_reader', email='api_reader@test.invalid', password='pw', role='patient')
        self.client = APIClient()
        self.client.force_authenticate(user=self.patient)

    def test_the_basic_read_endpoints_answer_200(self):
        for name in ('me', 'records_list', 'dashboard', 'analytics', 'alerts_list',
                     'my_predictions', 'notifications', 'ai_models',
                     'appointments', 'assistant_sessions', 'consent_list',
                     'consent_history', 'export_status',
                     'sharing_companions', 'care_tasks', 'care_occurrences',
                     'care_reports'):
            with self.subTest(endpoint=name):
                cache.clear()
                response = self.client.get(reverse(f'api:{name}'))
                self.assertEqual(response.status_code, 200,
                                 f'{name} answered {response.status_code}')

    def test_me_returns_this_user(self):
        response = self.client.get(reverse('api:me'))
        self.assertEqual(response.json().get('username'), 'api_reader')

    def test_me_does_not_leak_the_password_hash(self):
        body = response_body = self.client.get(reverse('api:me')).content.decode()
        for leak in ('password', 'pbkdf2', 'national_id'):
            self.assertNotIn(leak, response_body.lower(), f'{leak} appeared in /auth/me/')
        self.assertTrue(body)

    def test_an_empty_account_reports_zero_rather_than_failing(self):
        payload = self.client.get(reverse('api:dashboard')).json()
        self.assertEqual(payload.get('total_records'), 0)

    def test_a_missing_record_is_a_404_not_a_500(self):
        response = self.client.get(reverse(
            'api:record_detail', args=['00000000-0000-0000-0000-000000000009']))
        self.assertIn(response.status_code, (403, 404))


class WriteEndpointTests(TestCase):
    """Endpoints that change state must not do it on a GET."""

    def setUp(self):
        cache.clear()
        self.patient = User.objects.create_user(
            'api_writer', email='api_writer@test.invalid', password='pw', role='patient')
        self.client = APIClient()
        self.client.force_authenticate(user=self.patient)

    def test_state_changing_endpoints_reject_get(self):
        for name in ('consent_grant', 'consent_revoke', 'change_password',
                     'record_upload', 'upload_text', 'assistant_ask',
                     'create_share'):
            with self.subTest(endpoint=name):
                cache.clear()
                response = self.client.get(reverse(f'api:{name}'))
                self.assertEqual(response.status_code, 405,
                                 f'{name} accepted a GET')

    def test_consent_can_be_granted_and_revoked(self):
        from apps.accounts.consent import has_consent
        from apps.accounts.models import ConsentPurpose

        purpose = ConsentPurpose.EXTERNAL_LLM
        granted = self.client.post(reverse('api:consent_grant'),
                                   {'purpose': purpose}, format='json')
        self.assertIn(granted.status_code, (200, 201))
        self.assertTrue(has_consent(self.patient, purpose))

        revoked = self.client.post(reverse('api:consent_revoke'),
                                   {'purpose': purpose}, format='json')
        self.assertIn(revoked.status_code, (200, 204))
        self.assertFalse(has_consent(self.patient, purpose))

    def test_an_unknown_consent_purpose_is_rejected(self):
        response = self.client.post(reverse('api:consent_grant'),
                                    {'purpose': 'sell_my_data'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_text_upload_creates_a_record_for_the_caller(self):
        response = self.client.post(
            reverse('api:upload_text'),
            {'title': 'Home reading', 'text': 'Blood pressure 120/80'},
            format='json')
        self.assertIn(response.status_code, (200, 201), response.content)

        record = MedicalRecord.objects.filter(patient=self.patient).first()
        self.assertIsNotNone(record)
        self.assertEqual(record.patient, self.patient)

    def test_an_upload_cannot_be_attributed_to_another_patient(self):
        """A client-supplied patient id must not choose whose record this is."""
        victim = User.objects.create_user(
            'api_victim', email='api_victim@test.invalid', password='pw', role='patient')

        self.client.post(
            reverse('api:upload_text'),
            {'title': 'Planted', 'text': 'Blood pressure 120/80',
             'patient': victim.pk, 'patient_id': victim.pk},
            format='json')

        self.assertEqual(MedicalRecord.objects.filter(patient=victim).count(), 0)

    def test_a_share_can_be_created_and_revoked(self):
        from apps.accounts.models import SharingGrant

        recipient = User.objects.create_user(
            'api_recipient', email='api_recipient@test.invalid', password='pw', role='patient')

        created = self.client.post(
            reverse('api:create_share'),
            {'identifier': 'api_recipient', 'scopes': ['records', 'alerts']},
            format='json')
        self.assertEqual(created.status_code, 201, created.content)

        grant = SharingGrant.objects.get(patient=self.patient, recipient=recipient)
        self.assertTrue(grant.can_view_records)
        self.assertTrue(grant.can_view_alerts)

        revoked = self.client.post(reverse('api:revoke_share', args=[grant.pk]))
        self.assertEqual(revoked.status_code, 204)
        grant.refresh_from_db()
        self.assertEqual(grant.status, SharingGrant.Status.REVOKED)

    def test_a_share_cannot_be_created_with_yourself(self):
        response = self.client.post(
            reverse('api:create_share'),
            {'identifier': 'api_writer', 'scopes': ['records']}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_care_task_can_be_created_and_stopped(self):
        from apps.care.models import CareTask, TaskOccurrence

        created = self.client.post(
            reverse('api:care_tasks'),
            {'label': 'Evening pill', 'times_of_day': ['20:00']}, format='json')
        self.assertEqual(created.status_code, 201, created.content)

        task = CareTask.objects.get(patient=self.patient, label='Evening pill')
        # Materialised immediately, same as the web add_task view — a patient
        # who just set up a reminder should see it without waiting for cron.
        self.assertTrue(TaskOccurrence.objects.filter(task=task).exists())

        stopped = self.client.post(reverse('api:care_task_stop', args=[task.pk]))
        self.assertEqual(stopped.status_code, 204)
        task.refresh_from_db()
        self.assertFalse(task.is_active)

    def test_an_occurrence_can_only_be_resolved_to_a_human_state(self):
        from apps.care.models import CareTask, TaskOccurrence

        task = CareTask.objects.create(
            patient=self.patient, label='Morning pill', times_of_day=['08:00'])
        occurrence = TaskOccurrence.objects.create(
            task=task, patient=self.patient, due_at=timezone.now())

        response = self.client.post(
            reverse('api:occurrence_respond', args=[occurrence.pk]),
            {'state': 'unconfirmed'}, format='json')
        self.assertEqual(response.status_code, 400)

        response = self.client.post(
            reverse('api:occurrence_respond', args=[occurrence.pk]),
            {'state': 'confirmed'}, format='json')
        self.assertEqual(response.status_code, 200)
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.CONFIRMED)
