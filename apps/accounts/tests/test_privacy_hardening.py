"""Prompt 5 of the production-readiness audit: consent before egress (SEC-09),
minimum cohort (SEC-11), assistant logs per subject (SEC-12), field encryption
keys (SEC-15), erasure completeness and enumeration (SEC-14)."""
from datetime import date
from io import StringIO
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.consent import grant_consent
from apps.accounts.models import ConsentPurpose, DoctorAccessLog, PatientProfile, SharingGrant
from apps.medical_records.models import MedicalRecord, ParsedLabValue
from apps.rag_assistant.models import ChatSession, QueryLog

User = get_user_model()


def _user(name, **kw):
    return User.objects.create_user(name, email=f'{name}@test.invalid', password='pw', **kw)


class ConsentEgressDefaultTests(TestCase):
    def test_all_points_are_enforced_by_default(self):
        from django.conf import settings
        self.assertEqual(list(settings.CONSENT_ENFORCED_EGRESS), ['all'])
        self.assertEqual(list(settings.PHI_LLM_PROVIDERS), ['gemini', 'anthropic'])

    def test_upload_without_consent_reaches_no_provider(self):
        from apps.medical_records.services import MedicalRecordService
        from apps.rag_assistant.services.embedding_service import EmbeddingService
        user = _user('noconsent')
        with patch.object(EmbeddingService, '_call_api') as embed, \
                patch('apps.medical_records.parsers.extraction._call_llm_for_structuring',
                      create=True) as llm:
            result = MedicalRecordService.create_from_text(user, 'GLUCOSE 5.4 mmol/L')
        embed.assert_not_called()
        llm.assert_not_called()
        record = MedicalRecord.objects.get(pk=result['record'].pk)
        self.assertEqual(record.index_status, MedicalRecord.IndexStatus.BLOCKED)

    def test_the_record_page_says_it_was_not_ai_processed(self):
        user = _user('notice')
        record = MedicalRecord.objects.create(patient=user, title='r')
        MedicalRecord.objects.filter(pk=record.pk).update(index_status='blocked')
        self.client.force_login(user)
        self.assertContains(self.client.get(f'/records/{record.pk}/'), 'has not been processed by AI')

    def test_granting_consent_reindexes_that_users_records_only(self):
        alice, bob = _user('alice'), _user('bob')
        a = MedicalRecord.objects.create(patient=alice, title='a')
        b = MedicalRecord.objects.create(patient=bob, title='b')
        MedicalRecord.objects.filter(pk__in=[a.pk, b.pk]).update(index_status='blocked')
        with patch('apps.rag_assistant.signals.dispatch_index') as dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                grant_consent(alice, ConsentPurpose.EXTERNAL_LLM)
        self.assertEqual([c.args[0] for c in dispatch.call_args_list], [str(a.pk)])


@override_settings(POPULATION_MIN_COHORT=5)
class MinimumCohortTests(TestCase):
    def setUp(self):
        cache.clear()

    def _patients(self, n, value=5.0):
        people = []
        for i in range(n):
            p = _user(f'coh{i}_{n}')
            record = MedicalRecord.objects.create(patient=p, title='lab', record_date=date(2026, 1, 15))
            ParsedLabValue.objects.create(record=record, parameter_name='Glucose', value=str(value + i),
                                          unit='mmol/L')
            record2 = MedicalRecord.objects.create(patient=p, title='lab2', record_date=date(2026, 2, 15))
            ParsedLabValue.objects.create(record=record2, parameter_name='Glucose', value=str(value + i),
                                          unit='mmol/L')
            people.append(p)
        return people

    def _stats(self, **kw):
        from apps.ai_insights.services.insights_service import get_population_biomarker_stats
        return get_population_biomarker_stats(**kw)

    def test_four_patients_give_nothing(self):
        self._patients(4)
        trending, latest, avg, _ = self._stats()
        self.assertEqual((trending, latest, avg), ({}, {}, {}))

    def test_five_patients_are_shown(self):
        self._patients(5)
        trending, latest, avg, _ = self._stats()
        self.assertIn('Glucose', latest)
        self.assertIn('Glucose', avg)
        self.assertEqual(len(trending['Glucose']), 2)

    def test_the_viewer_never_counts_towards_their_own_comparison(self):
        people = self._patients(6)
        viewer = people[0]
        _, _, avg_all, _ = self._stats()
        _, _, avg_without, _ = self._stats(exclude_patient=viewer)
        self.assertNotEqual(avg_all['Glucose'], avg_without['Glucose'])
        # Excluding the viewer can push a cohort below the minimum.
        cache.clear()
        ParsedLabValue.objects.filter(record__patient=people[5]).delete()
        _, _, avg, _ = self._stats(exclude_patient=viewer)
        self.assertNotIn('Glucose', avg)


class AssistantSubjectTests(TestCase):
    def setUp(self):
        self.carer, self.mother = _user('carer'), _user('mother')
        self.session = ChatSession.objects.create(patient=self.carer, title='s')
        QueryLog.objects.create(session=self.session, query='mine', response='a1', subject=self.carer)
        QueryLog.objects.create(session=self.session, query='hers', response='a2', subject=self.mother)

    def test_history_never_crosses_subjects(self):
        from apps.rag_assistant.services.chat_service import load_history
        self.assertEqual([h['query'] for h in load_history(self.session, self.carer)], ['mine'])
        self.assertEqual([h['query'] for h in load_history(self.session, self.mother)], ['hers'])

    def test_revoking_the_share_removes_the_carers_answers_about_the_patient(self):
        grant = SharingGrant.objects.create(patient=self.mother, recipient=self.carer,
                                            can_view_records=True)
        grant.revoke(by=self.mother)
        self.assertEqual(list(QueryLog.objects.values_list('query', flat=True)), ['mine'])

    def test_erasing_the_patient_removes_answers_about_them_everywhere(self):
        from apps.accounts.services import purge_user_data
        purge_user_data(self.mother)
        self.assertEqual(list(QueryLog.objects.values_list('query', flat=True)), ['mine'])

    def test_an_empty_answer_is_not_saved(self):
        from apps.rag_assistant.services.chat_service import persist
        self.assertIsNone(persist(self.session, 'q', '   ', subject=self.carer))


class FieldEncryptionTests(TestCase):
    def _raw(self, profile):
        with connection.cursor() as cur:
            cur.execute(f'SELECT national_id FROM {PatientProfile._meta.db_table} WHERE id = %s',
                        [profile.pk])
            return cur.fetchone()[0]

    def test_data_under_the_old_key_stays_readable_after_a_new_primary(self):
        profile = PatientProfile.objects.create(user=_user('enc1'), national_id='010101-123A')
        with override_settings(FIELD_ENCRYPTION_KEYS=[Fernet.generate_key().decode()]):
            self.assertEqual(PatientProfile.objects.get(pk=profile.pk).national_id, '010101-123A')

    def test_rotation_re_encrypts_with_the_primary_key(self):
        from django.core.management import call_command
        profile = PatientProfile.objects.create(user=_user('enc2'), national_id='020202-456B')
        before = self._raw(profile)
        new_key = Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEYS=[new_key]):
            call_command('rotate_field_encryption', '--apply', stdout=StringIO())
            after = self._raw(profile)
            self.assertNotEqual(before, after)
            self.assertEqual(Fernet(new_key.encode()).decrypt(after.encode()).decode(), '020202-456B')

    def test_a_corrupted_value_does_not_leak_ciphertext(self):
        profile = PatientProfile.objects.create(user=_user('enc3'), national_id='030303-789C')
        with connection.cursor() as cur:
            cur.execute(f'UPDATE {PatientProfile._meta.db_table} SET national_id = %s WHERE id = %s',
                        ['gAAAAAcorrupted-token', profile.pk])
        loaded = PatientProfile.objects.get(pk=profile.pk)
        self.assertEqual(str(loaded.national_id), '')
        self.assertTrue(loaded.national_id.unreadable)
        loaded.blood_type = 'O+'
        loaded.save()
        self.assertEqual(self._raw(profile), 'gAAAAAcorrupted-token')  # preserved, not wiped


class ErasureActorLabelTests(TestCase):
    def test_no_access_log_keeps_the_erased_username(self):
        from apps.accounts.services import purge_user_data
        user = _user('erase_me')
        DoctorAccessLog.objects.create(actor=user, patient=user, resource='share_granted:9')
        purge_user_data(user)
        self.assertFalse(DoctorAccessLog.objects.filter(actor_label__contains='erase_me').exists())


@override_settings(ACCOUNT_EMAIL_VERIFICATION='mandatory')
class EnumerationTests(TestCase):
    PAYLOAD = {'password': 'Correct-Horse-Battery-77', 'password2': 'Correct-Horse-Battery-77'}

    def setUp(self):
        cache.clear()
        _user('taken')

    def test_api_register_answers_the_same_for_new_and_taken_addresses(self):
        new = APIClient().post('/api/v1/auth/register/',
                               {'email': 'fresh@test.invalid', **self.PAYLOAD}, format='json')
        taken = APIClient().post('/api/v1/auth/register/',
                                 {'email': 'taken@test.invalid', **self.PAYLOAD}, format='json')
        self.assertEqual(new.status_code, taken.status_code)
        self.assertEqual(new.json()['detail'].replace('fresh', 'X'),
                         taken.json()['detail'].replace('taken', 'X'))
        self.assertTrue(any('already has an account' in m.body for m in mail.outbox))

    def test_web_register_answers_the_same_for_new_and_taken_addresses(self):
        form = {'password1': self.PAYLOAD['password'], 'password2': self.PAYLOAD['password']}
        new = self.client.post('/accounts/register/', {'username': 'fresh2', 'email': 'fresh2@test.invalid', **form})
        taken = self.client.post('/accounts/register/', {'username': 'other3', 'email': 'taken@test.invalid', **form})
        self.assertEqual(new.status_code, taken.status_code)
        self.assertEqual(new.content.decode().replace('fresh2', 'X'),
                         taken.content.decode().replace('taken', 'X'))

    def test_api_forgot_password_is_identical(self):
        a = APIClient().post('/api/v1/auth/forgot-password/', {'email': 'taken@test.invalid'}, format='json')
        b = APIClient().post('/api/v1/auth/forgot-password/', {'email': 'ghost@test.invalid'}, format='json')
        self.assertEqual((a.status_code, a.json()), (b.status_code, b.json()))
