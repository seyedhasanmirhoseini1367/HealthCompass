"""
Adversarial tests for family sharing — every way a recipient might reach further
than the patient allowed.

Written against the recipient read path, which is where the scopes are actually
spent. The earlier suites cover the predicates and the grant workflow; these
assume an attacker who has a valid session, knows object ids, and constructs
requests by hand rather than clicking links.

The governing rule: authorization is decided in the view from the grant, never
inherited from a previous page, never implied by a template, and never widened
by holding a different scope.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import DoctorAccessLog, SharingGrant
from apps.ai_insights.models import HealthAlert
from apps.appointments.models import Appointment
from apps.medical_records.models import MedicalRecord

User = get_user_model()


class _Adversary(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'adv_patient', email='adv_patient@test.invalid', password='pw', role='patient')
        self.recipient = User.objects.create_user(
            'adv_recipient', email='adv_recipient@test.invalid', password='pw', role='patient')
        self.outsider = User.objects.create_user(
            'adv_outsider', email='adv_outsider@test.invalid', password='pw', role='patient')
        self.admin = User.objects.create_superuser(
            'adv_admin', email='adv_admin@test.invalid', password='pw-admin-1')

        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Confidential panel', record_type='lab_result')
        self.record.file.save('panel.pdf', ContentFile(b'%PDF-1.4 secret'), save=True)
        self.alert = HealthAlert.objects.create(
            patient=self.patient, title='High potassium', message='m', severity='critical')
        self.appointment = Appointment.objects.create(
            patient=self.patient, title='Nephrology',
            appointment_datetime=timezone.now() + timedelta(days=5))

        self.client.force_login(self.recipient)

    def _grant(self, records=False, alerts=False, appointments=False, **kwargs):
        return SharingGrant.objects.create(
            patient=self.patient, recipient=self.recipient,
            can_view_records=records, can_view_alerts=alerts,
            can_view_appointments=appointments, **kwargs)

    def _overview(self, subject=None):
        return self.client.get(
            reverse('accounts:shared_patient', args=[(subject or self.patient).pk]))

    def _record_page(self, record=None, subject=None):
        return self.client.get(reverse(
            'accounts:shared_record',
            args=[(subject or self.patient).pk, (record or self.record).pk]))


class NoGrantTests(_Adversary):

    def test_without_a_grant_the_overview_is_not_found(self):
        self.assertEqual(self._overview().status_code, 404)

    def test_without_a_grant_a_record_is_not_found(self):
        self.assertEqual(self._record_page().status_code, 404)

    def test_no_content_leaks_in_the_refusal(self):
        """A 404 must not describe what it is refusing."""
        body = self._overview().content.decode()
        self.assertNotIn('Confidential panel', body)
        self.assertNotIn('High potassium', body)

    def test_an_outsider_with_someone_elses_grant_gets_nothing(self):
        self._grant(records=True)
        self.client.force_login(self.outsider)
        self.assertEqual(self._overview().status_code, 404)

    def test_anonymous_is_redirected_to_login(self):
        self._grant(records=True)
        self.client.logout()
        self.assertIn(self._overview().status_code, (301, 302))


class RevokedAndExpiredTests(_Adversary):

    def test_a_revoked_grant_denies_the_overview(self):
        grant = self._grant(records=True)
        self.assertEqual(self._overview().status_code, 200)

        grant.revoke(by=self.patient)
        self.assertEqual(self._overview().status_code, 404)

    def test_a_revoked_grant_denies_a_record_the_recipient_already_saw(self):
        """Revocation takes effect on the next request, not the next session."""
        grant = self._grant(records=True)
        self.assertEqual(self._record_page().status_code, 200)

        grant.revoke(by=self.patient)
        self.assertEqual(self._record_page().status_code, 404)

    def test_an_expired_grant_denies(self):
        self._grant(records=True, expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self._overview().status_code, 404)

    def test_the_expiry_boundary_denies_rather_than_allowing(self):
        """At exactly the expiry instant, access has ended."""
        grant = self._grant(records=True)
        grant.expires_at = timezone.now()
        grant.save(update_fields=['expires_at'])
        self.assertEqual(self._overview().status_code, 404)

    def test_a_null_expiry_is_ongoing_not_expired(self):
        self._grant(records=True, expires_at=None)
        self.assertEqual(self._overview().status_code, 200)

    def test_revocation_by_an_administrator_denies_immediately(self):
        grant = self._grant(records=True)
        grant.revoke(by=self.admin, reason='reported')
        self.assertEqual(self._overview().status_code, 404)


class ScopeEnforcementTests(_Adversary):
    """Holding one scope must never spend another."""

    def test_alerts_only_does_not_expose_records(self):
        self._grant(alerts=True)
        response = self._overview()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context['records'])
        self.assertNotIn('Confidential panel', response.content.decode())

    def test_alerts_only_does_not_open_a_record_page(self):
        """ACCEPTANCE — the record URL must not be reachable by guessing it."""
        self._grant(alerts=True)
        self.assertEqual(self._record_page().status_code, 404)

    def test_records_only_does_not_expose_alerts(self):
        self._grant(records=True)
        response = self._overview()

        self.assertIsNone(response.context['alerts'])
        self.assertNotIn('High potassium', response.content.decode())

    def test_records_only_does_not_expose_appointments(self):
        self._grant(records=True)
        self.assertIsNone(self._overview().context['appointments'])

    def test_appointments_only_exposes_neither_records_nor_alerts(self):
        self._grant(appointments=True)
        response = self._overview()

        self.assertIsNone(response.context['records'])
        self.assertIsNone(response.context['alerts'])
        self.assertIsNotNone(response.context['appointments'])

    def test_a_grant_with_no_scope_is_indistinguishable_from_none(self):
        self._grant()
        self.assertEqual(self._overview().status_code, 404)

    def test_alerts_show_severity_without_the_values_behind_them(self):
        """
        The reason alerts is a scope separate from records: "something is wrong"
        without disclosing the numbers.
        """
        self.alert.message = 'Potassium 6.8 mmol/L — critically high'
        self.alert.save(update_fields=['message'])
        self._grant(alerts=True)

        body = self._overview().content.decode()
        self.assertIn('High potassium', body)
        self.assertNotIn('6.8', body)


class CrossPatientTests(_Adversary):
    """A grant is for one patient's data, not a key to the object id space."""

    def test_a_record_belonging_to_someone_else_is_not_reachable(self):
        other = User.objects.create_user(
            'adv_other', email='adv_other@test.invalid', password='pw', role='patient')
        other_record = MedicalRecord.objects.create(
            patient=other, title='Someone else', record_type='lab_result')
        self._grant(records=True)

        response = self.client.get(reverse(
            'accounts:shared_record', args=[self.patient.pk, other_record.pk]))
        self.assertEqual(response.status_code, 404)

    def test_substituting_another_patient_id_is_refused(self):
        other = User.objects.create_user(
            'adv_other2', email='adv_other2@test.invalid', password='pw', role='patient')
        self._grant(records=True)

        self.assertEqual(self._overview(subject=other).status_code, 404)


class DataCutoffTests(_Adversary):
    """A frozen share shows the record as it stood, not as it grows."""

    def test_a_record_created_after_the_cutoff_is_hidden(self):
        cutoff = timezone.now()
        later = MedicalRecord.objects.create(
            patient=self.patient, title='Added later', record_type='lab_result')
        self._grant(records=True, data_cutoff=cutoff)

        body = self._overview().content.decode()
        self.assertNotIn('Added later', body)
        self.assertTrue(later.pk)

    def test_the_later_record_is_not_reachable_directly_either(self):
        """ACCEPTANCE — the cutoff must bind the detail page, not only the list."""
        cutoff = timezone.now()
        later = MedicalRecord.objects.create(
            patient=self.patient, title='Added later', record_type='lab_result')
        self._grant(records=True, data_cutoff=cutoff)

        self.assertEqual(self._record_page(record=later).status_code, 404)

    def test_records_before_the_cutoff_remain_visible(self):
        self._grant(records=True, data_cutoff=timezone.now() + timedelta(days=1))
        self.assertEqual(self._record_page().status_code, 200)


class ReadOnlyTests(_Adversary):
    """Sharing is an access authority, never an edit authority."""

    def test_a_recipient_cannot_delete_a_shared_record(self):
        self._grant(records=True)

        response = self.client.post(
            reverse('medical_records:delete', args=[self.record.pk]))

        self.assertIn(response.status_code, (403, 404))
        self.assertTrue(MedicalRecord.objects.filter(pk=self.record.pk).exists())

    def test_a_recipient_cannot_see_the_patients_records_in_their_own_list(self):
        """The owner's own views stay scoped to the owner."""
        self._grant(records=True)
        body = self.client.get(reverse('medical_records:list')).content.decode()
        self.assertNotIn('Confidential panel', body)

    def test_a_recipient_cannot_correct_a_clinical_value(self):
        from apps.accounts.authz import can_correct_clinical_value
        self._grant(records=True)
        self.assertFalse(can_correct_clinical_value(self.recipient))

    def test_a_recipient_cannot_reach_the_api_for_the_patients_records(self):
        from rest_framework.test import APIClient

        self._grant(records=True)
        api = APIClient()
        api.force_authenticate(user=self.recipient)

        body = api.get(reverse('api:records_list')).content.decode()
        self.assertNotIn('Confidential panel', body)


class OnwardDelegationTests(_Adversary):
    """A recipient cannot pass access along."""

    def test_sharing_while_holding_a_grant_shares_only_your_own_data(self):
        """
        ACCEPTANCE — the grantor is always request.user, so a recipient
        "re-sharing" creates a grant over their own record, not the patient's.
        """
        self._grant(records=True)

        self.client.post(reverse('accounts:create_share'),
                         {'recipient': 'adv_outsider', 'scopes': ['records']},
                         follow=True)

        created = SharingGrant.objects.get(recipient=self.outsider)
        self.assertEqual(created.patient, self.recipient)
        self.assertNotEqual(created.patient, self.patient)

    def test_the_outsider_still_cannot_see_the_original_patient(self):
        self._grant(records=True)
        SharingGrant.objects.create(
            patient=self.recipient, recipient=self.outsider, can_view_records=True)

        self.client.force_login(self.outsider)
        self.assertEqual(self._overview().status_code, 404)


class AdministrativeBoundaryTests(_Adversary):

    def test_an_administrator_cannot_open_the_shared_overview(self):
        """ACCEPTANCE — being staff is not a way to become someone's family."""
        self._grant(records=True)
        self.client.force_login(self.admin)
        self.assertEqual(self._overview().status_code, 404)

    def test_an_administrator_cannot_open_a_shared_record_page(self):
        self._grant(records=True)
        self.client.force_login(self.admin)
        self.assertEqual(self._record_page().status_code, 404)


class AuditTests(_Adversary):
    """Every shared read lands in the patient's own trail."""

    def test_opening_the_overview_is_recorded(self):
        self._grant(records=True)
        DoctorAccessLog.objects.all().delete()

        self._overview()

        entry = DoctorAccessLog.objects.get()
        self.assertEqual(entry.actor, self.recipient)
        self.assertEqual(entry.patient, self.patient)
        self.assertIn('shared', entry.resource)

    def test_opening_a_record_is_recorded_with_its_identifier(self):
        self._grant(records=True)
        DoctorAccessLog.objects.all().delete()

        self._record_page()

        entry = DoctorAccessLog.objects.get()
        self.assertIn(str(self.record.pk), entry.resource)

    def test_a_refused_read_writes_no_access_entry(self):
        """The trail records disclosures, not attempts."""
        DoctorAccessLog.objects.all().delete()
        self._overview()
        self.assertEqual(DoctorAccessLog.objects.count(), 0)

    def test_the_trail_names_no_clinical_content(self):
        self._grant(records=True)
        DoctorAccessLog.objects.all().delete()

        self._record_page()

        resource = DoctorAccessLog.objects.get().resource
        self.assertNotIn('Confidential', resource)


class CorrectedValuesTests(_Adversary):
    """A family member sees the reading that stands, marked as corrected."""

    def test_a_corrected_value_is_shown_and_labelled(self):
        from apps.medical_records.models import LabValueCorrection, ParsedLabValue

        value = ParsedLabValue.objects.create(
            record=self.record, parameter_name='Potassium', value='6.8',
            unit='mmol/L', canonical_value=6.8, original_unit='mmol/L')
        LabValueCorrection.objects.create(
            original=value, value='4.8', canonical_value=4.8, unit='mmol/L',
            reason='Column misaligned', actor=self.admin)

        self._grant(records=True)
        body = self._record_page().content.decode()

        self.assertIn('4.8', body)
        self.assertIn('corrected', body)
