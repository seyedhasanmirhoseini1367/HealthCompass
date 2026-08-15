"""
The clinical summary over the API.

Same rule as the web page: the subject is the caller and there is no parameter
that could name anyone else. What is tested here beyond that is the shape —
specifically that a disputed medication is not delivered inside `current`, since
a client rendering only that field would show a confident answer where the
documents disagree.
"""
from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.medical_records.models import (ConditionStatement, MedicalRecord,
                                         MedicationStatement)

User = get_user_model()


class ClinicalSummaryApiTests(TestCase):

    def setUp(self):
        # APIClient, not the Django test client: these endpoints authenticate
        # with JWT, so a session login reaches them as anonymous.
        self.client = APIClient()
        self.patient = User.objects.create_user(
            'api_cs', email='api_cs@test.invalid', password='pw', role='patient')
        self.other = User.objects.create_user(
            'api_cs2', email='api_cs2@test.invalid', password='pw', role='patient')

    def _record(self, when=date(2026, 1, 1), patient=None):
        return MedicalRecord.objects.create(
            patient=patient or self.patient, title='Discharge summary',
            record_type='discharge', record_date=when)

    def _med(self, name='Metformin', status=MedicationStatement.Status.ACTIVE,
             when=date(2026, 1, 1), patient=None):
        owner = patient or self.patient
        return MedicationStatement.objects.create(
            record=self._record(when, owner), patient=owner, name=name, status=status)

    def _get(self, user=None):
        if user is not None:
            self.client.force_authenticate(user=user)
        return self.client.get('/api/v1/records/summary/')

    def test_it_requires_authentication(self):
        response = self.client.get('/api/v1/records/summary/')
        self.assertIn(response.status_code, (401, 403))

    def test_it_returns_the_callers_current_medications(self):
        self._med()
        response = self._get(self.patient)

        self.assertEqual(response.status_code, 200)
        names = [m['name'] for m in response.json()['current_medications']]
        self.assertEqual(names, ['Metformin'])

    def test_it_never_returns_another_patients_medications(self):
        self._med(name='Warfarin', patient=self.other)
        response = self._get(self.patient)

        self.assertEqual(response.json()['current_medications'], [])

    def test_a_discontinued_medication_is_reported_separately(self):
        self._med(when=date(2026, 1, 1))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 6, 1))

        body = self._get(self.patient).json()
        self.assertEqual(body['current_medications'], [])
        self.assertEqual(len(body['discontinued_medications']), 1)

    def test_a_disputed_medication_is_not_delivered_as_current(self):
        """ACCEPTANCE — a client rendering `current` must not be told a side."""
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 5, 5))

        body = self._get(self.patient).json()
        self.assertEqual(body['current_medications'], [])
        self.assertEqual(body['discontinued_medications'], [])
        self.assertEqual(len(body['conflicted_medications']), 1)

    def test_a_dispute_carries_both_claims_and_their_sources(self):
        self._med(status=MedicationStatement.Status.ACTIVE, when=date(2026, 5, 5))
        self._med(status=MedicationStatement.Status.DISCONTINUED, when=date(2026, 5, 5))

        conflict = self._get(self.patient).json()['conflicted_medications'][0]
        self.assertEqual(len(conflict['statements']), 2)
        self.assertEqual(len(conflict['sources']), 2)
        self.assertEqual({s['status'] for s in conflict['statements']},
                         {'active', 'discontinued'})

    def test_conditions_are_returned_too(self):
        ConditionStatement.objects.create(
            record=self._record(), patient=self.patient, description='Type 2 diabetes')

        body = self._get(self.patient).json()
        self.assertEqual(body['current_conditions'][0]['description'], 'Type 2 diabetes')

    def test_the_payload_states_what_it_is_not(self):
        """The caveat travels with the data, not only on the web page."""
        self._med()
        self.assertIn('uploaded documents', self._get(self.patient).json()['caveat'])

    def test_the_record_detail_endpoint_includes_this_documents_statements(self):
        record = self._record()
        MedicationStatement.objects.create(
            record=record, patient=self.patient, name='Ramipril', dose='5mg')

        self.client.force_authenticate(user=self.patient)
        body = self.client.get(f'/api/v1/records/{record.pk}/').json()

        self.assertEqual(body['medications'][0]['name'], 'Ramipril')

    def test_the_summary_route_is_not_swallowed_by_the_detail_route(self):
        """`records/<str:pk>/` matches "summary" — order is what saves it."""
        self.client.force_authenticate(user=self.patient)
        response = self.client.get('/api/v1/records/summary/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('current_medications', response.json())
