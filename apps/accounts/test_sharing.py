"""
Family / shared access — the patient hands a specific person a specific key.

Two mechanisms already existed and neither serves the case. A
`PatientDoctorRelationship` is created by a hospital admin for a clinician; the
emergency card is an unauthenticated token readable by anyone holding the URL.
Neither answers "let my daughter check on me" or "tell me if something is wrong
with my father, but do not let her read my file".

The rules that matter are here as executable assertions rather than prose,
because every one of them is a way patient data could leak.

Boundaries these tests defend
-----------------------------
* A family share and a clinical link are different authorities, checked
  separately. A share must not satisfy a clinical rule it was never tested
  against, and a clinician's DATA_SHARING consent must not be reachable through
  a family grant.
* Administrative authority is not a way to become someone's family. An
  administrator can revoke an abusive grant and read its metadata, and cannot
  read the records behind it or create a grant on anyone's behalf.
* Nothing depends on the UI hiding anything: every rule is asserted through the
  authorization seam and through the HTTP boundary.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts import authz
from apps.accounts.consent import grant_consent, revoke_consent
from apps.accounts.models import (ConsentPurpose, DoctorAccessLog,
                                  PatientDoctorRelationship, SharingGrant)
from apps.medical_records.models import MedicalRecord

User = get_user_model()
SHARING = ConsentPurpose.DATA_SHARING


class _Sharing(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'sh_patient', email='sh_patient@test.invalid', password='pw', role='patient')
        self.daughter = User.objects.create_user(
            'sh_daughter', email='sh_daughter@test.invalid', password='pw', role='patient')
        self.stranger = User.objects.create_user(
            'sh_stranger', email='sh_stranger@test.invalid', password='pw', role='patient')
        self.admin = User.objects.create_superuser(
            'sh_admin', email='sh_admin@test.invalid', password='pw-admin-1')

    def _grant(self, recipient=None, records=True, alerts=False,
               appointments=False, **kwargs):
        return SharingGrant.objects.create(
            patient=self.patient, recipient=recipient or self.daughter,
            can_view_records=records, can_view_alerts=alerts,
            can_view_appointments=appointments, **kwargs)

    def _record(self):
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Blood panel', record_type='lab_result')
        record.file.save('panel.pdf', ContentFile(b'%PDF-1.4 x'), save=True)
        return record


class GrantLifecycleTests(_Sharing):

    def test_an_active_grant_gives_the_named_scope(self):
        self._grant(records=True)
        self.assertTrue(authz.can_view_shared_records(self.daughter, self.patient))

    def test_a_grant_gives_nothing_it_does_not_name(self):
        """Sharing must never be broader than what was authorised."""
        self._grant(records=False, alerts=True)

        self.assertFalse(authz.can_view_shared_records(self.daughter, self.patient))
        self.assertIsNotNone(authz.sharing_grant(self.daughter, self.patient, 'alerts'))
        self.assertIsNone(authz.sharing_grant(self.daughter, self.patient, 'appointments'))

    def test_a_grant_with_no_scope_grants_nothing(self):
        self._grant(records=False, alerts=False, appointments=False)
        for scope in authz.SHARE_SCOPES:
            self.assertIsNone(authz.sharing_grant(self.daughter, self.patient, scope))

    def test_revocation_denies_immediately(self):
        grant = self._grant()
        self.assertTrue(authz.can_view_shared_records(self.daughter, self.patient))

        grant.revoke(by=self.patient)

        self.assertFalse(authz.can_view_shared_records(self.daughter, self.patient))

    def test_an_expired_grant_denies_without_anyone_revoking_it(self):
        self._grant(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertFalse(authz.can_view_shared_records(self.daughter, self.patient))

    def test_a_future_expiry_still_allows(self):
        self._grant(expires_at=timezone.now() + timedelta(days=30))
        self.assertTrue(authz.can_view_shared_records(self.daughter, self.patient))

    def test_an_unusable_expiry_denies_rather_than_failing_open(self):
        grant = self._grant()
        grant.expires_at = 'not a datetime'
        self.assertFalse(grant.is_effective)

    def test_an_unknown_scope_is_refused(self):
        self._grant(records=True)
        self.assertIsNone(authz.sharing_grant(self.daughter, self.patient, 'everything'))

    def test_a_stranger_gets_nothing(self):
        self._grant()
        self.assertFalse(authz.can_view_shared_records(self.stranger, self.patient))

    def test_sharing_is_not_symmetric(self):
        """A grant lets them see me; it does not let me see them."""
        self._grant()
        self.assertFalse(authz.can_view_shared_records(self.patient, self.daughter))

    def test_anonymous_gets_nothing(self):
        from django.contrib.auth.models import AnonymousUser

        self._grant()
        self.assertFalse(authz.can_view_shared_records(AnonymousUser(), self.patient))

    def test_revocation_records_who_and_why(self):
        grant = self._grant()
        grant.revoke(by=self.patient, reason='no longer needed')

        grant.refresh_from_db()
        self.assertEqual(grant.revoked_by, self.patient)
        self.assertEqual(grant.revoke_reason, 'no longer needed')
        self.assertIsNotNone(grant.revoked_at)


class DatabaseConstraintTests(_Sharing):
    """Invariants the database enforces, not only the application."""

    def test_a_duplicate_grant_is_refused(self):
        self._grant()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._grant()

    def test_a_self_grant_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SharingGrant.objects.create(
                    patient=self.patient, recipient=self.patient,
                    can_view_records=True)

    def test_the_seam_also_refuses_a_self_grant(self):
        self.assertIsNone(authz.sharing_grant(self.patient, self.patient, 'records'))


class AdministrativeBoundaryTests(_Sharing):
    """An administrator is not a way to become someone's family."""

    def test_an_administrator_does_not_inherit_shared_access(self):
        """ACCEPTANCE — holding a grant is the only route, and admins hold none."""
        self._grant()
        self.assertFalse(authz.can_view_shared_records(self.admin, self.patient))

    def test_an_administrator_cannot_create_a_grant_for_a_patient(self):
        self.assertFalse(authz.can_create_grant(self.admin, self.patient))

    def test_only_the_patient_can_create_their_own_grant(self):
        self.assertTrue(authz.can_create_grant(self.patient, self.patient))
        self.assertFalse(authz.can_create_grant(self.daughter, self.patient))

    def test_an_administrator_may_revoke_an_abusive_grant(self):
        """Asymmetric on purpose: removing access can protect someone."""
        grant = self._grant()
        self.assertTrue(authz.can_revoke_grant(self.admin, grant))

    def test_the_patient_may_revoke_their_own_grant(self):
        self.assertTrue(authz.can_revoke_grant(self.patient, self._grant()))

    def test_a_recipient_cannot_revoke_the_grant_they_hold(self):
        self.assertFalse(authz.can_revoke_grant(self.daughter, self._grant()))

    def test_metadata_inspection_does_not_confer_record_access(self):
        """The whole point of separating the two capabilities."""
        self._grant()
        self.assertTrue(authz.can_inspect_grant_metadata(self.admin))
        self.assertFalse(authz.can_view_shared_records(self.admin, self.patient))

    def test_a_patient_cannot_inspect_everyone_elses_grants(self):
        self.assertFalse(authz.can_inspect_grant_metadata(self.patient))


class MediaAccessTests(_Sharing):
    """Shared reads go through the same path as every other file read."""

    def test_a_recipient_can_open_a_shared_file(self):
        record = self._record()
        self._grant(records=True)

        self.assertTrue(authz.can_access_media(self.daughter, record.file.name))

    def test_the_read_appears_in_the_patients_access_trail(self):
        record = self._record()
        self._grant(records=True)
        DoctorAccessLog.objects.all().delete()

        authz.can_access_media(self.daughter, record.file.name)

        entry = DoctorAccessLog.objects.get()
        self.assertEqual(entry.actor, self.daughter)
        self.assertEqual(entry.patient, self.patient)

    def test_an_alerts_only_grant_does_not_open_files(self):
        record = self._record()
        self._grant(records=False, alerts=True)

        self.assertFalse(authz.can_access_media(self.daughter, record.file.name))

    def test_a_revoked_grant_closes_the_file(self):
        record = self._record()
        grant = self._grant(records=True)
        grant.revoke(by=self.patient)

        self.assertFalse(authz.can_access_media(self.daughter, record.file.name))

    def test_the_http_boundary_agrees_with_the_seam(self):
        """No rule may depend on the UI: the endpoint enforces it too."""
        record = self._record()
        url = f'/media/{record.file.name}'

        self.client.force_login(self.daughter)
        self.assertEqual(self.client.get(url).status_code, 403)

        self._grant(records=True)
        self.assertEqual(self.client.get(url).status_code, 200)


class ClinicalAuthorizationIsUnaffectedTests(_Sharing):
    """
    A family share and a clinical link are different authorities. Neither may
    widen the other.
    """

    def setUp(self):
        super().setUp()
        self.doctor = User.objects.create_user(
            'sh_doctor', email='sh_doctor@test.invalid', password='pw', role='doctor')

    def _link(self, status=PatientDoctorRelationship.Status.ACTIVE):
        return PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.doctor, status=status)

    def test_a_share_does_not_satisfy_the_doctor_link_rule(self):
        """ACCEPTANCE — a grant must not become a clinical relationship."""
        SharingGrant.objects.create(
            patient=self.patient, recipient=self.doctor, can_view_records=True)
        grant_consent(self.patient, SHARING)

        self.assertFalse(authz.doctor_has_active_link(self.doctor, self.patient))

    def test_a_doctor_link_does_not_create_a_share(self):
        self._link()
        grant_consent(self.patient, SHARING)

        self.assertFalse(authz.can_view_shared_records(self.doctor, self.patient))

    def test_data_sharing_consent_still_gates_the_clinical_route(self):
        self._link()
        grant_consent(self.patient, SHARING)
        self.assertTrue(authz.doctor_has_active_link(self.doctor, self.patient))

        revoke_consent(self.patient, SHARING)
        self.assertFalse(authz.doctor_has_active_link(self.doctor, self.patient))

    def test_revoking_data_sharing_does_not_revoke_a_family_grant(self):
        """
        DATA_SHARING is described to the patient as sharing with CLINICIANS.
        Silently ending a family share would be doing something they did not ask
        for, under a switch labelled for something else.
        """
        self._grant(recipient=self.daughter, records=True)
        grant_consent(self.patient, SHARING)
        revoke_consent(self.patient, SHARING)

        self.assertTrue(authz.can_view_shared_records(self.daughter, self.patient))

    def test_a_doctor_holding_a_family_grant_reads_through_the_grant_only(self):
        """
        A clinician can also be a family member. The grant gives what the
        patient granted; it does not give the clinical route.
        """
        SharingGrant.objects.create(
            patient=self.patient, recipient=self.doctor, can_view_records=True)

        self.assertTrue(authz.can_view_shared_records(self.doctor, self.patient))
        self.assertFalse(authz.doctor_has_active_link(self.doctor, self.patient))

    def test_a_revoked_grant_with_an_active_link_still_allows_the_clinical_route(self):
        self._link()
        grant_consent(self.patient, SHARING)
        grant = SharingGrant.objects.create(
            patient=self.patient, recipient=self.doctor, can_view_records=True)
        grant.revoke(by=self.patient)

        self.assertFalse(authz.can_view_shared_records(self.doctor, self.patient))
        self.assertTrue(authz.doctor_has_active_link(self.doctor, self.patient))


class EmergencyCardIsSeparateTests(_Sharing):
    """
    The emergency card is an unauthenticated token. Sharing is an authenticated,
    named, revocable grant. Neither may expand the other.
    """

    def test_a_share_does_not_enable_the_emergency_card(self):
        from apps.accounts.models import PatientProfile

        profile, _ = PatientProfile.objects.get_or_create(user=self.patient)
        self.assertFalse(profile.emergency_card_enabled)

        self._grant(records=True)

        profile.refresh_from_db()
        self.assertFalse(profile.emergency_card_enabled,
                         'sharing must not switch on a public token')

    def test_the_emergency_card_does_not_create_a_share(self):
        from apps.accounts.models import PatientProfile

        profile, _ = PatientProfile.objects.get_or_create(
            user=self.patient, defaults={'emergency_card_enabled': True})
        profile.emergency_card_enabled = True
        profile.save(update_fields=['emergency_card_enabled'])

        self.assertFalse(authz.can_view_shared_records(self.daughter, self.patient))


class ListingTests(_Sharing):

    def test_a_patient_can_see_who_they_share_with(self):
        self._grant()
        grants = SharingGrant.objects.filter(patient=self.patient)
        self.assertEqual([g.recipient for g in grants], [self.daughter])

    def test_shared_with_lists_only_effective_grants(self):
        self._grant(recipient=self.daughter, records=True)
        expired = SharingGrant.objects.create(
            patient=self.patient, recipient=self.stranger, can_view_records=True,
            expires_at=timezone.now() - timedelta(days=1))

        shared = authz.shared_with(self.daughter, 'records')
        self.assertEqual(shared, [self.patient])
        self.assertEqual(authz.shared_with(self.stranger, 'records'), [])
        self.assertTrue(expired.pk)

    def test_shared_with_respects_scope(self):
        self._grant(records=False, alerts=True)
        self.assertEqual(authz.shared_with(self.daughter, 'records'), [])
        self.assertEqual(authz.shared_with(self.daughter, 'alerts'), [self.patient])


class DeletionLifecycleTests(_Sharing):
    """No dangling grants, and sharing metadata must not retain PHI."""

    def test_deleting_the_patient_removes_their_grants(self):
        self._grant()
        self.patient.delete()
        self.assertEqual(SharingGrant.objects.count(), 0)

    def test_deleting_the_recipient_removes_the_grant(self):
        self._grant()
        self.daughter.delete()
        self.assertEqual(SharingGrant.objects.count(), 0)

    def test_gdpr_erasure_removes_grants_in_both_directions(self):
        from apps.accounts.services import purge_user_data

        self._grant()
        SharingGrant.objects.create(
            patient=self.daughter, recipient=self.patient, can_view_records=True)

        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(self.patient)

        self.assertEqual(SharingGrant.objects.count(), 0)

    def test_a_revoked_grant_survives_until_a_party_is_deleted(self):
        """Revocation is evidence: the patient can show a share once existed."""
        grant = self._grant()
        grant.revoke(by=self.patient)
        self.assertTrue(SharingGrant.objects.filter(pk=grant.pk).exists())

    def test_deleting_the_revoker_does_not_delete_the_grant(self):
        grant = self._grant()
        grant.revoke(by=self.admin, reason='reported')
        self.admin.delete()

        grant.refresh_from_db()
        self.assertIsNone(grant.revoked_by)
        self.assertEqual(grant.status, SharingGrant.Status.REVOKED)
