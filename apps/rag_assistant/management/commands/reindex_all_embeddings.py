"""
Management command: re-embed stored chunks with the currently configured model.

Usage:
    python manage.py reindex_all_embeddings --stale-only   # recommended
    python manage.py reindex_all_embeddings                # everything
    python manage.py reindex_all_embeddings --stale-only --general

--stale-only re-embeds just the chunks whose recorded provenance does not match
the active model, which is what a model migration actually needs. It also does
not clear vectors up front, so a mid-run API failure leaves the existing index
intact rather than empty.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Re-embed chunk vectors with the active embedding model.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--stale-only', action='store_true', default=False,
            help='Only re-embed chunks incompatible with the active model. '
                 'Does not clear existing vectors first.',
        )
        parser.add_argument(
            '--general', action='store_true', default=False,
            help='Also re-embed GeneralKnowledgeChunk rows.',
        )
        parser.add_argument(
            '--batch-size', type=int, default=100,
            help='Chunks per embedding request batch (default 100, the Gemini limit).',
        )

    def handle(self, *args, **options):
        from apps.rag_assistant.models import MedicalChunk
        from apps.rag_assistant.services.embedding_service import (
            EmbeddingService, active_embedding_model, audit_embeddings,
        )

        svc          = EmbeddingService()
        active_model = active_embedding_model()
        self.stdout.write(f'Active embedding model: {active_model}')

        if options['stale_only']:
            # Stale == provenance does not match the active model. Rows with no
            # recorded provenance are included: their vectors may well be fine,
            # but that cannot be verified, and re-embedding is the safe answer.
            chunks = MedicalChunk.objects.exclude(embedding_model=active_model)
            self.stdout.write('Mode: stale-only (existing vectors preserved until replaced)')

            unstamped = MedicalChunk.objects.filter(embedding_model='') \
                                            .exclude(embedding__isnull=True).count()
            if unstamped:
                # embedding_status reports these as usable in non-strict mode;
                # this command re-embeds them because "usable" there means
                # "right shape", which is not the same as "known to be right".
                self.stdout.write(self.style.WARNING(
                    f'  Note: {unstamped} row(s) have no recorded provenance and will be '
                    're-embedded. If you know which model produced them, '
                    '`backfill_embedding_provenance` records that instead — no API cost.'
                ))
        else:
            chunks = MedicalChunk.objects.all()
            self.stdout.write(self.style.WARNING(
                'Mode: full re-embed of every chunk. This will spend API quota on '
                'vectors that may already be correct — --stale-only is usually what you want.'
            ))

        chunks = chunks.select_related('document')
        total  = chunks.count()
        if not total:
            self.stdout.write(self.style.SUCCESS('Nothing to re-embed.'))
            return

        self.stdout.write(f'Re-embedding {total} MedicalChunk row(s)…')
        done = 0
        batch_size = max(1, options['batch_size'])
        # Snapshot the ids first: embed_chunks() mutates embedding_model, which
        # would otherwise shift the queryset out from under a lazy iterator.
        ids = list(chunks.values_list('pk', flat=True))
        for start in range(0, len(ids), batch_size):
            batch = list(
                MedicalChunk.objects.filter(pk__in=ids[start:start + batch_size])
                                    .select_related('document')
            )
            svc.embed_chunks(batch)
            done += len(batch)
            self.stdout.write(f'  {done}/{total}')

        if options['general']:
            self._reindex_general(svc, active_model, batch_size)

        self.stdout.write('')
        for rep in (audit_embeddings(MedicalChunk),):
            self.stdout.write(
                f"{rep['model']}: {rep['compatible']} compatible, {rep['stale']} stale"
            )
        self.stdout.write(self.style.SUCCESS('Done. Run `manage.py embedding_status` to verify.'))

    def _reindex_general(self, svc, active_model, batch_size):
        """GeneralKnowledgeChunk has no embed_chunks() helper — embed in place."""
        import numpy as np
        from django.conf import settings
        from django.utils import timezone

        from apps.rag_assistant.models import GeneralKnowledgeChunk

        stale = list(GeneralKnowledgeChunk.objects.exclude(embedding_model=active_model))
        if not stale:
            self.stdout.write('GeneralKnowledgeChunk: nothing stale.')
            return

        self.stdout.write(f'Re-embedding {len(stale)} GeneralKnowledgeChunk row(s)…')
        version = settings.RAG_CONFIG.get('EMBEDDING_MODEL_VERSION', '')
        for start in range(0, len(stale), batch_size):
            batch = stale[start:start + batch_size]
            vecs  = svc.embed_batch([c.content for c in batch], task_type='RETRIEVAL_DOCUMENT')
            now   = timezone.now()
            for chunk, vec in zip(batch, vecs):
                if not np.any(vec):
                    continue
                chunk.embedding               = vec.astype(np.float32).tobytes()
                chunk.embedding_model         = active_model
                chunk.embedding_model_version = version
                chunk.embedding_dimensions    = int(len(vec))
                chunk.embedded_at             = now
            GeneralKnowledgeChunk.objects.bulk_update(batch, [
                'embedding', 'embedding_model', 'embedding_model_version',
                'embedding_dimensions', 'embedded_at',
            ])
            self.stdout.write(f'  {min(start + batch_size, len(stale))}/{len(stale)}')
