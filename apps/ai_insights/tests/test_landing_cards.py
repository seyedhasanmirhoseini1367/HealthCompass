"""
Population Insights is open to everyone signed in, but for non-staff every
figure based on fewer than POPULATION_MIN_PATIENTS patients is withheld.
"""
import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.ai_insights.models import HealthAlert
from apps.medical_records.models import MedicalRecord, ParsedLabValue

User = get_user_model()
POP = reverse('ai_insights:population')


class LandingCardTests(TestCase):

    def _page(self):
        return self.client.get(reverse('ai_insights:list')).content.decode()

    def test_anonymous_gets_a_sign_in_link(self):
        self.assertIn(f'?next={POP}', self._page())

    def test_patient_sees_the_card(self):
        u = User.objects.create_user('lc_pat', email='lc@test.invalid', password='pw', role='patient')
        self.client.force_login(u)
        self.assertIn(f'href="{POP}"', self._page())


def _patients_with_labs(n, prefix, name='CRP', value=5.0):
    users = []
    for i in range(n):
        u = User.objects.create_user(f'{prefix}{i}', email=f'{prefix}{i}@test.invalid',
                                     password='pw', role='patient')
        rec = MedicalRecord.objects.create(patient=u, title='Labs', record_type='lab_result',
                                           record_date=datetime.date(2026, 10, 1))
        ParsedLabValue.objects.create(record=rec, parameter_name=name, value=str(value + i),
                                      unit='mg/L', canonical_value=value + i)
        users.append(u)
    return users


@override_settings(POPULATION_MIN_PATIENTS=5)
class SmallGroupTests(TestCase):

    def setUp(self):
        cache.clear()
        self.viewer = User.objects.create_user('pg_view', email='pgv@test.invalid',
                                               password='pw', role='patient')
        self.staff = User.objects.create_user('pg_staff', email='pgs@test.invalid',
                                              password='pw', is_staff=True)

    def tearDown(self):
        cache.clear()

    def test_a_patient_can_open_it(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(POP).status_code, 200)

    def test_a_tiny_cohort_shows_nothing_to_a_patient(self):
        _patients_with_labs(3, 'tiny')
        self.client.force_login(self.viewer)
        response = self.client.get(POP)
        self.assertContains(response, 'Not enough patients yet')
        self.assertEqual(response.context['latest_values'], {})
        self.assertNotContains(response, 'Biomarker Trends')

    # Staff are not held to the patients' stricter threshold. The minimum cohort
    # that applies to everyone (POPULATION_MIN_COHORT) is set to 1 here so this
    # test keeps measuring that difference; MinimumCohortTests cover the floor.
    @override_settings(POPULATION_MIN_COHORT=1)
    def test_staff_still_see_everything_in_a_tiny_cohort(self):
        _patients_with_labs(3, 'tinys')
        self.client.force_login(self.staff)
        response = self.client.get(POP)
        self.assertNotContains(response, 'Not enough patients yet')
        self.assertIn('CRP', response.context['latest_values'])

    def test_a_biomarker_from_too_few_patients_is_hidden(self):
        _patients_with_labs(6, 'big', name='CRP')
        _patients_with_labs(2, 'rare', name='RARE_MARKER')
        self.client.force_login(self.viewer)
        latest = self.client.get(POP).context['latest_values']
        self.assertIn('CRP', latest)
        self.assertNotIn('RARE_MARKER', latest)

    def test_alerts_from_too_few_patients_are_hidden(self):
        users = _patients_with_labs(6, 'al')
        HealthAlert.objects.create(patient=users[0], severity='critical',
                                   title='x', message='x')
        self.client.force_login(self.viewer)
        response = self.client.get(POP)
        self.assertTrue(response.context['alerts_hidden'])
        self.assertNotContains(response, 'Alert Distribution')
