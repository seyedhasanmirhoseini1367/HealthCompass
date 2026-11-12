"""Clinician edits and the print page."""
import copy

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.scribe.models import Encounter, Note, Transcript
from apps.scribe.tests.test_core import GOOD_NOTE, SEGMENTS, _wav

User = get_user_model()


class EditingTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('se_doc', email='se@test.invalid', password='pw')
        self.other = User.objects.create_user('se_other', email='seo@test.invalid', password='pw')
        self.enc = Encounter.objects.create(owner=self.user, audio=_wav(),
                                            status=Encounter.Status.DONE)
        Transcript.objects.create(encounter=self.enc, text='x', segments=SEGMENTS)
        self.note = Note.objects.create(
            encounter=self.enc, soap=copy.deepcopy(GOOD_NOTE), raw_output='{"original": true}',
            evidence={'assessment': [[{'title': 'Guideline A'}]], 'plan': [[{'title': 'Guideline B'}]]})
        self.client.force_login(self.user)
        self.url = reverse('scribe:edit', args=[self.enc.pk])

    def _post(self, **extra):
        data = {f'{s}-0-text': GOOD_NOTE[s][0]['text'] for s in GOOD_NOTE}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_unchanged_submit_changes_nothing(self):
        self._post()
        self.note.refresh_from_db()
        self.assertEqual(self.note.soap, GOOD_NOTE)
        self.assertIsNone(self.note.edited_at)

    def test_changed_item_keeps_sources_and_is_flagged(self):
        self._post(**{'plan-0-text': 'Rest, fluids, paracetamol.'})
        self.note.refresh_from_db()
        item = self.note.soap['plan'][0]
        self.assertEqual(item['text'], 'Rest, fluids, paracetamol.')
        self.assertEqual(item['source_segments'], [4])
        self.assertTrue(item['edited'])
        self.assertIsNotNone(self.note.edited_at)
        self.assertEqual(self.note.raw_output, '{"original": true}')

    def test_added_item_has_no_sources_and_aligned_evidence(self):
        self._post(**{'assessment-new': 'Consider strep test\n\n'})
        self.note.refresh_from_db()
        items = self.note.soap['assessment']
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1], {'text': 'Consider strep test', 'source_segments': [], 'added': True})
        self.assertEqual(self.note.evidence['assessment'], [[{'title': 'Guideline A'}], []])

    def test_removed_item_drops_its_evidence(self):
        self._post(**{'assessment-0-delete': '1'})
        self.note.refresh_from_db()
        self.assertEqual(self.note.soap['assessment'], [])
        self.assertEqual(self.note.evidence['assessment'], [])
        self.assertEqual(self.note.evidence['plan'], [[{'title': 'Guideline B'}]])

    def test_detail_shows_flags(self):
        self._post(**{'plan-0-text': 'Changed.', 'subjective-new': 'Added line'})
        body = self.client.get(reverse('scribe:detail', args=[self.enc.pk])).content.decode()
        self.assertIn('Edited — check it still matches', body)
        self.assertIn('Added by clinician', body)

    def test_someone_elses_note_cannot_be_edited_or_printed(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, {'plan-0-delete': '1'}).status_code, 404)
        self.assertEqual(self.client.get(reverse('scribe:print', args=[self.enc.pk])).status_code, 404)
        self.note.refresh_from_db()
        self.assertEqual(len(self.note.soap['plan']), 1)

    def test_print_page(self):
        r = self.client.get(reverse('scribe:print', args=[self.enc.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Consultation note')
        self.assertContains(r, 'Viral pharyngitis.')
        self.assertContains(r, 'window.print()')
        self.assertContains(r, 'Not a final record')
