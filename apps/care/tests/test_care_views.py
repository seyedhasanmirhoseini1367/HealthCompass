"""
The two surfaces, at the HTTP boundary.

The caregiver dashboard is a new way to see something about another person, so
it gets the same treatment as every other such path in this codebase: gated on
the patient's own grant, logged in the patient's own access trail, and holding
only what it needs. "Family sharing" is not a synonym for medical-record access,
and this page is where that distinction is either kept or quietly lost.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import DoctorAccessLog, SharingGrant
from apps.care.models import (CareTask, MonitoringSignal, PatientReport,
                              TaskOccurrence)

User = get_user_model()

DRUG = 'Zidovudine'


class _Views(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'cv_patient', email='cv_patient@test.invalid', password='pw',
            role='patient', first_name='Aino')
        self.caregiver = User.objects.create_user(
            'cv_care', email='cv_care@test.invalid', password='pw',
            role='patient', first_name='Mikko')
        self.stranger = User.objects.create_user(
            'cv_stranger', email='cv_stranger@test.invalid', password='pw',
            role='patient')
        self.task = CareTask.objects.create(
            patient=self.patient, label=DRUG, times_of_day=['08:00'])

    def _grant(self, **kwargs):
        options = dict(can_view_alerts=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        return SharingGrant.objects.create(
            patient=self.patient, recipient=self.caregiver, **options)

    def _occurrence(self, state=None, hours_ago=5):
        occurrence = TaskOccurrence.objects.create(
            task=self.task, patient=self.patient,
            due_at=timezone.now() - timedelta(hours=hours_ago))
        if state:
            occurrence.state = state
            occurrence.save(update_fields=['state'])
        return occurrence


class PatientSurfaceTests(_Views):

    def test_the_page_requires_a_login(self):
        response = self.client.get('/care/')
        self.assertEqual(response.status_code, 302)

    def test_a_patient_sees_their_own_due_tasks(self):
        self._occurrence()
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/health/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, DRUG)

    def test_the_page_offers_three_answers_not_one(self):
        """
        ACCEPTANCE — a lone "Done" button learns nothing from the times it was
        not done, leaving only silence to reason about.
        """
        self._occurrence()
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/health/')

        for value in ('confirmed', 'skipped', 'missed'):
            self.assertContains(response, f'value="{value}"')

    def test_the_page_never_offers_unconfirmed_as_an_answer(self):
        """That state means the system heard nothing; a click IS something."""
        self._occurrence()
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/health/')

        self.assertNotContains(response, 'value="unconfirmed"')

    def test_confirming_records_the_answer(self):
        occurrence = self._occurrence()
        self.client.force_login(self.patient)
        self.client.post(f'/care/respond/{occurrence.pk}/', {'state': 'confirmed'})

        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.CONFIRMED)
        self.assertEqual(occurrence.responded_by, self.patient)

    def test_a_crafted_post_cannot_record_unconfirmed(self):
        """ACCEPTANCE — the system's ignorance is not a thing a request creates."""
        occurrence = self._occurrence()
        self.client.force_login(self.patient)
        self.client.post(f'/care/respond/{occurrence.pk}/', {'state': 'unconfirmed'})

        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.PENDING)

    def test_a_stranger_cannot_answer_someone_elses_occurrence(self):
        occurrence = self._occurrence()
        self.client.force_login(self.stranger)
        response = self.client.post(
            f'/care/respond/{occurrence.pk}/', {'state': 'confirmed'})

        self.assertEqual(response.status_code, 404)
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.PENDING)

    def test_a_report_is_stored_in_the_patients_own_words(self):
        self.client.force_login(self.patient)
        self.client.post('/care/report/', {'text': 'I felt dizzy this morning'})

        self.assertEqual(PatientReport.objects.get().text,
                         'I felt dizzy this morning')

    def test_an_empty_report_is_rejected(self):
        self.client.force_login(self.patient)
        self.client.post('/care/report/', {'text': '   '})

        self.assertEqual(PatientReport.objects.count(), 0)

    def test_a_report_is_attributed_to_the_patient_who_made_it(self):
        self.client.force_login(self.patient)
        self.client.post('/care/report/', {'text': 'I felt dizzy'})

        entry = PatientReport.objects.get()
        self.assertEqual(entry.reported_by, self.patient)
        self.assertEqual(entry.reported_by_role, PatientReport.Reporter.PATIENT)

    def test_the_page_explains_what_not_answering_means(self):
        """Said to the person whose behaviour is being interpreted."""
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/health/')

        # Matched on phrases that sit on one line: the sentence wraps in the
        # template, so the rendered HTML has a newline mid-phrase.
        self.assertContains(response, 'hear back')
        self.assertContains(response, 'Only you can say what actually happened')


class CaregiverSurfaceTests(_Views):
    """
    The caregiver's list, and the one page about a person.

    There used to be two detail pages for the same human being — this app's
    /care/watching/<pk>/ and accounts' /accounts/shared/<pk>/ — each asking "may
    I see this person" with its own copy of the scope rule. They were
    consolidated into the accounts one, which already handles every scope and
    the frozen-share cutoff. These assertions moved with the page rather than
    being dropped: what they protect did not change.
    """

    def _person_url(self):
        return f'/care/person/{self.patient.pk}/'

    # -- The list ------------------------------------------------------------

    def test_a_caregiver_with_the_scope_sees_the_person(self):
        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get('/care/')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Aino')

    def test_someone_with_no_grant_sees_nobody(self):
        self.client.force_login(self.stranger)
        response = self.client.get('/care/')

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Aino')

    # -- Consolidation -------------------------------------------------------

    def test_the_old_detail_url_redirects_to_the_one_person_page(self):
        """
        ACCEPTANCE - links already sent in notifications must keep working.

        Permanent, because the duplicate is not coming back.
        """
        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get(f'/care/watching/{self.patient.pk}/')

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response['Location'], f'/care/person/{self.patient.pk}/')

    def test_the_redirect_does_not_leak_whether_a_grant_exists(self):
        """
        The redirect is unconditional; the destination decides.

        Refusing here for a stranger and redirecting for a caregiver would make
        the status code an oracle for "does this person share with you".
        """
        self.client.force_login(self.stranger)
        response = self.client.get(f'/care/watching/{self.patient.pk}/')

        self.assertEqual(response.status_code, 301)
        self.assertEqual(self.client.get(response['Location']).status_code, 404)

    # -- The person page -----------------------------------------------------

    def test_the_detail_page_needs_a_grant(self):
        self.client.force_login(self.stranger)
        response = self.client.get(self._person_url())

        self.assertEqual(response.status_code, 404)

    def test_a_revoked_grant_closes_the_detail_page(self):
        grant = self._grant()
        grant.revoke(by=self.patient, reason='no longer needed')

        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        self.assertEqual(response.status_code, 404)

    def test_a_records_only_grant_does_not_show_care_status(self):
        """
        Separate promises. Documents shared is not "watch over me", so the care
        section is absent even though the page itself opens.
        """
        self._grant(can_view_alerts=False, can_view_records=True)
        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['has_care_scope'])
        self.assertNotContains(response, 'Does anything need attention?')

    def test_reading_someones_care_status_is_recorded_in_their_trail(self):
        """"Who has been checking on me" deserves a complete answer."""
        self._grant()
        self.client.force_login(self.caregiver)
        self.client.get(self._person_url())

        self.assertTrue(DoctorAccessLog.objects.filter(
            actor=self.caregiver, patient=self.patient).exists())

    def test_the_caregiver_page_is_not_a_record_browser(self):
        """
        ACCEPTANCE - one grant of "tell me if something is wrong" must not
        quietly deliver the file as well.
        """
        from datetime import date

        from apps.medical_records.models import (MedicalRecord,
                                                 ParsedLabValue)

        record = MedicalRecord.objects.create(
            patient=self.patient, title='Cardiology discharge',
            record_type='discharge', record_date=date(2026, 1, 1))
        ParsedLabValue.objects.create(
            record=record, patient=self.patient,
            parameter_name='Troponin T', value='250', unit='ng/L')

        self._grant()          # alerts only
        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        self.assertNotContains(response, 'Troponin')
        self.assertNotContains(response, 'Cardiology discharge')

    def test_an_unconfirmed_streak_is_described_as_silence_not_as_a_miss(self):
        """ACCEPTANCE - the caregiver is told what the app knows, not more."""
        occurrences = [self._occurrence(TaskOccurrence.State.UNCONFIRMED, hours_ago=h)
                       for h in (24, 48, 72)]
        signal = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=occurrences[-1].due_at, window_end=occurrences[0].due_at,
            subject_key=f'task:{self.task.pk}')
        signal.occurrences.set(occurrences)

        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        self.assertContains(response, 'not that it was missed')

    def test_the_patients_own_words_are_shown_as_a_quotation(self):
        """
        Inside the app, behind the login, the caregiver DOES see what was said -
        marked as the patient's words rather than the system's characterisation.
        """
        report = PatientReport.objects.create(
            patient=self.patient, reported_by=self.patient, text='I felt dizzy')
        signal = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPORTED_SYMPTOM,
            window_start=report.effective_at, window_end=report.effective_at,
            subject_key=f'report:{report.pk}')
        signal.reports.set([report])

        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        self.assertContains(response, 'I felt dizzy')
        self.assertContains(response, 'Their own words')

    def test_a_resolved_signal_leaves_the_attention_list(self):
        signal = MonitoringSignal.objects.create(
            patient=self.patient,
            kind=MonitoringSignal.Kind.REPEATED_UNCONFIRMED,
            window_start=timezone.now(), window_end=timezone.now(),
            subject_key='task:x', resolved_at=timezone.now())
        signal.occurrences.set([self._occurrence(TaskOccurrence.State.UNCONFIRMED)])

        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        self.assertContains(response, 'Nothing needs your attention')

    def test_the_seven_day_activity_survived_the_consolidation(self):
        """
        Functionality moved, not deleted. The four states stay four counts -
        never one adherence figure, which would average our ignorance into
        their behaviour.
        """
        self._occurrence(TaskOccurrence.State.CONFIRMED, hours_ago=10)
        self._occurrence(TaskOccurrence.State.UNCONFIRMED, hours_ago=20)

        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.get(self._person_url())

        activity = response.context['care_activity']
        self.assertEqual(activity['confirmed'], 1)
        self.assertEqual(activity['unconfirmed'], 1)
        self.assertContains(response, 'No answer')

    def test_a_caregiver_cannot_answer_on_the_patients_behalf(self):
        """
        Not forbidden in the model - a caregiver confirming is a real case, and
        `responded_by` keeps it distinguishable. It is simply not offered,
        because a one-tap "mark it done" for someone else's medication is a
        claim about them they did not make.
        """
        occurrence = self._occurrence()
        self._grant()
        self.client.force_login(self.caregiver)
        response = self.client.post(
            f'/care/respond/{occurrence.pk}/', {'state': 'confirmed'})

        self.assertEqual(response.status_code, 404)
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.state, TaskOccurrence.State.PENDING)


class TaskManagementTests(_Views):
    """
    Creating a reminder — the step that was missing entirely.

    The care feature shipped with no way to make a CareTask, so /care/ listed
    occurrences of schedules nothing could produce. Every test passed, because
    the fixtures created tasks directly; a real person could not.
    """

    def test_a_patient_can_add_a_reminder(self):
        self.client.force_login(self.patient)
        self.client.post('/care/tasks/add/',
                         {'label': 'Morning tablet', 'times': '08:00'})

        task = CareTask.objects.filter(patient=self.patient,
                                       label='Morning tablet').first()
        self.assertIsNotNone(task)
        self.assertEqual(task.times_of_day, ['08:00'])

    def test_adding_a_reminder_produces_occurrences_immediately(self):
        """ACCEPTANCE — an empty page after adding one gives no sign it worked."""
        self.client.force_login(self.patient)
        self.client.post('/care/tasks/add/',
                         {'label': 'Evening tablet', 'times': '20:00'})

        task = CareTask.objects.get(label='Evening tablet')
        self.assertTrue(TaskOccurrence.objects.filter(task=task).exists())

    def test_several_times_can_be_given(self):
        self.client.force_login(self.patient)
        self.client.post('/care/tasks/add/',
                         {'label': 'Twice daily', 'times': '08:00, 20:00'})

        self.assertEqual(CareTask.objects.get(label='Twice daily').times_of_day,
                         ['08:00', '20:00'])

    def test_an_impossible_time_is_rejected_not_coerced(self):
        """25:00 is a typo. Silently reading it as 01:00 invents a dose time."""
        self.client.force_login(self.patient)
        self.client.post('/care/tasks/add/',
                         {'label': 'Bad time', 'times': '25:00'})

        self.assertFalse(CareTask.objects.filter(label='Bad time').exists())

    def test_a_reminder_with_no_name_is_rejected(self):
        self.client.force_login(self.patient)
        self.client.post('/care/tasks/add/', {'label': '  ', 'times': '08:00'})

        self.assertEqual(CareTask.objects.filter(patient=self.patient).count(), 1)

    def test_a_new_reminder_belongs_to_the_caller(self):
        """No patient field is accepted from the request."""
        self.client.force_login(self.stranger)
        self.client.post('/care/tasks/add/',
                         {'label': 'Theirs', 'times': '08:00',
                          'patient': self.patient.pk})

        self.assertEqual(CareTask.objects.get(label='Theirs').patient,
                         self.stranger)

    def test_stopping_a_reminder_keeps_what_it_already_recorded(self):
        """
        ACCEPTANCE — history is evidence of what was asked and answered. A
        caregiver looking at last week must not find it rewritten because the
        schedule changed today.
        """
        answered = self._occurrence(TaskOccurrence.State.CONFIRMED, hours_ago=48)

        self.client.force_login(self.patient)
        self.client.post(f'/care/tasks/{self.task.pk}/stop/')

        self.task.refresh_from_db()
        self.assertFalse(self.task.is_active)
        self.assertTrue(TaskOccurrence.objects.filter(pk=answered.pk).exists())

    def test_stopping_a_reminder_removes_only_future_unanswered_ones(self):
        from datetime import timedelta

        future = TaskOccurrence.objects.create(
            task=self.task, patient=self.patient,
            due_at=timezone.now() + timedelta(hours=6))

        self.client.force_login(self.patient)
        self.client.post(f'/care/tasks/{self.task.pk}/stop/')

        self.assertFalse(TaskOccurrence.objects.filter(pk=future.pk).exists())

    def test_a_stranger_cannot_stop_someone_elses_reminder(self):
        self.client.force_login(self.stranger)
        response = self.client.post(f'/care/tasks/{self.task.pk}/stop/')

        self.assertEqual(response.status_code, 404)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_active)

    def test_the_page_offers_a_way_to_add_one(self):
        """The gap was invisible because nothing pointed at it."""
        self.client.force_login(self.patient)
        response = self.client.get('/dashboard/health/')

        self.assertContains(response, '/care/tasks/add/')
