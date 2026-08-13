"""
Management command: stamp provenance onto embeddings written before the
provenance columns existed.

This does NOT re-embed anything. It records which model an existing vector is
believed to have come from, so that unknown-provenance rows disappear and
EMBEDDING_STRICT_PROVENANCE can safely be turned on.

Safety properties:
  • Dry-run by default — pass --apply to write.
  • A row is only stamped when its decoded vector length matches the expected
    dimension of the claimed model. Rows that disagree are left untouched and
    reported, because stamping them would assert something false.
  • Rows that already carry provenance are never overwritten.

Usage:
    python manage.py backfill_embedding_provenance                    # dry run
    python manage.py backfill_embedding_provenance --apply
    python manage.py backfill_embedding_provenance --model models/gemini-embedding-001 --apply
"""
import numpy as np
from django.core.management.base import BaseCommand, CommandError


def _decode_len(raw):
    """Length of a stored vector, handling both pickle and raw-float32 encodings."""
    raw = bytes(raw)
    if raw[:2] in (b'\x80\x03', b'\x80\x04', b'\x80\x05'):
        import pickle
        return len(pickle.loads(raw))
    return len(np.frombuffer(raw, dtype=np.float32))


class Command(BaseCommand):
    help = 'Record the originating model on embeddings that predate provenance tracking.'

    def add_arguments(self, parser):
        parser.add_argument('--model', type=str, default=None,
                            help='Model name to attribute existing vectors to. '
                                 'Defaults to the active RAG_CONFIG EMBEDDING_MODEL.')
        parser.add_argument('--dim', type=int, default=None,
                            help='Expected dimension for that model. '
                                 'Defaults to the active RAG_CONFIG EMBEDDING_DIM.')
        parser.add_argument('--apply', action='store_true', default=False,
                            help='Actually write. Without this the command only reports.')

    def handle(self, *args, **options):
        from apps.rag_assistant.models import MedicalChunk, GeneralKnowledgeChunk
        from apps.rag_assistant.services.embedding_service import (
            active_embedding_dim, active_embedding_model,
        )

        model = options['model'] or active_embedding_model()
        dim   = options['dim'] or active_embedding_dim()
        if not model:
            raise CommandError('No model name available to attribute embeddings to.')

        self.stdout.write(
            f'Attributing unstamped embeddings to: {model} ({dim}-dim)'
            f"{'' if options['apply'] else '   [DRY RUN — nothing will be written]'}"
        )

        grand_stamped = grand_skipped = 0
        for model_cls in (MedicalChunk, GeneralKnowledgeChunk):
            stamped, skipped = self._backfill(model_cls, model, dim, options['apply'])
            grand_stamped += stamped
            grand_skipped += skipped

        if options['apply']:
            self.stdout.write(self.style.SUCCESS(
                f'\nStamped {grand_stamped} row(s). Left {grand_skipped} row(s) untouched.'
            ))
            if grand_skipped == 0:
                self.stdout.write(
                    'No unknown-provenance rows remain — you can now set '
                    'EMBEDDING_STRICT_PROVENANCE=True.'
                )
        else:
            self.stdout.write(self.style.WARNING(
                f'\nWould stamp {grand_stamped} row(s); {grand_skipped} would be skipped. '
                'Re-run with --apply to write.'
            ))

    def _backfill(self, model_cls, model, dim, apply):
        qs = model_cls.objects.filter(embedding_model='').exclude(embedding__isnull=True)
        total = qs.count()
        self.stdout.write(f'\n{model_cls.__name__}: {total} row(s) without provenance')
        if not total:
            return 0, 0

        to_update, mismatched = [], 0
        for chunk in qs.iterator(chunk_size=500):
            try:
                actual = _decode_len(chunk.embedding)
            except Exception as exc:
                self.stderr.write(f'  ! {chunk.pk}: undecodable embedding ({exc}) — skipped')
                mismatched += 1
                continue

            if actual != dim:
                # Do not assert a model this vector demonstrably did not come from.
                mismatched += 1
                continue

            chunk.embedding_model      = model
            chunk.embedding_dimensions = actual
            # embedded_at is deliberately left NULL: the true generation time is
            # unknown, and inventing one would be worse than recording nothing.
            to_update.append(chunk)

        if apply and to_update:
            model_cls.objects.bulk_update(
                to_update, ['embedding_model', 'embedding_dimensions'], batch_size=500,
            )

        self.stdout.write(
            f'  {len(to_update)} stampable, {mismatched} skipped (dimension != {dim})'
        )
        return len(to_update), mismatched
