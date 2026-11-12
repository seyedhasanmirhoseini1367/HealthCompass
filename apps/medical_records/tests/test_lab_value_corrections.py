"""
Clinical corrections are appended, never written over the original.

Lab values come from LLM extraction of uploaded documents, so they can be wrong:
a misread digit, an unrecognised unit. A wrong unit can raise a false critical
alert, so correcting them has to be possible — and until now the only way was to
edit the ParsedLabValue row in the Django admin, which destroyed what the source
document actually said.

That is not tidiness. The original extraction is evidence of what the document
contained and of what the patient was told at the time. An alert that fired on
5.2 cannot be explained by a row that now reads 52, and "the system said X"
becomes unanswerable.

Chain shape
-----------
Every correction points at the ORIGINAL value, never at the correction before
it, so correcting a correction appends another row against the same original.
A cycle is structurally impossible rather than something to detect, and the
effective value is simply the newest row — resolved by (created_at, id) so two
corrections in the same tick still give one deterministic answer.

The substitution happens in exactly one place: `_to_observation` in
clinical_facts. Every accessor — latest, previous, on_date, contested_dates,
trend — goes through it, so they became correction-aware together rather than
each remembering to.
"""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.medical_records import clinical_facts as facts
from apps.medical_records.models import (LabValueCorrection, MedicalRecord,
                                         ParsedLabValue)

User = get_user_model()


class _Values(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'corr_patient', email='corr_patient@test.invalid', password='pw', role='patient')
        self.admin = User.objects.create_superuser(
            'corr_admin', email='corr_admin@test.invalid', password='pw-admin-1')

    def _value(self, value='5.2', canonical=5.2, analyte='Glucose',
               when=date(2026, 1, 10), unit='mmol/L', critical=False):
        record = MedicalRecord.objects.create(
            patient=self.patient, title=f'Panel {when}', record_type='lab_result',
            record_date=when)
        return ParsedLabValue.objects.create(
            record=record, parameter_name=analyte, value=value, unit=unit,
            canonical_value=canonical, original_unit=unit, unit_known=True,
            is_critical=critical)

    def _correct(self, original, value='52', canonical=52.0, unit='mmol/L',
                 reason='Misread digit in the source PDF', actor=None):
        return LabValueCorrection.objects.create(
            original=original, value=value, canonical_value=canonical, unit=unit,
            original_unit=unit, unit_known=True, reason=reason,
            actor=actor or self.admin, source='re-read of the source document')


class OriginalIsImmutableTests(_Values):

    def test_correcting_does_not_change_the_original_row(self):
        """ACCEPTANCE — the admin edit used to overwrite this."""
        original = self._value(value='5.2', canonical=5.2)
        self._correct(original, value='52', canonical=52.0)

        original.refresh_from_db()
        self.assertEqual(original.value, '5.2')
        self.assertEqual(original.canonical_value, 5.2)

    def test_the_extracted_value_stays_reconstructable(self):
        original = self._value(value='5.2', canonical=5.2)
        self._correct(original)

        stored = ParsedLabValue.objects.get(pk=original.pk)
        self.assertEqual(stored.value, '5.2', 'the document said 5.2 and must keep saying so')

    def test_the_correction_records_its_provenance(self):
        original = self._value()
        correction = self._correct(original, reason='Unit column misaligned')

        self.assertEqual(correction.actor, self.admin)
        self.assertIn('corr_admin', correction.actor_label)
        self.assertEqual(correction.reason, 'Unit column misaligned')
        self.assertTrue(correction.source)
        self.assertIsNotNone(correction.created_at)

    def test_the_actor_label_survives_deleting_the_actor(self):
        original = self._value()
        self._correct(original)

        self.admin.delete()

        correction = LabValueCorrection.objects.get()
        self.assertIsNone(correction.actor)
        self.assertIn('corr_admin', correction.actor_label)


class EffectiveValueTests(_Values):

    def test_an_uncorrected_value_is_its_own_effective_value(self):
        original = self._value()
        self.assertIs(original.effective(), original)
        self.assertFalse(original.is_corrected)

    def test_a_corrected_value_resolves_to_the_correction(self):
        original = self._value(value='5.2', canonical=5.2)
        self._correct(original, value='52', canonical=52.0)

        self.assertEqual(original.effective().canonical_value, 52.0)
        self.assertTrue(original.is_corrected)

    def test_the_newest_correction_wins(self):
        original = self._value()
        self._correct(original, value='52', canonical=52.0)
        self._correct(original, value='5.4', canonical=5.4)

        self.assertEqual(original.effective().canonical_value, 5.4)

    def test_correcting_a_correction_appends_against_the_original(self):
        """A flat chain: cycles are impossible by construction."""
        original = self._value()
        self._correct(original, value='52', canonical=52.0)
        self._correct(original, value='5.4', canonical=5.4)

        self.assertEqual(original.corrections.count(), 2)
        for correction in original.corrections.all():
            self.assertEqual(correction.original_id, original.pk)

    def test_resolution_is_deterministic_for_same_instant_corrections(self):
        original = self._value()
        first = self._correct(original, value='52', canonical=52.0)
        second = self._correct(original, value='5.4', canonical=5.4)
        LabValueCorrection.objects.filter(pk__in=[first.pk, second.pk]).update(
            created_at=first.created_at)

        resolved = {original.effective().pk for _ in range(5)}
        self.assertEqual(len(resolved), 1, 'the effective value must not vary between reads')


class TypedFactsUseTheCorrectionTests(_Values):
    """Every accessor goes through _to_observation, so all of them inherit it."""

    def test_latest_reports_the_corrected_value(self):
        original = self._value(value='5.2', canonical=5.2)
        self._correct(original, value='52', canonical=52.0)

        result = facts.latest(self.patient, 'Glucose')
        self.assertEqual(result.observation.value, 52.0)

    def test_latest_marks_the_reading_as_corrected(self):
        """
        A quoted value that differs from the source document must be able to
        say so — "5.4 (corrected)" is a different statement from "5.4".
        """
        original = self._value()
        self._correct(original, value='5.4', canonical=5.4)

        self.assertTrue(facts.latest(self.patient, 'Glucose').observation.corrected)

    def test_an_uncorrected_reading_is_not_marked(self):
        self._value()
        self.assertFalse(facts.latest(self.patient, 'Glucose').observation.corrected)

    def test_previous_reports_the_corrected_value(self):
        older = self._value(value='4.0', canonical=4.0, when=date(2026, 1, 1))
        self._value(value='5.0', canonical=5.0, when=date(2026, 2, 1))
        self._correct(older, value='9.9', canonical=9.9)

        self.assertEqual(facts.previous(self.patient, 'Glucose').observation.value, 9.9)

    def test_series_reports_corrected_values_in_order(self):
        first = self._value(value='4.0', canonical=4.0, when=date(2026, 1, 1))
        self._value(value='5.0', canonical=5.0, when=date(2026, 2, 1))
        self._correct(first, value='6.0', canonical=6.0)

        self.assertEqual([o.value for o in facts.series(self.patient, 'Glucose')],
                         [6.0, 5.0])

    def test_on_date_reports_the_corrected_value(self):
        original = self._value(when=date(2026, 3, 3), value='5.2', canonical=5.2)
        self._correct(original, value='7.7', canonical=7.7)

        result = facts.on_date(self.patient, 'Glucose', date(2026, 3, 3))
        self.assertEqual(result.observation.value, 7.7)

    def test_a_correction_can_resolve_a_conflict(self):
        """
        Two readings on one date used to be permanently Conflicted. Correcting
        the misread one is exactly how a human resolves it — and the resolution
        is recorded rather than assumed.
        """
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result',
            record_date=date(2026, 4, 4))
        keep = ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='5.2', unit='mmol/L',
            canonical_value=5.2, original_unit='mmol/L', unit_known=True)
        wrong = ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='52', unit='mmol/L',
            canonical_value=52.0, original_unit='mmol/L', unit_known=True)

        self.assertIsInstance(facts.latest(self.patient, 'Glucose', exact=True),
                              facts.Conflicted)

        self._correct(wrong, value='5.2', canonical=5.2)

        resolved = facts.latest(self.patient, 'Glucose', exact=True)
        self.assertIsInstance(resolved, facts.Confirmed)
        self.assertEqual(resolved.observation.value, 5.2)
        self.assertEqual(keep.canonical_value, 5.2)

    def test_a_correction_can_create_a_conflict_and_that_is_reported(self):
        """The layer must not hide a disagreement it just introduced."""
        record = MedicalRecord.objects.create(
            patient=self.patient, title='Panel', record_type='lab_result',
            record_date=date(2026, 5, 5))
        ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='5.2', unit='mmol/L',
            canonical_value=5.2, original_unit='mmol/L', unit_known=True)
        second = ParsedLabValue.objects.create(
            record=record, parameter_name='Glucose', value='5.2', unit='mmol/L',
            canonical_value=5.2, original_unit='mmol/L', unit_known=True)

        self.assertIsInstance(facts.latest(self.patient, 'Glucose', exact=True),
                              facts.Confirmed)

        self._correct(second, value='9.9', canonical=9.9)

        self.assertIsInstance(facts.latest(self.patient, 'Glucose', exact=True),
                              facts.Conflicted)

    def test_identity_stays_with_the_original(self):
        """
        A correction changes what was measured, not when or from which
        document. Letting it move those would make the timeline
        unreconstructable.
        """
        original = self._value(when=date(2026, 6, 6))
        self._correct(original, value='9.9', canonical=9.9)

        observation = facts.latest(self.patient, 'Glucose').observation
        self.assertEqual(observation.date, date(2026, 6, 6))
        self.assertEqual(observation.record_id, str(original.record_id))


class HistoricalReconstructionTests(_Values):

    def test_both_readings_remain_available(self):
        original = self._value(value='5.2', canonical=5.2)
        self._correct(original, value='52', canonical=52.0)

        self.assertEqual(ParsedLabValue.objects.get(pk=original.pk).value, '5.2')
        self.assertEqual(original.corrections.first().value, '52')

    def test_the_full_correction_history_is_ordered_newest_first(self):
        original = self._value()
        self._correct(original, value='52', canonical=52.0, reason='first')
        self._correct(original, value='5.4', canonical=5.4, reason='second')

        self.assertEqual([c.reason for c in original.corrections.all()],
                         ['second', 'first'])

    def test_why_an_alert_fired_is_still_answerable(self):
        """
        The point of immutability. An alert raised on the original value must
        remain explainable after the value is corrected.
        """
        original = self._value(value='6.8', canonical=6.8, critical=True)
        self._correct(original, value='4.8', canonical=4.8,
                      reason='Potassium column misaligned by one row')

        stored = ParsedLabValue.objects.get(pk=original.pk)
        self.assertTrue(stored.is_critical)
        self.assertEqual(stored.canonical_value, 6.8)
        self.assertEqual(original.effective().canonical_value, 4.8)


class RetentionTests(_Values):

    def test_deleting_the_record_removes_its_corrections(self):
        original = self._value()
        self._correct(original)

        original.record.delete()

        self.assertEqual(LabValueCorrection.objects.count(), 0)

    def test_erasing_the_patient_removes_corrections(self):
        """A correction is the patient's clinical data and must not outlive them."""
        original = self._value()
        self._correct(original)

        from apps.accounts.services import purge_user_data
        with self.captureOnCommitCallbacks(execute=True):
            purge_user_data(self.patient)

        self.assertEqual(LabValueCorrection.objects.count(), 0)


class AuthorizationTests(_Values):
    """Authority comes from the authz seam, not a local role check."""

    def test_platform_administration_may_correct(self):
        from apps.accounts.authz import can_correct_clinical_value
        self.assertTrue(can_correct_clinical_value(self.admin))

    def test_a_patient_may_not_correct_their_own_values(self):
        """
        The subject owns the data but not its evidential integrity. Rewriting
        one's own lab history destroys the record's value for everyone relying
        on it, including the patient.
        """
        from apps.accounts.authz import can_correct_clinical_value
        self.assertFalse(can_correct_clinical_value(self.patient))

    def test_a_linked_doctor_may_not_correct(self):
        """Consent to be read is not consent to be edited."""
        from apps.accounts.authz import can_correct_clinical_value

        doctor = User.objects.create_user(
            'corr_doc', email='corr_doc@test.invalid', password='pw', role='doctor')
        self.assertFalse(can_correct_clinical_value(doctor))

    def test_anonymous_may_not_correct(self):
        from django.contrib.auth.models import AnonymousUser

        from apps.accounts.authz import can_correct_clinical_value
        self.assertFalse(can_correct_clinical_value(AnonymousUser()))

    def test_the_admin_refuses_an_unauthorised_actor(self):
        from django.contrib import admin as dj
        from django.core.exceptions import PermissionDenied

        from apps.medical_records.admin import LabValueCorrectionAdmin

        original = self._value()
        correction = LabValueCorrection(
            original=original, value='9.9', canonical_value=9.9, reason='r')

        ma = LabValueCorrectionAdmin(LabValueCorrection, dj.site)
        request = type('R', (), {'user': self.patient})()

        with self.assertRaises(PermissionDenied):
            ma.save_model(request, correction, form=None, change=False)

        self.assertEqual(LabValueCorrection.objects.count(), 0)

    def test_the_original_row_is_not_editable_in_the_admin(self):
        from django.contrib import admin as dj

        from apps.medical_records.admin import ParsedLabValueAdmin

        ma = ParsedLabValueAdmin(ParsedLabValue, dj.site)
        request = type('R', (), {'user': self.admin})()

        self.assertFalse(ma.has_change_permission(request))
        self.assertFalse(ma.has_add_permission(request))
        self.assertFalse(ma.has_delete_permission(request))

    def test_a_correction_cannot_be_edited_or_deleted(self):
        """A correction is evidence too. Wrong correction? Append another."""
        from django.contrib import admin as dj

        from apps.medical_records.admin import LabValueCorrectionAdmin

        ma = LabValueCorrectionAdmin(LabValueCorrection, dj.site)
        request = type('R', (), {'user': self.admin})()

        self.assertFalse(ma.has_change_permission(request))
        self.assertFalse(ma.has_delete_permission(request))


class AuditTests(_Values):

    def test_a_correction_is_recorded_without_clinical_values(self):
        from django.contrib import admin as dj

        from apps.accounts.models import AdminAuditEvent
        from apps.medical_records.admin import LabValueCorrectionAdmin

        original = self._value(analyte='Potassium', value='6.8', canonical=6.8)
        correction = LabValueCorrection(
            original=original, value='4.8', canonical_value=4.8,
            reason='Column misaligned')

        ma = LabValueCorrectionAdmin(LabValueCorrection, dj.site)
        request = type('R', (), {'user': self.admin})()
        ma.save_model(request, correction, form=None, change=False)

        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.Action.VALUE_CORRECTED)
        self.assertTrue(event.success)
        self.assertEqual(event.actor, self.admin)

        blob = f'{event.metadata} {event.target_label}'
        self.assertNotIn('Potassium', blob)
        self.assertNotIn('6.8', blob)
        self.assertNotIn('4.8', blob)

    def test_a_refused_correction_is_recorded_as_a_failure(self):
        from django.contrib import admin as dj
        from django.core.exceptions import PermissionDenied

        from apps.accounts.models import AdminAuditEvent
        from apps.medical_records.admin import LabValueCorrectionAdmin

        original = self._value()
        correction = LabValueCorrection(
            original=original, value='9.9', canonical_value=9.9, reason='r')

        ma = LabValueCorrectionAdmin(LabValueCorrection, dj.site)
        request = type('R', (), {'user': self.patient})()
        with self.assertRaises(PermissionDenied):
            ma.save_model(request, correction, form=None, change=False)

        event = AdminAuditEvent.objects.get(action=AdminAuditEvent.Action.VALUE_CORRECTED)
        self.assertFalse(event.success)


class BackwardCompatibilityTests(_Values):

    def test_existing_values_behave_exactly_as_before(self):
        self._value(value='5.2', canonical=5.2, when=date(2026, 1, 1))
        self._value(value='5.6', canonical=5.6, when=date(2026, 2, 1))

        result = facts.latest(self.patient, 'Glucose')
        self.assertEqual(result.observation.value, 5.6)
        self.assertFalse(result.observation.corrected)

    def test_trend_still_reads_the_series(self):
        for i, value in enumerate((4.0, 5.0, 6.0), start=1):
            self._value(value=str(value), canonical=value,
                        when=date(2026, 1, 1) + timedelta(days=30 * i))

        self.assertEqual(len(facts.series(self.patient, 'Glucose')), 3)
