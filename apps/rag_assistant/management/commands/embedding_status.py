"""
Management command: report embedding provenance across the vector store.

Read-only. Answers "does what is stored still match the model we query with?"
— the question that went unanswered when text-embedding-004 was deprecated.

Usage:
    python manage.py embedding_status
    python manage.py embedding_status --json
    python manage.py embedding_status --fail-on-stale   # exit 1 if any stale rows
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Report which embedding models produced the stored vectors, and flag stale ones.'

    def add_arguments(self, parser):
        parser.add_argument('--json', action='store_true', default=False,
                            help='Emit machine-readable JSON instead of a table.')
        parser.add_argument('--fail-on-stale', action='store_true', default=False,
                            help='Exit with status 1 when incompatible embeddings exist '
                                 '(for use in deploy checks / CI).')

    def handle(self, *args, **options):
        from apps.rag_assistant.models import MedicalChunk, GeneralKnowledgeChunk
        from apps.rag_assistant.services.embedding_service import audit_embeddings

        reports = [audit_embeddings(MedicalChunk), audit_embeddings(GeneralKnowledgeChunk)]

        if options['json']:
            self.stdout.write(json.dumps(reports, indent=2))
        else:
            for rep in reports:
                self._render(rep)

        total_stale = sum(r['stale'] for r in reports)
        if total_stale:
            self.stdout.write(self.style.WARNING(
                f'\n{total_stale} incompatible embedding(s) are being excluded from retrieval.\n'
                'Re-embed them with:  python manage.py reindex_all_embeddings --stale-only\n'
                'Or, if the vectors are correct and only the provenance is missing:\n'
                '                     python manage.py backfill_embedding_provenance'
            ))
            if options['fail_on_stale']:
                raise SystemExit(1)
        else:
            self.stdout.write(self.style.SUCCESS('\nAll stored embeddings are compatible.'))

    def _render(self, rep):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n{rep['model']}"))
        self.stdout.write(
            f"  active model : {rep['active_model']} ({rep['active_dim']}-dim)"
            f"{'  [STRICT]' if rep['strict'] else ''}"
        )
        self.stdout.write(
            f"  rows         : {rep['total']} total, {rep['unembedded']} not embedded, "
            f"{rep['compatible']} compatible, {rep['stale']} stale"
        )
        if not rep['breakdown']:
            return
        self.stdout.write('  breakdown    :')
        for row in rep['breakdown']:
            marker = ' ' if row['status'] == 'ok' else '!'
            self.stdout.write(
                f"    {marker} {row['count']:>6}  {row['embedding_model']}  "
                f"dim={row['embedding_dimensions']}  [{row['status']}]"
            )
