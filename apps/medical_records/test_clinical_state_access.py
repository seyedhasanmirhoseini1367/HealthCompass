"""
Who may read a patient's medications and conditions — and through which door.

Derived state is a new disclosure surface, and it is the kind that gets missed.
The record list was gated carefully; a summary computed from those same records
is a second path to the same facts, and if it does not carry the same limits it
quietly undoes them. Two properties are what this file is really about:

  1. no new authority — a reader who cannot reach the records cannot reach the
     state derived from them;
  2. no cutoff evasion — a frozen share must not reveal, through "current
     medications", what changed after the freeze.

The second is the one that would not have been caught by reasoning about the
model layer alone.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import (DoctorAccessLog, PatientDoctorRelationship,
                                  SharingGrant)
from apps.medical_records.models import (ConditionStatement, MedicalRecord,
                                         MedicationStatement)

User = get_user_model()


class _Base(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'csa_patient', email='csa_patient@test.invalid', password='pw', role='patient')
        self.stranger = User.objects.create_user(
            'csa_stranger', email='csa_stranger@test.invalid', password='pw', role='patient')

        self.record = MedicalRecord.objects.create(
            patient=self.patient, title='Discharge summary', record_type='discharge',
            record_date=date(2026, 1, 1))
        MedicationStatement.objects.create(
            record=self.record, patient=self.patient, name='Metformin', dose='500mg')
        ConditionStatement.objects.create(
            record=self.record, patient=self.patient, description='Type 2 diabetes')

    def _grant(self, recipient, **kwargs):
        options = dict(can_view_records=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        return SharingGrant.objects.create(
            patient=self.patient, recipient=recipient, **options)


class OwnAccessTests(_Base):

    def test_the_patient_sees_their_own_summary(self):
        self.client.force_login(self.patient)
        response = self.client.get('/records/summary/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Metformin')
        self.assertContains(response, 'Type 2 diabetes')

    def test_the_page_requires_a_login(self):
        response = self.client.get('/records/summary/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response['Location'])

    def test_a_stranger_sees_only_their_own_empty_summary(self):
        """ACCEPTANCE — no patient id is accepted from the request at all."""
        self.client.force_login(self.stranger)
        response = self.client.get('/records/summary/')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Metformin')

    def test_the_summary_is_reachable_from_the_navigation(self):
        """A page nobody can find is a page nobody has."""
        self.client.force_login(self.patient)
        response = self.client.get('/records/')

        self.assertContains(response, '/records/summary/')

    def test_a_conflict_is_shown_with_both_claims_and_their_sources(self):
        """
        ACCEPTANCE — "there is a disagreement" is not enough. The reader has to
        see what each document said and which one said it, or they cannot go
        and settle it.
        """
        later = MedicalRecord.objects.create(
            patient=self.patient, title='Same-day clinic note', record_type='other',
            record_date=self.record.record_date)
        MedicationStatement.objects.create(
            record=later, patient=self.patient, name='Metformin',
            status=MedicationStatement.Status.DISCONTINUED)

        self.client.force_login(self.patient)
        response = self.client.get('/records/summary/')

        self.assertContains(response, 'Documents disagree')
        self.assertContains(response, 'Discharge summary')
        self.assertContains(response, 'Same-day clinic note')
        self.assertContains(response, 'Being taken')
        self.assertContains(response, 'Stopped')

    def test_a_conflicted_medication_is_not_also_listed_as_current(self):
        later = MedicalRecord.objects.create(
            patient=self.patient, title='Same-day note', record_type='other',
            record_date=self.record.record_date)
        MedicationStatement.objects.create(
            record=later, patient=self.patient, name='Metformin',
            status=MedicationStatement.Status.DISCONTINUED)

        self.client.force_login(self.patient)
        response = self.client.get('/records/summary/')

        self.assertEqual(response.context['current_medications'], [])
        self.assertEqual(response.context['discontinued_medications'], [])

    def test_the_page_says_what_it_is_not(self):
        """A medication list read as a prescription list is a real hazard."""
        self.client.force_login(self.patient)
        response = self.client.get('/records/summary/')

        self.assertContains(response, 'not a prescription list')

    def test_the_record_page_shows_what_that_document_stated(self):
        self.client.force_login(self.patient)
        response = self.client.get(f'/records/{self.record.pk}/')

        self.assertContains(response, 'Metformin')

    def test_another_patients_record_page_is_not_reachable(self):
        self.client.force_login(self.stranger)
        response = self.client.get(f'/records/{self.record.pk}/')

        self.assertEqual(response.status_code, 404)


class DashboardTests(_Base):
    """
    The landing page is where a patient actually looks.

    A conflict that only appears on a page nobody visits is a conflict nobody
    resolves, which is the same outcome as not detecting it.
    """

    def test_the_dashboard_counts_current_medications_and_conditions(self):
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['current_medication_count'], 1)
        self.assertEqual(response.context['current_condition_count'], 1)

    def test_a_conflict_is_surfaced_on_the_landing_page(self):
        """ACCEPTANCE — otherwise it is only visible to someone already looking."""
        later = MedicalRecord.objects.create(
            patient=self.patient, title='Same-day note', record_type='other',
            record_date=self.record.record_date)
        MedicationStatement.objects.create(
            record=later, patient=self.patient, name='Metformin',
            status=MedicationStatement.Status.DISCONTINUED)

        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.context['clinical_conflict_count'], 1)
        self.assertContains(response, 'documents disagree')

    def test_no_conflict_means_no_warning(self):
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.context['clinical_conflict_count'], 0)
        self.assertNotContains(response, 'documents disagree')

    def test_the_counts_are_the_callers_own(self):
        self.client.force_login(self.stranger)
        response = self.client.get('/dashboard/')

        self.assertEqual(response.context['current_medication_count'], 0)


class SharedAccessTests(_Base):

    def setUp(self):
        super().setUp()
        self.family = User.objects.create_user(
            'csa_family', email='csa_family@test.invalid', password='pw', role='patient')

    def test_a_recipient_with_the_records_scope_sees_the_summary(self):
        self._grant(self.family)
        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Metformin')

    def test_a_recipient_without_the_records_scope_does_not(self):
        """
        ACCEPTANCE — medications are inside the records scope, not beside it.

        A grant of alerts only says "tell me if something is wrong, do not read
        my file". A medication list is the file.
        """
        self._grant(self.family, can_view_records=False, can_view_alerts=True)
        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Metformin')

    def test_no_grant_means_no_page(self):
        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertEqual(response.status_code, 404)

    def test_a_revoked_grant_takes_the_summary_away(self):
        grant = self._grant(self.family)
        grant.revoke(by=self.patient, reason='no longer needed')

        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertEqual(response.status_code, 404)

    def test_an_expired_grant_takes_the_summary_away(self):
        self._grant(self.family, expires_at=timezone.now() - timedelta(days=1))

        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertEqual(response.status_code, 404)

    def test_a_recipient_cannot_read_a_third_partys_summary(self):
        """The grant names one patient; the URL names another."""
        self._grant(self.family)
        other = User.objects.create_user(
            'csa_other', email='csa_other@test.invalid', password='pw', role='patient')
        other_record = MedicalRecord.objects.create(
            patient=other, title='Theirs', record_type='discharge',
            record_date=date(2026, 1, 1))
        MedicationStatement.objects.create(
            record=other_record, patient=other, name='Warfarin')

        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{other.pk}/')

        self.assertEqual(response.status_code, 404)

    def test_reading_a_shared_summary_is_recorded(self):
        self._grant(self.family)
        self.client.force_login(self.family)
        self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertTrue(DoctorAccessLog.objects.filter(
            actor=self.family, patient=self.patient).exists())

    def test_the_shared_record_page_shows_that_documents_statements(self):
        self._grant(self.family)
        self.client.force_login(self.family)
        response = self.client.get(
            f'/accounts/shared/{self.patient.pk}/record/{self.record.pk}/')

        self.assertContains(response, 'Metformin')


class CutoffTests(_Base):
    """
    A frozen share must not leak forward through derived state.

    This is the defect the feature invites: the record list is filtered by
    `uploaded_at`, so the recipient cannot open the newer document — but a
    medication summary resolved over every record would tell them the drug was
    stopped, which is precisely what the newer document says.
    """

    def setUp(self):
        super().setUp()
        self.family = User.objects.create_user(
            'csa_cut', email='csa_cut@test.invalid', password='pw', role='patient')

        # The freeze happens now; the second document arrives after it.
        self.cutoff = timezone.now()
        self.later = MedicalRecord.objects.create(
            patient=self.patient, title='Later clinic note', record_type='other',
            record_date=date(2026, 6, 1))
        MedicationStatement.objects.create(
            record=self.later, patient=self.patient, name='Metformin',
            status=MedicationStatement.Status.DISCONTINUED)
        MedicationStatement.objects.create(
            record=self.later, patient=self.patient, name='Apixaban')

        SharingGrant.objects.create(
            patient=self.patient, recipient=self.family, can_view_records=True,
            status=SharingGrant.Status.ACTIVE, data_cutoff=self.cutoff)

    def test_a_medication_first_named_after_the_cutoff_is_not_shown(self):
        """ACCEPTANCE — otherwise the summary discloses a record they cannot open."""
        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertNotContains(response, 'Apixaban')

    def test_a_post_cutoff_discontinuation_does_not_reach_the_recipient(self):
        """
        They see the state as of the freeze: still taking it.

        Uncomfortable but correct — the patient chose to share a frozen view,
        and inventing a fresher answer would override that choice. The page says
        plainly that it reflects the documents shared, not a live prescription.
        """
        self.client.force_login(self.family)
        response = self.client.get(f'/accounts/shared/{self.patient.pk}/')

        self.assertContains(response, 'Metformin')
        self.assertNotContains(response, 'No longer taken')

    def test_the_patient_themselves_sees_the_current_state(self):
        """The cutoff binds the recipient, not the owner."""
        self.client.force_login(self.patient)
        response = self.client.get('/records/summary/')

        self.assertContains(response, 'Apixaban')
        self.assertContains(response, 'No longer taken')

    def test_the_cutoff_is_enforced_on_the_state_layer_itself(self):
        """Not only in the view: the next caller gets the same guarantee."""
        from apps.medical_records.clinical_state import current_medications

        names = [r.statement.name for r in
                 current_medications(self.patient, data_cutoff=self.cutoff)]
        self.assertEqual(names, ['Metformin'])


class DoctorAccessTests(_Base):

    def setUp(self):
        super().setUp()
        self.doctor = User.objects.create_user(
            'csa_doc', email='csa_doc@test.invalid', password='pw', role='doctor')
        self.unlinked = User.objects.create_user(
            'csa_doc2', email='csa_doc2@test.invalid', password='pw', role='doctor')

    def _link(self, *, consent=True):
        """An ACTIVE relationship AND the patient's DATA_SHARING consent."""
        from apps.accounts.consent import grant_consent
        from apps.accounts.models import ConsentPurpose

        relationship = PatientDoctorRelationship.objects.create(
            doctor=self.doctor, patient=self.patient,
            status=PatientDoctorRelationship.Status.ACTIVE)
        if consent:
            grant_consent(self.patient, ConsentPurpose.DATA_SHARING)
        return relationship

    def test_a_linked_doctor_sees_the_summary(self):
        self._link()
        self.client.force_login(self.doctor)
        response = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Metformin')

    def test_an_unlinked_doctor_does_not(self):
        self.client.force_login(self.unlinked)
        response = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')

        self.assertEqual(response.status_code, 404)

    def test_revoking_data_sharing_consent_closes_the_summary(self):
        """
        ACCEPTANCE — the summary rides on `doctor_has_active_link`, which is
        link AND consent. A new page checking only the link would reopen the
        exact hole that predicate was written to close.
        """
        from apps.accounts.consent import revoke_consent
        from apps.accounts.models import ConsentPurpose

        self._link()
        revoke_consent(self.patient, ConsentPurpose.DATA_SHARING)

        self.client.force_login(self.doctor)
        response = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')

        self.assertEqual(response.status_code, 404)

    def test_a_family_grant_does_not_make_someone_a_doctor(self):
        """Two separate authorities. A share is not a clinical link."""
        SharingGrant.objects.create(
            patient=self.patient, recipient=self.doctor, can_view_records=True,
            status=SharingGrant.Status.ACTIVE)

        self.client.force_login(self.doctor)
        response = self.client.get(f'/dashboard/patient/{self.patient.pk}/records/')

        self.assertEqual(response.status_code, 404)


class ReadOnlyTests(_Base):
    """
    Nobody edits a statement through the application.

    A statement records what a document said. Editing one is not a correction,
    it is a claim that the document said something else — and the lab-value case
    already established the alternative: append, with provenance and a reason.
    Until that exists for statements, no write path is the honest position.
    """

    def test_no_url_writes_a_statement(self):
        from django.urls import get_resolver

        patterns = str(get_resolver().url_patterns)
        for word in ('medicationstatement', 'conditionstatement'):
            self.assertNotIn(word, patterns.lower())

    def test_a_recipient_cannot_post_to_the_shared_page(self):
        family = User.objects.create_user(
            'csa_ro', email='csa_ro@test.invalid', password='pw', role='patient')
        self._grant(family)

        self.client.force_login(family)
        response = self.client.post(
            f'/accounts/shared/{self.patient.pk}/',
            {'name': 'Warfarin', 'status': 'active'})

        # Whatever the view does with a POST, it must not have created anything.
        self.assertEqual(MedicationStatement.objects.filter(name='Warfarin').count(), 0)

    def test_the_summary_page_rejects_a_post(self):
        self.client.force_login(self.patient)
        self.client.post('/records/summary/', {'name': 'Warfarin'})

        self.assertEqual(MedicationStatement.objects.filter(name='Warfarin').count(), 0)


class ErasureTests(_Base):

    def test_deleting_the_record_removes_its_statements_through_the_view(self):
        self.client.force_login(self.patient)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f'/records/{self.record.pk}/delete/')

        self.assertEqual(MedicationStatement.objects.count(), 0)
        self.assertEqual(ConditionStatement.objects.count(), 0)

    def test_the_export_includes_medications_and_conditions(self):
        """GDPR access means everything held, including what was extracted."""
        from apps.accounts.export import _medical_records

        blob = str(_medical_records(self.patient))

        self.assertIn('Metformin', blob)
        self.assertIn('Type 2 diabetes', blob)
