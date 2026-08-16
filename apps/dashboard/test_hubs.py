"""
The hubs, and the demo data that makes them inspectable.

Two things are being protected here.

  1. **Consolidation is not deletion.** Twelve navigation entries became six,
     and every capability that left the sidebar has to still be one click from
     a hub. Asserted by fetching hubs and finding links, never by trusting a
     template was edited correctly.
  2. **The demo seed goes through real authorization.** Maria sees Anna because
     a SharingGrant exists and `accounts.authz` agrees — not because a fixture
     handed a template a list. A demo that bypassed authorization would make a
     broken permission model look fine, which is worse than having no demo.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

User = get_user_model()


class MyHealthHubTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'hb_patient', email='hb_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.other = User.objects.create_user(
            'hb_other', email='hb_other@test.invalid', password='pw', role='patient')

    def test_it_requires_a_login(self):
        self.assertEqual(self.client.get('/dashboard/health/').status_code, 302)

    def test_an_empty_account_is_not_told_it_is_healthy(self):
        """ACCEPTANCE — no alert means no rule fired, not that nothing is wrong."""
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/health/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No alerts have been raised')
        for phrase in ('You are healthy', 'Everything is fine', 'All good'):
            self.assertNotContains(response, phrase)

    def test_it_never_shows_another_patients_data(self):
        """
        Patient isolation on My Health. This was asserted through a medication
        belonging to someone else; that feature is gone, so it is asserted
        through a record title instead — the page still lists recent records,
        and the isolation guarantee is the same one.
        """
        from datetime import date

        from apps.medical_records.models import MedicalRecord

        MedicalRecord.objects.create(
            patient=self.other, title='Warfarin clinic letter',
            record_type='discharge', record_date=date(2026, 1, 1))

        self.client.force_login(self.patient)
        self.assertNotContains(self.client.get('/dashboard/health/'), 'Warfarin')

    def test_there_is_no_health_score(self):
        self.client.force_login(self.patient)
        body = self.client.get('/dashboard/health/').content.decode().lower()

        for term in ('health score', 'wellness score', '%'):
            if term == '%':
                continue        # percentages appear in CSS widths
            self.assertNotIn(term, body)


class SettingsHubTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'st_patient', email='st_patient@test.invalid', password='pw',
            role='patient')

    def test_it_requires_a_login(self):
        self.assertEqual(self.client.get('/dashboard/settings/').status_code, 302)

    def test_privacy_controls_are_reachable(self):
        """
        ACCEPTANCE — these decide who can see a patient's health data, and were
        previously reachable only from a dropdown.
        """
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/settings/')

        for href in ('/accounts/my-shares/', '/accounts/consent/',
                     '/accounts/emergency-card/', '/accounts/export/'):
            self.assertContains(response, href)

    def test_it_reports_how_many_people_can_see_them(self):
        from apps.accounts.models import SharingGrant

        other = User.objects.create_user(
            'st_other', email='st_other@test.invalid', password='pw', role='patient')
        SharingGrant.objects.create(patient=self.patient, recipient=other,
                                    can_view_alerts=True,
                                    status=SharingGrant.Status.ACTIVE)

        self.client.force_login(self.patient)
        self.assertContains(self.client.get('/dashboard/settings/'), '1 active')


class CareHubTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'ch_patient', email='ch_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.caregiver = User.objects.create_user(
            'ch_care', email='ch_care@test.invalid', password='pw',
            role='patient', first_name='Mikko')

    def _grant(self, **kwargs):
        from apps.accounts.models import SharingGrant

        options = dict(can_view_alerts=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        return SharingGrant.objects.create(
            patient=self.patient, recipient=self.caregiver, **options)

    def test_a_caregiver_with_no_people_is_told_how_it_starts(self):
        """ACCEPTANCE — an empty box teaches nothing; sharing starts elsewhere."""
        self.client.force_login(self.caregiver)
        response = self.client.get('/care/')

        self.assertContains(response, 'Sharing always starts')
        self.assertContains(response, 'requested from this side')

    def test_a_caregiver_sees_the_people_who_share_with_them(self):
        self._grant()
        self.client.force_login(self.caregiver)

        self.assertContains(self.client.get('/care/'), 'Aino')

    def test_a_revoked_grant_removes_the_person(self):
        grant = self._grant()
        grant.revoke(by=self.patient, reason='no longer needed')

        self.client.force_login(self.caregiver)
        self.assertNotContains(self.client.get('/care/'), 'Aino')

    def test_both_directions_of_sharing_are_on_one_page(self):
        """
        "Who can see me" and "who can I see" were two destinations for one
        relationship seen from opposite ends.
        """
        self._grant()
        self.client.force_login(self.patient)
        response = self.client.get('/care/')

        self.assertContains(response, 'Who can see me')
        self.assertContains(response, 'Mikko')

    def test_the_old_watching_page_folds_into_the_hub(self):
        self.client.force_login(self.caregiver)
        response = self.client.get('/care/watching/')

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], '/care/')

    def test_the_person_page_is_reached_by_the_care_url(self):
        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get(f'/care/person/{self.patient.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Aino')

    def test_the_person_page_still_enforces_the_grant(self):
        """ACCEPTANCE — the nicer URL must not be a second authorization path."""
        stranger = User.objects.create_user(
            'ch_stranger', email='ch_s@test.invalid', password='pw', role='patient')
        self.client.force_login(stranger)

        self.assertEqual(
            self.client.get(f'/care/person/{self.patient.pk}/').status_code, 404)

    def test_a_revoked_grant_closes_the_person_page(self):
        grant = self._grant()
        grant.revoke(by=self.patient, reason='changed my mind')

        self.client.force_login(self.caregiver)
        self.assertEqual(
            self.client.get(f'/care/person/{self.patient.pk}/').status_code, 404)


@override_settings(DEBUG=True)
class DemoSeedTests(TestCase):
    """The demo has to be real data through real rules, or it proves nothing."""

    def _seed(self):
        call_command('seed_demo', '--quiet', stdout=StringIO())

    def test_it_creates_the_two_people(self):
        self._seed()

        self.assertTrue(User.objects.filter(username='demo_anna').exists())
        self.assertTrue(User.objects.filter(username='demo_maria').exists())

    def test_the_caregiver_sees_the_patient_through_real_authorization(self):
        """
        ACCEPTANCE — through `accounts.authz`, not through a fixture.

        If the sharing rule broke, this would fail, which is the entire reason
        the demo builds a SharingGrant instead of handing the template a list.
        """
        from apps.accounts.authz import sharing_grant
        from apps.notifications.recipients import CARE_SCOPE

        self._seed()
        anna = User.objects.get(username='demo_anna')
        maria = User.objects.get(username='demo_maria')

        self.assertIsNotNone(sharing_grant(maria, anna, CARE_SCOPE))

        self.client.force_login(maria)
        response = self.client.get(f'/care/person/{anna.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Anna')

    def test_the_grant_is_least_privilege(self):
        """Maria gets care and appointments. She does NOT get the records."""
        from apps.accounts.models import SharingGrant

        self._seed()
        grant = SharingGrant.objects.get(
            patient__username='demo_anna', recipient__username='demo_maria')

        self.assertTrue(grant.can_view_alerts)
        self.assertFalse(grant.can_view_records)

    def test_an_unrelated_user_cannot_see_the_demo_patient(self):
        self._seed()
        anna = User.objects.get(username='demo_anna')
        stranger = User.objects.create_user(
            'ds_stranger', email='ds@test.invalid', password='pw', role='patient')

        self.client.force_login(stranger)
        self.assertEqual(
            self.client.get(f'/care/person/{anna.pk}/').status_code, 404)

    def test_the_signal_is_raised_by_the_rule_not_inserted(self):
        """
        The demo builds three unanswered occurrences and lets `evaluate_task`
        decide. Inserting a MonitoringSignal directly would demonstrate a
        threshold nobody actually applied.
        """
        from apps.care.models import MonitoringSignal

        self._seed()
        signal = MonitoringSignal.objects.filter(
            patient__username='demo_anna').first()

        self.assertIsNotNone(signal)
        self.assertEqual(signal.rule, 'unconfirmed_streak')
        self.assertEqual(signal.occurrences.count(), 3)

    def test_the_demo_alert_does_not_manufacture_urgent(self):
        """
        ACCEPTANCE — URGENT is reserved for clinician-authored critical alerts.
        A demo that fabricated one would misrepresent what the state means.
        """
        from apps.ai_insights.models import HealthAlert
        from apps.dashboard.overview import build_dashboard
        from apps.dashboard.state import State

        self._seed()
        anna = User.objects.get(username='demo_anna')

        self.assertFalse(HealthAlert.objects.filter(
            patient=anna, severity=HealthAlert.Severity.CRITICAL).exists())
        self.assertNotEqual(build_dashboard(anna)['overall'], State.URGENT)

    def test_the_dashboard_is_worth_looking_at(self):
        """The whole point: the developer can see a populated, meaningful page."""
        self._seed()
        anna = User.objects.get(username='demo_anna')

        data = build = None
        self.client.force_login(anna)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['attention'],
                        'the demo dashboard has nothing to show')

    def test_seeding_twice_does_not_duplicate(self):
        self._seed()
        self._seed()

        self.assertEqual(User.objects.filter(username='demo_anna').count(), 1)

    def test_clear_demo_reports_before_it_deletes(self):
        self._seed()
        call_command('clear_demo', stdout=StringIO())

        self.assertTrue(User.objects.filter(username='demo_anna').exists())

    def test_clear_demo_removes_everything_with_apply(self):
        from apps.care.models import CareTask, MonitoringSignal
        from apps.medical_records.models import MedicalRecord

        self._seed()
        call_command('clear_demo', '--apply', stdout=StringIO())

        self.assertFalse(User.objects.filter(username__startswith='demo_').exists())
        self.assertFalse(MedicalRecord.objects.filter(
            patient__username='demo_anna').exists())
        self.assertFalse(CareTask.objects.filter(
            patient__username='demo_anna').exists())
        self.assertFalse(MonitoringSignal.objects.filter(
            patient__username='demo_anna').exists())

    def test_clear_demo_leaves_real_accounts_alone(self):
        """ACCEPTANCE — a prefix match against a user table has to be exact."""
        real = User.objects.create_user(
            'modemo_person', email='real@test.invalid', password='pw',
            role='patient')
        self._seed()
        call_command('clear_demo', '--apply', stdout=StringIO())

        self.assertTrue(User.objects.filter(pk=real.pk).exists())


class DemoSafetyTests(TestCase):
    """It writes people and clinical data. Production must be out of reach."""

    @override_settings(DEBUG=False)
    def test_seed_refuses_outside_debug(self):
        """ACCEPTANCE — a fabricated patient in a real system, by one command."""
        with self.assertRaises(CommandError):
            call_command('seed_demo', stdout=StringIO())

        self.assertFalse(User.objects.filter(username='demo_anna').exists())

    @override_settings(DEBUG=False)
    def test_clear_refuses_outside_debug(self):
        """It deletes user accounts by prefix. Not against production."""
        with self.assertRaises(CommandError):
            call_command('clear_demo', '--apply', stdout=StringIO())
