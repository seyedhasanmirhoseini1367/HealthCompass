"""
Scribe MVP tests. Whisper, the LLM chain and guideline retrieval are mocked —
nothing here loads a model or makes a network call.
"""
import datetime
import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.consent import grant_consent
from apps.accounts.models import ConsentPurpose
from apps.scribe.models import Encounter, Note, Transcript
from apps.scribe.services import note_generation, pipeline
from apps.scribe.services.errors import ScribeError
from apps.scribe.services.note_generation import (NoteValidationError, generate_note_from_segments,
                                                  parse_note_json)

User = get_user_model()

WAV_BYTES = b'RIFF\x24\x00\x00\x00WAVEfmt ' + b'\x00' * 32
SEGMENTS = [
    {'start': 0.0, 'end': 3.0, 'text': 'What brings you in today?'},
    {'start': 3.0, 'end': 7.5, 'text': "I've had a sore throat for three days."},
    {'start': 7.5, 'end': 11.0, 'text': 'Your temperature is 38.2.'},
    {'start': 11.0, 'end': 15.0, 'text': 'This looks like viral pharyngitis.'},
    {'start': 15.0, 'end': 19.0, 'text': 'Rest, fluids, and come back if it lasts a week.'},
]
GOOD_NOTE = {
    'subjective': [{'text': 'Sore throat for 3 days.', 'source_segments': [1]}],
    'objective':  [{'text': 'Temperature 38.2 °C.', 'source_segments': [2]}],
    'assessment': [{'text': 'Viral pharyngitis.', 'source_segments': [3]}],
    'plan':       [{'text': 'Rest and fluids; return if >1 week.', 'source_segments': [4]}],
}


def _wav(name='visit.wav'):
    return SimpleUploadedFile(name, WAV_BYTES, content_type='audio/wav')


# ── JSON validation ──────────────────────────────────────────────────────────

class ParseNoteJsonTests(TestCase):

    def test_valid_note_round_trips(self):
        self.assertEqual(parse_note_json(json.dumps(GOOD_NOTE), 5), GOOD_NOTE)

    def test_code_fences_and_surrounding_prose_are_tolerated(self):
        raw = 'Here is the note:\n```json\n' + json.dumps(GOOD_NOTE) + '\n```'
        self.assertEqual(parse_note_json(raw, 5), GOOD_NOTE)

    def test_empty_sections_are_allowed(self):
        empty = {s: [] for s in note_generation.SECTIONS}
        self.assertEqual(parse_note_json(json.dumps(empty), 5), empty)

    def test_out_of_range_bool_and_duplicate_indices_are_dropped(self):
        note = dict(GOOD_NOTE, plan=[{'text': 'x', 'source_segments': [4, 4, 99, -1, True, '2', 0]}])
        self.assertEqual(parse_note_json(json.dumps(note), 5)['plan'][0]['source_segments'], [0, 4])

    def test_structural_errors_are_rejected(self):
        bad_cases = {
            'not json':        'I cannot help with that.',
            'broken json':     '{"subjective": [',
            'array top level': json.dumps([GOOD_NOTE]),
            'missing section': json.dumps({k: v for k, v in GOOD_NOTE.items() if k != 'plan'}),
            'section not list': json.dumps(dict(GOOD_NOTE, plan='rest')),
            'item is string':  json.dumps(dict(GOOD_NOTE, plan=['rest'])),
            'empty text':      json.dumps(dict(GOOD_NOTE, plan=[{'text': '  ', 'source_segments': []}])),
            'sources not list': json.dumps(dict(GOOD_NOTE, plan=[{'text': 'x', 'source_segments': 3}])),
        }
        for label, raw in bad_cases.items():
            with self.subTest(label), self.assertRaises(NoteValidationError):
                parse_note_json(raw, 5)


class GenerateWithRetryTests(TestCase):

    @mock.patch.object(note_generation, 'call_llm')
    def test_retries_once_then_succeeds(self, call_llm):
        call_llm.side_effect = [('not json', 'groq'), (json.dumps(GOOD_NOTE), 'groq')]
        soap, raw, provider = generate_note_from_segments(SEGMENTS)
        self.assertEqual(soap, GOOD_NOTE)
        self.assertEqual(call_llm.call_count, 2)
        self.assertIn('rejected', call_llm.call_args_list[1].args[1])

    @mock.patch.object(note_generation, 'call_llm', return_value=('nope', 'groq'))
    def test_two_invalid_replies_raise(self, call_llm):
        with self.assertRaises(ScribeError):
            generate_note_from_segments(SEGMENTS)
        self.assertEqual(call_llm.call_count, 2)

    def test_transcript_is_sent_as_numbered_segments(self):
        with mock.patch.object(note_generation, 'call_llm',
                               return_value=(json.dumps(GOOD_NOTE), 'groq')) as call_llm:
            generate_note_from_segments(SEGMENTS)
        context = call_llm.call_args.args[0]
        self.assertIn('[1] [00:03-00:07] I\'ve had a sore throat', context)

    def test_providers_off_the_phi_allowlist_are_never_called(self):
        groq = mock.Mock(return_value='should not be used')
        gemini = mock.Mock(return_value=json.dumps(GOOD_NOTE))
        with self.settings(PHI_LLM_PROVIDERS=['gemini']), \
             mock.patch.object(note_generation, '_PROVIDERS', (('groq', groq), ('gemini', gemini))):
            _, _, provider = generate_note_from_segments(SEGMENTS)
        groq.assert_not_called()
        self.assertEqual(provider, 'gemini')

    def test_no_provider_available_raises(self):
        with mock.patch.object(note_generation, '_PROVIDERS', (('groq', mock.Mock(return_value=None)),)):
            with self.assertRaises(ScribeError):
                generate_note_from_segments(SEGMENTS)


# ── Pipeline status transitions ──────────────────────────────────────────────

class PipelineTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('sc_doc', email='sc_doc@test.invalid',
                                             password='pw', role='doctor')
        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        self.encounter = Encounter.objects.create(owner=self.user, audio=_wav())
        self.seen = []

    def _status(self):
        return Encounter.objects.get(pk=self.encounter.pk).status

    def _fake_transcribe(self, path, on_progress=None):
        self.seen.append(('transcribe', self._status()))
        return {'text': ' '.join(s['text'] for s in SEGMENTS), 'segments': SEGMENTS,
                'language': 'en', 'duration': 19.0}

    def _fake_llm(self, context, instruction):
        self.seen.append(('llm', self._status()))
        return json.dumps(GOOD_NOTE), 'groq'

    def _run(self, transcribe=None, llm=None, retrieve=None):
        retriever = mock.Mock()
        retriever.retrieve.side_effect = retrieve or (lambda q, top_k: [
            {'text': 'Guideline text', 'score': 0.5,
             'metadata': {'title': 'Sore throat', 'source_name': 'Käypä hoito', 'source_url': 'https://x'}}])
        with mock.patch('apps.scribe.services.transcription.transcribe_audio',
                        side_effect=transcribe or self._fake_transcribe), \
             mock.patch.object(note_generation, 'call_llm', side_effect=llm or self._fake_llm), \
             mock.patch('apps.scribe.services.evidence._retriever', return_value=retriever):
            pipeline.run_pipeline(self.encounter.pk)
        self.encounter.refresh_from_db()
        return retriever

    def test_happy_path_transitions_and_outputs(self):
        self.assertEqual(self.encounter.status, Encounter.Status.UPLOADED)
        retriever = self._run()
        self.assertEqual(self.seen, [('transcribe', 'transcribing'), ('llm', 'generating')])
        self.assertEqual(self.encounter.status, Encounter.Status.DONE)
        self.assertEqual(self.encounter.transcript.segments, SEGMENTS)
        note = self.encounter.note
        self.assertEqual(note.soap, GOOD_NOTE)
        self.assertEqual(note.provider, 'groq')
        # Evidence only for assessment + plan, one list per item.
        self.assertEqual(retriever.retrieve.call_count, 2)
        self.assertEqual(set(note.evidence), {'assessment', 'plan'})
        self.assertEqual(note.evidence['plan'][0][0]['source_name'], 'Käypä hoito')

    def test_invalid_llm_output_twice_marks_failed(self):
        self._run(llm=lambda c, i: ('garbage', 'groq'))
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertIn('valid note', self.encounter.error_message)
        self.assertFalse(Note.objects.filter(encounter=self.encounter).exists())

    def test_transcription_crash_marks_failed_with_safe_message(self):
        def boom(path, **kw):
            raise RuntimeError('decoder exploded at /secret/path')
        self._run(transcribe=boom)
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertNotIn('/secret/path', self.encounter.error_message)

    def test_no_speech_marks_failed(self):
        self._run(transcribe=lambda p, **kw: {'text': '', 'segments': [], 'language': '', 'duration': 0})
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertIn('No speech', self.encounter.error_message)

    def test_without_consent_the_transcript_is_not_sent(self):
        from apps.accounts.consent import revoke_consent
        revoke_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        self._run()
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertNotIn('llm', [step for step, _ in self.seen])

    def test_evidence_failure_does_not_fail_the_encounter(self):
        def boom(q, top_k):
            raise RuntimeError('embedding API down')
        self._run(retrieve=boom)
        self.assertEqual(self.encounter.status, Encounter.Status.DONE)
        self.assertEqual(self.encounter.note.evidence['assessment'], [[]])

    def test_stale_in_progress_encounter_is_marked_failed(self):
        Encounter.objects.filter(pk=self.encounter.pk).update(
            status=Encounter.Status.TRANSCRIBING,
            updated_at=timezone.now() - datetime.timedelta(hours=5))
        self.encounter.refresh_from_db()
        pipeline.mark_if_stale(self.encounter)
        self.assertEqual(self._status(), Encounter.Status.FAILED)


# ── Views and access control ─────────────────────────────────────────────────

class AccessControlTests(TestCase):

    def setUp(self):
        self.alice = User.objects.create_user('sc_alice', email='a@test.invalid', password='pw')
        self.bob = User.objects.create_user('sc_bob', email='b@test.invalid', password='pw')
        grant_consent(self.alice, ConsentPurpose.EXTERNAL_LLM)
        self.alice_enc = Encounter.objects.create(owner=self.alice, audio=_wav('alice.wav'),
                                                  original_filename='alice-visit.wav')
        self.bob_enc = Encounter.objects.create(owner=self.bob, audio=_wav('bob.wav'),
                                                original_filename='bob-visit.wav')

    def test_anonymous_users_are_redirected_to_login(self):
        for url in (reverse('scribe:list'), reverse('scribe:upload'),
                    reverse('scribe:detail', args=[self.alice_enc.pk]),
                    reverse('scribe:status', args=[self.alice_enc.pk])):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 302)

    def test_list_shows_only_own_encounters(self):
        self.client.force_login(self.alice)
        body = self.client.get(reverse('scribe:list')).content.decode()
        self.assertIn('alice-visit.wav', body)
        self.assertNotIn('bob-visit.wav', body)
        self.assertIn('not for clinical use', body)

    def test_other_users_encounter_is_404_everywhere(self):
        self.client.force_login(self.alice)
        pk = self.bob_enc.pk
        self.assertEqual(self.client.get(reverse('scribe:detail', args=[pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('scribe:status', args=[pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('scribe:delete', args=[pk])).status_code, 404)
        self.assertTrue(Encounter.objects.filter(pk=pk).exists())

    def test_own_detail_renders_transcript_and_note(self):
        Transcript.objects.create(encounter=self.alice_enc, text='x', segments=SEGMENTS)
        Note.objects.create(encounter=self.alice_enc, soap=GOOD_NOTE,
                            evidence={'assessment': [[]], 'plan': [[]]})
        self.alice_enc.status = Encounter.Status.DONE
        self.alice_enc.save()
        self.client.force_login(self.alice)
        body = self.client.get(reverse('scribe:detail', args=[self.alice_enc.pk])).content.decode()
        self.assertIn('Viral pharyngitis.', body)
        self.assertIn('data-segs="3"', body)
        self.assertIn('id="seg-3"', body)

    def test_status_endpoint(self):
        self.client.force_login(self.alice)
        data = self.client.get(reverse('scribe:status', args=[self.alice_enc.pk])).json()
        self.assertEqual(data['status'], 'uploaded')
        self.assertTrue(data['in_progress'])

    def test_delete_removes_row_and_audio_file(self):
        path = self.alice_enc.audio.path
        self.assertTrue(os.path.exists(path))
        self.client.force_login(self.alice)
        resp = self.client.post(reverse('scribe:delete', args=[self.alice_enc.pk]))
        self.assertRedirects(resp, reverse('scribe:list'))
        self.assertFalse(Encounter.objects.filter(pk=self.alice_enc.pk).exists())
        self.assertFalse(os.path.exists(path))

    def test_delete_requires_post(self):
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(reverse('scribe:delete', args=[self.alice_enc.pk])).status_code, 405)


class UploadTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('sc_up', email='up@test.invalid', password='pw')
        self.client.force_login(self.user)

    @mock.patch('apps.scribe.views.start_pipeline')
    def test_valid_upload_creates_encounter_and_starts_pipeline(self, start):
        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        resp = self.client.post(reverse('scribe:upload'), {'audio': _wav()})
        enc = Encounter.objects.get(owner=self.user)
        self.assertRedirects(resp, reverse('scribe:detail', args=[enc.pk]), fetch_redirect_response=False)
        self.assertEqual(enc.status, Encounter.Status.UPLOADED)
        self.assertNotIn('visit', os.path.basename(enc.audio.name))  # stored under a random name
        start.assert_called_once()

    @mock.patch('apps.scribe.views.start_pipeline')
    def test_upload_without_consent_is_refused(self, start):
        self.client.post(reverse('scribe:upload'), {'audio': _wav()})
        self.assertFalse(Encounter.objects.exists())
        start.assert_not_called()

    @mock.patch('apps.scribe.views.start_pipeline')
    def test_wrong_extension_or_content_is_refused(self, start):
        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        for f in (SimpleUploadedFile('x.ogg', WAV_BYTES),
                  SimpleUploadedFile('x.wav', b'<html>not audio</html>')):
            with self.subTest(f.name):
                self.client.post(reverse('scribe:upload'), {'audio': f})
        self.assertFalse(Encounter.objects.exists())
        start.assert_not_called()


# ── Recovery: heartbeat, stale detection, retry, model config ────────────────

class RecoveryTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user('sc_rec', email='rec@test.invalid', password='pw')
        grant_consent(self.user, ConsentPurpose.EXTERNAL_LLM)
        self.encounter = Encounter.objects.create(owner=self.user, audio=_wav())

    def _age(self, minutes, status=Encounter.Status.TRANSCRIBING):
        Encounter.objects.filter(pk=self.encounter.pk).update(
            status=status, updated_at=timezone.now() - datetime.timedelta(minutes=minutes))
        self.encounter.refresh_from_db()

    def test_heartbeat_keeps_a_running_job_from_looking_stale(self):
        self._age(60)
        pipeline._heartbeat(self.encounter.pk)()
        self.encounter.refresh_from_db()
        pipeline.mark_if_stale(self.encounter)
        self.assertEqual(self.encounter.status, Encounter.Status.TRANSCRIBING)

    def test_heartbeat_is_throttled(self):
        beat = pipeline._heartbeat(self.encounter.pk)
        with mock.patch.object(Encounter.objects, 'filter', wraps=Encounter.objects.filter) as f:
            for _ in range(50):
                beat()
        self.assertEqual(f.call_count, 1)

    def test_a_job_killed_mid_way_fails_within_minutes_not_hours(self):
        self._age(20)
        pipeline.mark_if_stale(self.encounter)
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertIn('out of memory', self.encounter.error_message)

    def test_a_recent_job_is_left_alone(self):
        self._age(5)
        pipeline.mark_if_stale(self.encounter)
        self.assertEqual(self.encounter.status, Encounter.Status.TRANSCRIBING)

    @mock.patch('apps.scribe.services.pipeline.start_pipeline')
    def test_retry_resets_a_failed_encounter_and_restarts_it(self, start):
        Transcript.objects.create(encounter=self.encounter, text='x', segments=SEGMENTS)
        self._age(0, status=Encounter.Status.FAILED)
        self.client.force_login(self.user)
        resp = self.client.post(reverse('scribe:retry', args=[self.encounter.pk]))
        self.assertRedirects(resp, reverse('scribe:detail', args=[self.encounter.pk]),
                             fetch_redirect_response=False)
        self.encounter.refresh_from_db()
        self.assertEqual(self.encounter.status, Encounter.Status.UPLOADED)
        self.assertFalse(Transcript.objects.filter(encounter=self.encounter).exists())
        start.assert_called_once()

    @mock.patch('apps.scribe.services.pipeline.start_pipeline')
    def test_retry_refuses_an_encounter_that_is_not_failed(self, start):
        self.client.force_login(self.user)
        self.client.post(reverse('scribe:retry', args=[self.encounter.pk]))
        start.assert_not_called()

    @mock.patch('apps.scribe.services.pipeline.start_pipeline')
    def test_retry_of_another_users_encounter_is_404(self, start):
        self._age(0, status=Encounter.Status.FAILED)
        other = User.objects.create_user('sc_rec2', email='rec2@test.invalid', password='pw')
        self.client.force_login(other)
        resp = self.client.post(reverse('scribe:retry', args=[self.encounter.pk]))
        self.assertEqual(resp.status_code, 404)
        start.assert_not_called()

    def _lose_audio(self):
        # What a redeploy on an ephemeral disk does: the row survives, the file does not.
        self.encounter.audio.storage.delete(self.encounter.audio.name)

    @mock.patch('apps.scribe.services.pipeline.start_pipeline')
    def test_retry_with_the_recording_gone_explains_instead_of_restarting(self, start):
        self._age(0, status=Encounter.Status.FAILED)
        self._lose_audio()
        self.client.force_login(self.user)
        self.client.post(reverse('scribe:retry', args=[self.encounter.pk]))
        start.assert_not_called()
        self.encounter.refresh_from_db()
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertIn('no longer on the server', self.encounter.error_message)

    def test_detail_hides_retry_when_the_recording_is_gone(self):
        self._age(0, status=Encounter.Status.FAILED)
        self.client.force_login(self.user)
        url = reverse('scribe:detail', args=[self.encounter.pk])
        self.assertContains(self.client.get(url), reverse('scribe:retry', args=[self.encounter.pk]))
        self._lose_audio()
        resp = self.client.get(url)
        self.assertNotContains(resp, reverse('scribe:retry', args=[self.encounter.pk]))
        self.assertContains(resp, 'upload the recording again')

    def test_pipeline_fails_clearly_when_the_recording_is_gone(self):
        self._lose_audio()
        with mock.patch('apps.scribe.services.transcription.transcribe_audio') as ta:
            pipeline.run_pipeline(self.encounter.pk)
        ta.assert_not_called()
        self.encounter.refresh_from_db()
        self.assertEqual(self.encounter.status, Encounter.Status.FAILED)
        self.assertIn('no longer on the server', self.encounter.error_message)

    def test_model_size_can_come_from_the_environment(self):
        from apps.scribe.services.transcription import _cfg
        with mock.patch.dict(os.environ, {'SCRIBE_WHISPER_MODEL': 'tiny'}):
            self.assertEqual(_cfg('SCRIBE_WHISPER_MODEL', 'small'), 'tiny')
        with self.settings(SCRIBE_WHISPER_MODEL='base'):
            self.assertEqual(_cfg('SCRIBE_WHISPER_MODEL', 'small'), 'base')
