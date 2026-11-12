"""
Copy stored embeddings (BinaryField) into the pgvector column, in batches.

Idempotent: only rows whose embedding_vec is NULL are touched. PostgreSQL only.

  python manage.py backfill_embedding_vectors              # copy
  python manage.py backfill_embedding_vectors --verify 50  # compare old and new search

--verify picks up to N patients, runs a nearest-neighbour query with one of
their own chunk vectors through NumPy (old path) and through pgvector (new path),
and reports how much the top-10 lists overlap. Expect >= 0.9 before relying on
the new path; HNSW is approximate and halfvec rounds to 16 bits.
"""
import numpy as np
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = 'Backfill embedding_vec from the stored embedding blobs (PostgreSQL).'

    def add_arguments(self, parser):
        parser.add_argument('--batch', type=int, default=500)
        parser.add_argument('--verify', type=int, default=0, metavar='PATIENTS',
                            help='Compare old and new nearest-neighbour results.')

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError('pgvector backfill needs PostgreSQL.')
        from apps.rag_assistant.models import GeneralKnowledgeChunk, MedicalChunk

        for Model in (MedicalChunk, GeneralKnowledgeChunk):
            copied = 0
            while True:
                batch = list(Model.objects.filter(embedding__isnull=False, embedding_vec__isnull=True)
                             .only('pk', 'embedding')[:options['batch']])
                if not batch:
                    break
                for chunk in batch:
                    chunk.embedding_vec = np.frombuffer(bytes(chunk.embedding), dtype=np.float32)
                Model.objects.bulk_update(batch, ['embedding_vec'])
                copied += len(batch)
            self.stdout.write(f'{Model.__name__}: copied {copied} vector(s).')

        if options['verify']:
            self._verify(options['verify'])

    def _verify(self, n_patients):
        from pgvector.django import CosineDistance

        from apps.rag_assistant.models import MedicalChunk

        patient_ids = list(MedicalChunk.objects.filter(embedding_vec__isnull=False)
                           .values_list('patient_id', flat=True).distinct()[:n_patients])
        overlaps = []
        for pid in patient_ids:
            rows = list(MedicalChunk.objects.filter(patient_id=pid, embedding__isnull=False)
                        .values_list('pk', 'embedding'))
            if len(rows) < 2:
                continue
            ids = [r[0] for r in rows]
            matrix = np.stack([np.frombuffer(bytes(r[1]), dtype=np.float32) for r in rows])
            query = matrix[0]
            norms = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query)
            sims = matrix @ query / np.where(norms == 0, 1, norms)
            k = min(10, len(ids))
            old = {ids[i] for i in np.argsort(-sims)[:k]}
            new = set(MedicalChunk.objects.filter(patient_id=pid, embedding_vec__isnull=False)
                      .order_by(CosineDistance('embedding_vec', query))
                      .values_list('pk', flat=True)[:k])
            overlaps.append(len(old & new) / k)
        if not overlaps:
            self.stdout.write('verify: no patient has enough chunks to compare.')
            return
        mean = sum(overlaps) / len(overlaps)
        self.stdout.write(f'verify: {len(overlaps)} patient(s), mean top-10 overlap {mean:.2f}, '
                          f'min {min(overlaps):.2f}.')
        if mean < 0.9:
            self.stdout.write(self.style.WARNING('Overlap below 0.9: keep RAG_PGVECTOR=False until investigated.'))
