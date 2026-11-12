"""
The assistant answers about the caller and nobody else.

Why this is a source-level guard
--------------------------------
Every other read path in this system carries an explicit predicate —
`doctor_has_active_link`, `sharing_grant`, `can_access_media`. The assistant
carries none, and does not need one, because of a structural fact: `ask()` and
`stream_ask()` take the patient as an argument, and all four call sites pass
`request.user`. Retrieval then filters `patient=patient`, so there is no input
through which one user's question can reach another user's chunks.

That safety is a property of the call sites, not of the pipeline. The moment
someone adds "let the doctor ask about their patient" by passing a different
user, retrieval will happily serve those chunks — no predicate stands in the
way, consent is checked against the *subject* rather than the reader, and the
change looks like a one-line feature.

So the guard is written where the property actually lives. A runtime test cannot
catch this: the bypass is code that does not exist yet.

That feature was wanted, and this test failed as designed. It was not deleted:
the predicate it asked for now exists as `accounts.authz.can_ask_assistant_about`
plus `rag_assistant.subject.resolve()`, which is the only thing permitted to
produce a subject. The allowlist below therefore accepts the names that resolver
returns and nothing else, so a call site that reaches past it still fails here.

The predicate is stricter than viewing the person's care page, because answering
transmits their records to an external LLM: it needs the RECORDS scope and the
SUBJECT's own consent. See apps/accounts/test_assistant_scope.py.
"""
import pathlib
import re

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.rag_assistant.models import MedicalChunk, MedicalDocument

User = get_user_model()

#: Anything that is not a test and not a migration.
_SOURCE = [p for p in pathlib.Path('apps').rglob('*.py')
           if 'test' not in p.name and 'migrations' not in p.parts]

#: `.ask(` / `.stream_ask(` with whatever it was handed first.
_CALL = re.compile(r'\.(?:stream_)?ask\(\s*\n?\s*(?:patient\s*=\s*)?([A-Za-z_][\w.]*)')

#: The acceptable subjects.
#:
#: `person` and `stream_subject` are the resolved output of
#: `rag_assistant.subject.resolve()`, which is the predicate this guard asked
#: for. Everything else on this list forwards a parameter rather than choosing
#: a subject.
_ALLOWED = {'request.user', 'patient', 'self', 'person', 'stream_subject'}


class CallSiteTests(TestCase):

    def test_the_assistant_is_only_ever_asked_about_the_caller(self):
        """ACCEPTANCE — no predicate protects this; the call sites do."""
        offenders = []
        for path in _SOURCE:
            text = path.read_text(encoding='utf-8', errors='ignore')
            for match in _CALL.finditer(text):
                subject = match.group(1)
                if subject not in _ALLOWED:
                    line = text[:match.start()].count('\n') + 1
                    offenders.append(f'{path}:{line} passes {subject!r}')

        self.assertEqual(offenders, [], (
            'The assistant was asked about someone other than the caller. '
            'Retrieval filters on the patient it is given and nothing else, so '
            'this reaches their chunks without consulting sharing, the doctor '
            'link, or that patient\'s consent. Put an authorization predicate '
            'at the ask()/stream_ask() choke point before allowing it.'))

    def test_the_guard_would_notice_a_new_call_site(self):
        """
        A guard that matches nothing passes for the wrong reason.

        Counted as call sites, not files: `rag_service` *defines* ask() rather
        than calling it, so the two web views and the two API views are all
        there is. If this drops, the pattern has stopped matching reality.
        """
        found = sum(len(_CALL.findall(p.read_text(encoding='utf-8', errors='ignore')))
                    for p in _SOURCE)
        self.assertGreaterEqual(found, 4,
                                'the call-site pattern no longer matches the '
                                'real call sites — it needs updating, not passing')

    def test_a_disallowed_subject_is_actually_detected(self):
        """Mutation check on the matcher itself, without editing the tree."""
        sample = 'RAGService().ask(some_other_patient, query, history)'
        match = _CALL.search(sample)

        self.assertIsNotNone(match)
        self.assertNotIn(match.group(1), _ALLOWED)


class ChunkIsolationTests(TestCase):
    """The other half: retrieval's own filter, exercised rather than read."""

    def setUp(self):
        self.mine = User.objects.create_user(
            'rb_mine', email='rb_mine@test.invalid', password='pw', role='patient')
        self.theirs = User.objects.create_user(
            'rb_theirs', email='rb_theirs@test.invalid', password='pw', role='patient')

    def _chunk(self, owner, content):
        document = MedicalDocument.objects.create(
            patient=owner, title='Note', document_type='note', content=content)
        return MedicalChunk.objects.create(
            patient=owner, document=document, chunk_index=0, content=content)

    def test_another_patients_chunks_are_never_loaded(self):
        import numpy as np

        from apps.rag_assistant.services.embedding_service import (
            EmbeddingService, active_embedding_dim, active_embedding_model)

        self._chunk(self.theirs, 'Warfarin 5mg daily')
        mine = self._chunk(self.mine, 'Metformin 500mg')

        # A local vector: this test must not reach the provider.
        vector = np.ones(active_embedding_dim(), dtype=np.float32)
        for chunk in MedicalChunk.objects.all():
            chunk.embedding            = vector.tobytes()
            chunk.embedding_model      = active_embedding_model()
            chunk.embedding_dimensions = active_embedding_dim()
            chunk.save()

        texts, vectors, meta = EmbeddingService().load_patient_embeddings(self.mine)

        self.assertEqual(texts, [mine.content])
        self.assertEqual(len(vectors), 1)

    def test_deleting_the_record_removes_the_chunks_that_quote_it(self):
        """
        Revocation has no stale-embedding problem here, but deletion would if
        the vectors lived outside the database. They do not: the embedding is a
        column on the chunk, so the cascade takes it.
        """
        from apps.medical_records.models import MedicalRecord

        record = MedicalRecord.objects.create(
            patient=self.mine, title='Note', record_type='other')
        document = MedicalDocument.objects.create(
            patient=self.mine, record=record, title='Note',
            document_type='note', content='Metformin 500mg')
        MedicalChunk.objects.create(
            patient=self.mine, document=document, chunk_index=0,
            content='Metformin 500mg', embedding=b'\x00\x00\x00\x00')

        record.delete()

        self.assertEqual(MedicalChunk.objects.count(), 0)
        self.assertEqual(MedicalDocument.objects.count(), 0)
