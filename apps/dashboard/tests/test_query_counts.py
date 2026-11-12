"""Query budgets for the busiest pages (audit PERF-08).

Each page is measured with a few records and with many: the count must not grow
with the number of records (that is an N+1), and must stay under a fixed bound
so a new per-row query is caught in review rather than in production.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.accounts.models import SharingGrant
from apps.medical_records.models import MedicalRecord, ParsedLabValue

User = get_user_model()

# Upper bounds, measured when these tests were written plus headroom.
BUDGET = {'dashboard': 60, 'records': 25, 'shared_patient': 40}


def _records(patient, n, start=0):
    for i in range(start, start + n):
        r = MedicalRecord.objects.create(patient=patient, title=f'R{i}', record_type='lab_result',
                                         is_flagged=bool(i % 2))
        ParsedLabValue.objects.create(record=r, parameter_name='Hb', value=str(130 + i), unit='g/L')


class QueryBudgetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.patient = User.objects.create_user('qc_p', email='qcp@test.invalid', password='pw')
        self.carer = User.objects.create_user('qc_c', email='qcc@test.invalid', password='pw')
        SharingGrant.objects.create(patient=self.patient, recipient=self.carer, can_view_records=True,
                                    can_view_alerts=True, can_view_appointments=True)

    def _count(self, user, url):
        self.client.force_login(user)
        self.client.get(url, follow=True)  # warm caches (session, nav, consent)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url, follow=True)
        self.assertEqual(response.status_code, 200, url)
        return len(ctx.captured_queries)

    def _check(self, key, user, url):
        _records(self.patient, 2)
        few = self._count(user, url)
        _records(self.patient, 10, start=2)
        many = self._count(user, url)
        self.assertLessEqual(many, few + 2, f'{key}: {few} queries with 2 records, {many} with 12')
        self.assertLessEqual(many, BUDGET[key], f'{key}: {many} queries')

    def test_dashboard_home(self):
        self._check('dashboard', self.patient, '/dashboard/')

    def test_records_list(self):
        self._check('records', self.patient, '/records/')

    def test_shared_patient(self):
        self._check('shared_patient', self.carer, f'/accounts/shared/{self.patient.pk}/')
