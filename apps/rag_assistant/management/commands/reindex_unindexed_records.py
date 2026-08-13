"""
Management command: index records whose indexing never ran.

The gap this closes
-------------------
Indexing is dispatched to an in-process ThreadPoolExecutor with an unbounded
in-memory queue. A 200-record Kanta import queues 200 jobs; if the container is
redeployed the queue evaporates and those records are never chunked.

`retry_failed_embeddings` cannot recover them: it finds chunks whose embedding
is NULL, and a record that never reached DocumentProcessor has no chunk row to
find. The patient sees the record in their list and the assistant says it has no
such record — the same failure CB-2 was about, arriving by a different route.

`MedicalRecord.indexed_at` is stamped only when indexing succeeds, so NULL is
the marker for "this was never made searchable".

Safe to run repeatedly, and safe to run from a scheduler: it selects only
records that are actually missing an index, and skips ones uploaded in the last
few minutes so it never races the background worker still processing them.

Usage:
    python manage.py reindex_unindexed_records --dry-run
    python manage.py reindex_unindexed_records
    python manage.py reindex_unindexed_records --older-than 30 --limit 200
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Index medical records that were never successfully indexed.'

    def add_arguments(self, parser):
        parser.add_argument('--older-than', type=int, default=10,
                            help='Only records uploaded more than N minutes ago '
                                 '(default 10), so the background worker is not raced.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Maximum records to process (0 = no limit).')
        parser.add_argument('--patient', type=str, default=None,
                            help='Restrict to one patient (pk).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be indexed; change nothing.')

    def handle(self, *args, **options):
        from datetime import timedelta

        from django.contrib.auth import get_user_model
        from django.utils import timezone

        from apps.medical_records.models import MedicalRecord
        from apps.rag_assistant.services.rag_service import RAGService

        cutoff = timezone.now() - timedelta(minutes=options['older_than'])
        qs = (MedicalRecord.objects
              .filter(indexed_at__isnull=True, uploaded_at__lt=cutoff)
              .select_related('patient')
              .order_by('uploaded_at'))

        if options['patient']:
            User = get_user_model()
            qs = qs.filter(patient=User.objects.get(pk=options['patient']))
        if options['limit']:
            qs = qs[:options['limit']]

        records = list(qs)
        if not records:
            self.stdout.write(self.style.SUCCESS(
                'Nothing to do — every record older than the cutoff is indexed.'))
            return

        self.stdout.write(f'{len(records)} record(s) were never indexed:')
        for record in records[:10]:
            self.stdout.write(f'  {record.uploaded_at:%Y-%m-%d %H:%M}  '
                              f'{record.record_type:<12} {record.title[:48]}')
        if len(records) > 10:
            self.stdout.write(f'  … and {len(records) - 10} more')

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('--dry-run: nothing was changed.'))
            return

        svc = RAGService()
        indexed = failed = 0
        for record in records:
            try:
                svc.index_record(record)
                MedicalRecord.objects.filter(pk=record.pk).update(
                    indexed_at=timezone.now())
                indexed += 1
            except Exception as exc:
                failed += 1
                self.stderr.write(f'  FAILED {record.pk}: {exc}')

        self.stdout.write(self.style.SUCCESS(f'Indexed: {indexed}'))
        if failed:
            self.stdout.write(self.style.ERROR(
                f'Failed: {failed} — these remain unsearchable and will be '
                f'retried on the next run.'))
