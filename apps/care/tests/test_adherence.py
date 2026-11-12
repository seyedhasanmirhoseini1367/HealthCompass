"""The 14-day reminder strip on My Health."""
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.care.adherence import adherence
from apps.care.models import CareTask, TaskOccurrence

User = get_user_model()
S = TaskOccurrence.State


class AdherenceTests(TestCase):

    def setUp(self):
        self.patient = User.objects.create_user(
            'ad_patient', email='ad@test.invalid', password='pw', role='patient')
        self.task = CareTask.objects.create(
            patient=self.patient, label='Morning tablet', times_of_day=['08:00'])

    def _occ(self, days_ago, state):
        day = timezone.localdate() - timedelta(days=days_ago)
        due = timezone.make_aware(datetime.combine(day, time(8, 0)))
        o = TaskOccurrence.objects.create(task=self.task, patient=self.patient, due_at=due)
        o.state = state
        o.save(update_fields=['state'])
        return o

    def test_nothing_scheduled_means_no_strip(self):
        self.assertIsNone(adherence(self.patient))

    def test_counts_and_no_answer_is_not_missed(self):
        self._occ(1, S.CONFIRMED)
        self._occ(2, S.CONFIRMED)
        self._occ(3, S.UNCONFIRMED)
        self._occ(4, S.MISSED)
        data = adherence(self.patient)
        self.assertEqual(data['confirmed'], 2)
        self.assertEqual(data['answered_due'], 4)
        self.assertEqual(data['no_answer'], 1)
        self.assertEqual(data['percent'], 50)
        self.assertEqual(len(data['days']), 14)
        day3 = data['days'][-4]
        self.assertEqual([k for k, *_ in day3['parts']], ['no_answer'])
        self.assertIn('no answer', day3['label'])
        self.assertNotIn('missed', day3['label'])

    def test_older_than_two_weeks_is_ignored(self):
        self._occ(20, S.CONFIRMED)
        self.assertIsNone(adherence(self.patient))

    def test_my_health_shows_the_strip(self):
        self._occ(1, S.CONFIRMED)
        self.client.force_login(self.patient)
        r = self.client.get('/dashboard/health/')
        self.assertContains(r, 'Last 14 days')
        self.assertContains(r, '<b>1 of 1</b> confirmed', html=False)
        self.assertContains(r, 'ad-seg confirmed')

    def test_other_patients_are_not_counted(self):
        other = User.objects.create_user(
            'ad_other', email='ad_o@test.invalid', password='pw', role='patient')
        task = CareTask.objects.create(patient=other, label='X', times_of_day=['08:00'])
        TaskOccurrence.objects.create(task=task, patient=other, due_at=timezone.now())
        self.assertIsNone(adherence(self.patient))
