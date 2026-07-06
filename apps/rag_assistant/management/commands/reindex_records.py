# rag_assistant/management/commands/reindex_records.py
"""
Management command: re-index all (or a specific patient's) medical records.

Usage:
    python manage.py reindex_records                    # all patients
    python manage.py reindex_records --patient <id>     # specific patient
    python manage.py reindex_records --clear            # delete existing index first
"""
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'Re-index medical records into the RAG vector store'

    def add_arguments(self, parser):
        parser.add_argument(
            '--patient',
            type=str,
            default=None,
            help='Patient user ID (pk) to re-index (default: all patients)',
        )
        parser.add_argument(
            '--clear',
            action='store_true',
            default=False,
            help='Delete existing MedicalDocument + MedicalChunk rows before re-indexing',
        )

    def handle(self, *args, **options):
        from django.conf import settings
        from django.contrib.auth import get_user_model

        from apps.medical_records.models import MedicalRecord
        from apps.rag_assistant.models   import MedicalDocument, MedicalChunk
        from apps.rag_assistant.services.rag_service import RAGService

        User    = get_user_model()
        svc     = RAGService()
        patient_id = options['patient']

        if patient_id:
            try:
                patients = [User.objects.get(pk=patient_id)]
            except User.DoesNotExist:
                raise CommandError(f'No user with pk={patient_id}')
        else:
            patients = list(User.objects.all())

        for patient in patients:
            if options['clear']:
                deleted, _ = MedicalDocument.objects.filter(patient=patient).delete()
                self.stdout.write(f'  Cleared {deleted} document(s) for {patient}')

            records = MedicalRecord.objects.filter(patient=patient)
            if not records.exists():
                self.stdout.write(f'  {patient}: no records — skipping')
                continue

            self.stdout.write(f'Indexing {records.count()} record(s) for {patient}…')
            total = 0
            for rec in records:
                n = svc.index_record(rec)
                total += n
                self.stdout.write(f'  [{rec.record_type}] {rec.title} → {n} chunk(s)')

            status = svc.index_status(patient)
            self.stdout.write(self.style.SUCCESS(
                f'Done: {total} chunks created '
                f'({status["embedded_chunks"]}/{status["total_chunks"]} embedded, '
                f'{status["coverage_pct"]}% coverage)'
            ))
