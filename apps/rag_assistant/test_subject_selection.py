"""
The subject selector at the HTTP boundary.

`accounts.test_assistant_scope` covers the predicate. This covers the thing an
attacker actually touches: a `subject` value in a POST body. The properties that
matter here are that the id is untrusted, that refusal is indistinguishable from
non-existence, and that asking about somebody lands in THEIR access trail.
"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.consent import grant_consent
from apps.accounts.models import ConsentPurpose, DoctorAccessLog, SharingGrant
from apps.rag_assistant import subject as subject_mod

User = get_user_model()

ANSWER = ('An answer.', [], 'groq', 1, False, [])


class _Boundary(TestCase):

    def setUp(self):
        self.child = User.objects.create_user(
            'sb_child', email='sb_child@test.invalid', password='pw',
            role='patient', first_name='Maria')
        self.parent = User.objects.create_user(
            'sb_parent', email='sb_parent@test.invalid', password='pw',
            role='patient', first_name='Anna')
        self.client.force_login(self.child)

    def _share(self, **kwargs):
        options = dict(can_view_records=True, status=SharingGrant.Status.ACTIVE)
        options.update(kwargs)
        SharingGrant.objects.create(patient=self.parent, recipient=self.child,
                                    **options)
        grant_consent(self.parent, ConsentPurpose.EXTERNAL_LLM)

    def _ask(self, subject=None):
        body = {'message': 'what are my medications?'}
        if subject is not None:
            body['subject'] = subject
        return self.client.post('/assistant/send/', json.dumps(body),
                                content_type='application/json')


class SubjectEnforcementTests(_Boundary):

    def test_asking_about_yourself_needs_no_share(self):
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ask.call_args[0][0], self.child)

    def test_a_permitted_subject_is_used_for_retrieval(self):
        """ACCEPTANCE — the answer must come from the parent's records."""
        self._share()
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask(str(self.parent.pk))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ask.call_args[0][0], self.parent)

    def test_a_subject_without_a_share_is_refused(self):
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask(str(self.parent.pk))

        self.assertEqual(response.status_code, 404)
        ask.assert_not_called()

    def test_an_alerts_only_share_is_refused(self):
        """"Tell me if something is wrong" is not "read her file to an AI"."""
        SharingGrant.objects.create(
            patient=self.parent, recipient=self.child, can_view_alerts=True,
            status=SharingGrant.Status.ACTIVE)
        grant_consent(self.parent, ConsentPurpose.EXTERNAL_LLM)

        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask(str(self.parent.pk))

        self.assertEqual(response.status_code, 404)
        ask.assert_not_called()

    def test_a_share_without_the_subjects_consent_is_refused(self):
        """ACCEPTANCE — the records are hers, so the consent must be hers."""
        SharingGrant.objects.create(
            patient=self.parent, recipient=self.child, can_view_records=True,
            status=SharingGrant.Status.ACTIVE)
        grant_consent(self.child, ConsentPurpose.EXTERNAL_LLM)   # wrong person

        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask(str(self.parent.pk))

        self.assertEqual(response.status_code, 404)
        ask.assert_not_called()

    def test_an_unknown_id_looks_exactly_like_a_refusal(self):
        """
        ACCEPTANCE — otherwise the status code is an oracle for which user ids
        exist, and a different one for which of them share with you.
        """
        self._share()
        refused = self._ask(str(self.parent.pk + 9999))

        SharingGrant.objects.all().delete()
        not_permitted = self._ask(str(self.parent.pk))

        self.assertEqual(refused.status_code, not_permitted.status_code)

    def test_a_malformed_subject_is_refused_not_crashed(self):
        for value in ('../../etc', 'null', '1 OR 1=1', '   '):
            with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                       return_value=ANSWER):
                response = self._ask(value)
            self.assertIn(response.status_code, (200, 404),
                          f'{value!r} produced {response.status_code}')

    def test_a_revoked_share_stops_working_immediately(self):
        self._share()
        SharingGrant.objects.filter(patient=self.parent).first().revoke(
            by=self.parent, reason='no')

        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask(str(self.parent.pk))

        self.assertEqual(response.status_code, 404)
        ask.assert_not_called()


class AccessTrailTests(_Boundary):

    def test_asking_about_someone_is_recorded_in_their_trail(self):
        """"Who has looked at my data" must include "my daughter asked an AI"."""
        self._share()
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER):
            self._ask(str(self.parent.pk))

        self.assertTrue(DoctorAccessLog.objects.filter(
            actor=self.child, patient=self.parent,
            resource='assistant:question').exists())

    def test_asking_about_yourself_is_not_logged_as_an_access_event(self):
        """Logging it would bury the entries that matter in noise."""
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER):
            self._ask()

        self.assertFalse(DoctorAccessLog.objects.filter(
            actor=self.child, patient=self.child).exists())

    def test_the_question_text_is_not_stored_in_the_trail(self):
        """
        The trail is read by the subject to see WHO looked, not to read what
        was typed about them — and the question can carry clinical detail.
        """
        self._share()
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER):
            self._ask(str(self.parent.pk))

        entry = DoctorAccessLog.objects.get(patient=self.parent)
        self.assertNotIn('medications', entry.resource)


class EveryoneTests(_Boundary):
    """"Everyone" answers per person; it never merges records into one prompt."""

    def test_it_answers_once_per_person(self):
        self._share()
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            response = self._ask('all')

        self.assertEqual(response.status_code, 200)
        subjects = [call[0][0] for call in ask.call_args_list]
        self.assertIn(self.child, subjects)
        self.assertIn(self.parent, subjects)

    def test_each_answer_is_labelled_with_whose_records_it_used(self):
        """ACCEPTANCE — an unattributed medication list is a clinical hazard."""
        self._share()
        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER):
            response = self._ask('all')

        text = response.json()['response']
        self.assertIn('**You**', text)
        self.assertIn('**Anna**', text)

    def test_everyone_only_covers_permitted_people(self):
        stranger = User.objects.create_user(
            'sb_stranger', email='sb_s@test.invalid', password='pw',
            role='patient', first_name='Other')

        with patch('apps.rag_assistant.services.rag_service.RAGService.ask',
                   return_value=ANSWER) as ask:
            self._ask('all')

        self.assertNotIn(stranger, [call[0][0] for call in ask.call_args_list])


class SelectorTests(_Boundary):

    def test_the_selector_is_hidden_when_there_is_nothing_to_choose(self):
        """A select with one option teaches nothing and takes space."""
        response = self.client.get('/assistant/')

        self.assertEqual(len(response.context['subject_choices']), 1)
        self.assertNotContains(response, 'id="subjectSelect"')

    def test_the_selector_appears_once_someone_shares(self):
        self._share()
        response = self.client.get('/assistant/')

        self.assertContains(response, 'id="subjectSelect"')
        labels = [c['label'] for c in response.context['subject_choices']]
        self.assertEqual(labels, ['My own data', 'Anna', 'Everyone'])

    def test_the_offered_options_all_pass_the_predicate(self):
        """The list shown and the check enforced must never disagree."""
        from apps.accounts.authz import can_ask_assistant_about

        self._share()
        for choice in subject_mod.choices_for(self.child):
            if choice['value'] in (subject_mod.SELF, subject_mod.ALL):
                continue
            person = User.objects.get(pk=choice['value'])
            self.assertTrue(can_ask_assistant_about(self.child, person))
