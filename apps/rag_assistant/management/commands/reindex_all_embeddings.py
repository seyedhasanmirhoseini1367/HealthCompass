from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Clear all stored chunk embeddings and re-index with gemini-embedding-001 (3072-dim).'

    def handle(self, *args, **options):
        from apps.rag_assistant.models import MedicalChunk
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        cleared = MedicalChunk.objects.exclude(embedding=None).update(embedding=None)
        self.stdout.write(f'Cleared embeddings on {cleared} chunks.')

        svc    = EmbeddingService()
        chunks = MedicalChunk.objects.all().select_related('document')
        total  = chunks.count()
        self.stdout.write(f'Re-embedding {total} chunks...')

        svc.embed_chunks(chunks)

        done = MedicalChunk.objects.exclude(embedding=None).count()
        self.stdout.write(self.style.SUCCESS(f'Done. {done}/{total} chunks embedded.'))
