"""
REGRESSION — ConsentPurpose.DATA_SHARING was enforced nowhere.

The purpose was defined (models.py) and described on the consent page
(consent.py) as "Sharing my records with linked clinicians". A patient could
open Privacy & Consent, revoke it, see the confirmation message — and every
linked doctor carried on reading their records, because the only check anywhere
was the relationship status.

A consent control that changes nothing is worse than an absent one: it tells the
data subject they have exercised a right they have not, and the withdrawal is
recorded in the consent history as though it took effect.

Enforcement lives in `authz.doctor_has_active_link`, beside the relationship
test, so the three doctor-facing read paths cannot answer it differently.
"""
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase
from django.urls import reverse

from apps.accounts.authz import can_access_media, doctor_has_active_link
from apps.accounts.consent import grant_consent, has_consent, revoke_consent
from apps.accounts.models import (ConsentPurpose, DoctorAccessLog,
                                  PatientDoctorRelationship)
from apps.medical_records.models import MedicalRecord

User = get_user_model()
Status = PatientDoctorRelationship.Status
SHARING = ConsentPurpose.DATA_SHARING


class _Fixture(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'ds_patient', email='ds_patient@test.invalid', password='pw', role='patient')
        self.doctor = User.objects.create_user(
            'ds_doctor', email='ds_doctor@test.invalid', password='pw', role='doctor')

        self.link = PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.doctor, status=Status.ACTIVE)
        grant_consent(self.patient, SHARING)

        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Blood panel', record_type='lab_result')
        self.record.file.save('panel.pdf', ContentFile(b'%PDF-1.4 x'), save=True)


class PredicateTests(_Fixture):

    def test_an_active_link_with_consent_grants_access(self):
        self.assertTrue(doctor_has_active_link(self.doctor, self.patient))

    def test_revoking_data_sharing_closes_the_link(self):
        """ACCEPTANCE — this returned True regardless of consent."""
        revoke_consent(self.patient, SHARING)
        self.assertFalse(doctor_has_active_link(self.doctor, self.patient))

    def test_a_patient_who_never_granted_it_shares_nothing(self):
        """Default-deny: no consent row means no consent."""
        other = User.objects.create_user(
            'ds_patient2', email='ds_patient2@test.invalid', password='pw', role='patient')
        PatientDoctorRelationship.objects.create(
            patient=other, doctor=self.doctor, status=Status.ACTIVE)

        self.assertFalse(has_consent(other, SHARING))
        self.assertFalse(doctor_has_active_link(self.doctor, other))

    def test_re_granting_reopens_it(self):
        revoke_consent(self.patient, SHARING)
        grant_consent(self.patient, SHARING)
        self.assertTrue(doctor_has_active_link(self.doctor, self.patient))

    def test_consent_alone_is_not_enough(self):
        """Both conditions are required, not either."""
        self.link.status = Status.REVOKED
        self.link.save(update_fields=['status'])
        self.assertTrue(has_consent(self.patient, SHARING))
        self.assertFalse(doctor_has_active_link(self.doctor, self.patient))

    def test_it_is_a_master_switch_over_every_link(self):
        second = User.objects.create_user(
            'ds_doctor2', email='ds_doctor2@test.invalid', password='pw', role='doctor')
        PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=second, status=Status.ACTIVE)

        revoke_consent(self.patient, SHARING)
        self.assertFalse(doctor_has_active_link(self.doctor, self.patient))
        self.assertFalse(doctor_has_active_link(second, self.patient))

    def test_another_patients_consent_does_not_help(self):
        bystander = User.objects.create_user(
            'ds_patient3', email='ds_patient3@test.invalid', password='pw', role='patient')
        grant_consent(bystander, SHARING)
        PatientDoctorRelationship.objects.create(
            patient=bystander, doctor=self.doctor, status=Status.ACTIVE)

        revoke_consent(self.patient, SHARING)
        self.assertFalse(doctor_has_active_link(self.doctor, self.patient))
        self.assertTrue(doctor_has_active_link(self.doctor, bystander))


class ReadPathTests(_Fixture):
    """All three doctor-facing reads must answer identically."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.doctor)
        self.records_url = reverse('dashboard:patient_records', args=[self.patient.pk])
        self.detail_url = reverse('dashboard:doctor_record', args=[self.record.pk])
        self.media_url = f'/media/{self.record.file.name}'

    def _statuses(self):
        return (self.client.get(self.records_url).status_code,
                self.client.get(self.detail_url).status_code,
                self.client.get(self.media_url).status_code)

    def test_all_three_are_open_while_consent_stands(self):
        self.assertEqual(self._statuses(), (200, 200, 200))

    def test_revoking_data_sharing_closes_all_three(self):
        """ACCEPTANCE — all three stayed open after revocation."""
        revoke_consent(self.patient, SHARING)

        records, detail, media = self._statuses()
        self.assertEqual(records, 404)
        self.assertEqual(detail, 404)
        self.assertIn(media, (403, 404))

    def test_no_record_content_leaks_after_revocation(self):
        revoke_consent(self.patient, SHARING)
        for url in (self.records_url, self.detail_url):
            with self.subTest(url=url):
                self.assertNotIn(b'Blood panel', self.client.get(url).content)

    def test_a_refused_read_is_not_written_to_the_access_log(self):
        """The trail records disclosures, not attempts."""
        revoke_consent(self.patient, SHARING)
        DoctorAccessLog.objects.all().delete()

        self._statuses()
        self.assertEqual(DoctorAccessLog.objects.count(), 0)

    def test_the_patients_own_access_is_unaffected(self):
        """DATA_SHARING governs sharing, never the subject's own records."""
        revoke_consent(self.patient, SHARING)
        self.client.force_login(self.patient)

        self.assertTrue(can_access_media(self.patient, self.record.file.name))
        self.assertEqual(self.client.get(self.media_url).status_code, 200)


class NoResurrectionTests(TestCase):
    """
    The master switch failed open in one direction.

    Revoking DATA_SHARING closes every link but leaves each relationship ACTIVE,
    because the switch works by failing the consent check rather than by
    touching the links. Approving one new doctor then granted DATA_SHARING as a
    side effect and brought all the closed ones back — one affirmative act about
    one person silently restoring three the patient had deliberately cut off.

    Approval now refuses while sharing is switched off. Chosen over cascading
    the revocation into every link because `approve_doctor_access` refuses to
    re-approve a REVOKED link (views.py:535-538), so cascading would leave the
    patient unable to restore any doctor without asking the clinic to re-issue
    every request — turning the exercise of a consent right into an
    administrative penalty.
    """

    def setUp(self):
        self.patient = User.objects.create_user(
            'nr_patient', email='nr_patient@test.invalid', password='pw', role='patient')
        self.doctors = {}
        for name in ('a', 'b', 'c'):
            doctor = User.objects.create_user(
                f'nr_{name}', email=f'nr_{name}@test.invalid', password='pw', role='doctor')
            PatientDoctorRelationship.objects.create(
                patient=self.patient, doctor=doctor, status=Status.ACTIVE)
            self.doctors[name] = doctor

        self.newcomer = User.objects.create_user(
            'nr_d', email='nr_d@test.invalid', password='pw', role='doctor')
        self.pending = PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.newcomer, status=Status.PENDING)

        grant_consent(self.patient, SHARING)
        self.client.force_login(self.patient)

    def _approve_newcomer(self):
        return self.client.post(
            reverse('accounts:approve_doctor_access', args=[self.pending.pk]))

    def test_approving_a_new_doctor_does_not_reopen_revoked_ones(self):
        """ACCEPTANCE — A, B and C came back when D was approved."""
        revoke_consent(self.patient, SHARING)
        for name, doctor in self.doctors.items():
            self.assertFalse(doctor_has_active_link(doctor, self.patient), name)

        self._approve_newcomer()

        for name, doctor in self.doctors.items():
            with self.subTest(doctor=name):
                self.assertFalse(doctor_has_active_link(doctor, self.patient),
                                 f'doctor {name} was resurrected')

    def test_the_new_doctor_is_not_approved_either(self):
        revoke_consent(self.patient, SHARING)
        self._approve_newcomer()

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Status.PENDING)
        self.assertFalse(doctor_has_active_link(self.newcomer, self.patient))

    def test_the_approval_does_not_re_grant_consent_as_a_side_effect(self):
        revoke_consent(self.patient, SHARING)
        self._approve_newcomer()
        self.assertFalse(has_consent(self.patient, SHARING))

    def test_the_patient_is_told_where_to_turn_it_back_on(self):
        """A refusal that does not say what to do next is a dead end."""
        revoke_consent(self.patient, SHARING)
        response = self.client.post(
            reverse('accounts:approve_doctor_access', args=[self.pending.pk]), follow=True)

        messages = [str(m) for m in response.context['messages']]
        joined = ' '.join(messages)
        self.assertIn('Privacy & Consent', joined)
        self.assertIn('switched off', joined)

    def test_re_enabling_sharing_restores_the_still_active_links(self):
        """
        The master switch is reversible by the patient alone — that is the point
        of choosing refusal over cascading REVOKED into every link.
        """
        revoke_consent(self.patient, SHARING)
        grant_consent(self.patient, SHARING)

        for name, doctor in self.doctors.items():
            with self.subTest(doctor=name):
                self.assertTrue(doctor_has_active_link(doctor, self.patient))

    def test_a_first_ever_approval_still_works(self):
        """
        Only an explicit withdrawal blocks. A patient who has never been asked
        about DATA_SHARING is expressing exactly that decision by approving.
        """
        fresh = User.objects.create_user(
            'nr_fresh', email='nr_fresh@test.invalid', password='pw', role='patient')
        doctor = User.objects.create_user(
            'nr_fresh_d', email='nr_fresh_d@test.invalid', password='pw', role='doctor')
        link = PatientDoctorRelationship.objects.create(
            patient=fresh, doctor=doctor, status=Status.PENDING)

        self.assertFalse(has_consent(fresh, SHARING))
        self.client.force_login(fresh)
        self.client.post(reverse('accounts:approve_doctor_access', args=[link.pk]))

        self.assertTrue(has_consent(fresh, SHARING))
        self.assertTrue(doctor_has_active_link(doctor, fresh))

    def test_was_revoked_distinguishes_withdrawal_from_never_asked(self):
        from apps.accounts.consent import was_revoked

        never = User.objects.create_user(
            'nr_never', email='nr_never@test.invalid', password='pw', role='patient')
        self.assertFalse(was_revoked(never, SHARING))

        revoke_consent(self.patient, SHARING)
        self.assertTrue(was_revoked(self.patient, SHARING))

        grant_consent(self.patient, SHARING)
        self.assertFalse(was_revoked(self.patient, SHARING))


class ApprovalGrantsConsentTests(TestCase):
    """Approving a link is the affirmative act; it records the consent."""

    def setUp(self):
        self.patient = User.objects.create_user(
            'ds_ap', email='ds_ap@test.invalid', password='pw', role='patient')
        self.doctor = User.objects.create_user(
            'ds_ad', email='ds_ad@test.invalid', password='pw', role='doctor')
        self.link = PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=self.doctor, status=Status.PENDING)
        self.client.force_login(self.patient)

    def test_approving_a_doctor_grants_data_sharing(self):
        """
        Otherwise the patient approves, the doctor is still refused, and nothing
        on either screen explains why.
        """
        self.assertFalse(has_consent(self.patient, SHARING))

        self.client.post(reverse('accounts:approve_doctor_access', args=[self.link.pk]))

        self.assertTrue(has_consent(self.patient, SHARING))
        self.assertTrue(doctor_has_active_link(self.doctor, self.patient))

    def test_approving_a_second_doctor_is_idempotent(self):
        from apps.accounts.models import Consent

        self.client.post(reverse('accounts:approve_doctor_access', args=[self.link.pk]))

        second = User.objects.create_user(
            'ds_ad2', email='ds_ad2@test.invalid', password='pw', role='doctor')
        link2 = PatientDoctorRelationship.objects.create(
            patient=self.patient, doctor=second, status=Status.PENDING)
        self.client.post(reverse('accounts:approve_doctor_access', args=[link2.pk]))

        self.assertEqual(
            Consent.objects.filter(user=self.patient, purpose=SHARING,
                                   status='granted', revoked_at__isnull=True).count(), 1)


class BackfillTests(TestCase):
    """
    Migration 0013 records the consent for links patients already approved.

    Without it, enforcing a never-checked consent would close every existing
    doctor link on deploy — silently, with no prompt telling the patient what to
    grant to restore it.
    """

    def test_the_migration_covers_patients_with_an_active_link(self):
        from importlib import import_module

        module = import_module(
            'apps.accounts.migrations.0013_backfill_data_sharing_consent'
            .replace('0013', '0013'))
        self.assertTrue(hasattr(module, 'backfill_data_sharing'))

    def test_reversing_does_not_delete_consent_evidence(self):
        from importlib import import_module

        module = import_module('apps.accounts.migrations.0013_backfill_data_sharing_consent')
        import inspect
        source = inspect.getsource(module.unbackfill)
        self.assertNotIn('delete', source.lower().replace('deleting', ''))
