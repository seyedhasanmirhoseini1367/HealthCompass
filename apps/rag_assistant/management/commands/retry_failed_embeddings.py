"""
Management command: re-embed chunks whose embedding never succeeded.

This is the recovery path for CB-2. Before it existed, an embedding failure
left chunks persisted with a NULL vector, retrieval silently excluded them, and
the only remedies were:

  * `reindex_records`, which DELETES every MedicalDocument for the record and
    rebuilds it — discarding embeddings that had succeeded and spending quota
    to recompute them, or
  * `reindex_all_embeddings --stale-only`, which targets provenance mismatches
    and does not look at NULL embeddings at all.

Neither recovered a failed embedding in place, so a quota outage during upload
made those records permanently invisible to the assistant.

This command is deliberately narrow and safe to run repeatedly:

  * it only selects chunks that have NO vector, so successful embeddings are
    never recomputed or overwritten;
  * it embeds IN PLACE — no document or chunk is deleted or recreated, so chunk
    ids are stable and no duplicates can appear;
  * it is idempotent — running it twice embeds nothing the second time;
  * it goes through EmbeddingService.embed_chunks(), so the consent/egress
    guard and provenance stamping apply exactly as they do during ingestion;
  * BLOCKED chunks (consent refused) are skipped unless --include-blocked is
    passed, so a retry never pushes data the patient declined to share.

Usage:
    python manage.py retry_failed_embeddings --dry-run
    python manage.py retry_failed_embeddings
    python manage.py retry_failed_embeddings --patient <id>
    python manage.py retry_failed_embeddings --limit 500
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Re-embed chunks whose embedding is missing (pending/failed), in place.'

    def add_arguments(self, parser):
        parser.add_argument('--patient', type=str, default=None,
                            help='Restrict to one patient (pk).')
        parser.add_argument('--limit', type=int, default=0,
                            help='Maximum chunks to attempt (0 = no limit).')
        parser.add_argument('--dry-run', action='store_true', default=False,
                            help='Report what would be attempted; change nothing.')
        parser.add_argument('--include-blocked', action='store_true', default=False,
                            help='Also attempt chunks marked BLOCKED by the consent '
                                 'guard. Off by default — consent is not a failure.')

    def handle(self, *args, **options):
        from django.contrib.auth import get_user_model
        from apps.rag_assistant.models import MedicalChunk
        from apps.rag_assistant.services.embedding_service import EmbeddingService

        Status = MedicalChunk.EmbeddingStatus

        # No vector is the ground truth: status alone could be stale if a row
        # was written before this field existed.
        qs = MedicalChunk.objects.filter(embedding__isnull=True)

        if not options['include_blocked']:
            qs = qs.exclude(embedding_status=Status.BLOCKED)

        if options['patient']:
            User = get_user_model()
            try:
                patient = User.objects.get(pk=options['patient'])
            except User.DoesNotExist:
                raise CommandError(f'No user with pk={options["patient"]}')
            qs = qs.filter(patient=patient)

        qs = qs.select_related('patient', 'document').order_by('document_id', 'chunk_index')
        if options['limit']:
            qs = qs[:options['limit']]

        chunks = list(qs)
        if not chunks:
            self.stdout.write(self.style.SUCCESS(
                'Nothing to do — every chunk already has an embedding.'))
            return

        by_status = {}
        for c in chunks:
            by_status[c.embedding_status] = by_status.get(c.embedding_status, 0) + 1
        self.stdout.write(f'{len(chunks)} chunk(s) without an embedding:')
        for status, count in sorted(by_status.items()):
            self.stdout.write(f'  {status:<10} {count}')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run: nothing was changed.'))
            return

        # Grouped per patient: embed_chunks() reads the consent guard from the
        # first chunk's owner, so a mixed batch would apply one patient's
        # decision to another's data.
        svc = EmbeddingService()
        per_patient = {}
        for c in chunks:
            per_patient.setdefault(c.patient_id, []).append(c)

        for patient_id, group in per_patient.items():
            svc.embed_chunks(group)

        ids = [c.pk for c in chunks]
        recovered = MedicalChunk.objects.filter(
            pk__in=ids, embedding__isnull=False).count()
        still_missing = len(ids) - recovered

        self.stdout.write(self.style.SUCCESS(f'Recovered: {recovered}'))
        if still_missing:
            self.stdout.write(self.style.ERROR(
                f'Still missing: {still_missing} — see embedding_error on those rows.'))
