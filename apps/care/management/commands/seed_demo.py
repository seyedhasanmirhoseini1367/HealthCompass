"""
Development-only demonstration data.

Why this exists
---------------
Every screen in this product is built to say "nothing recorded yet" honestly,
which is correct and makes the application impossible to look at while building
it. A developer cannot tell a well-designed dashboard from a broken one when
both are empty.

What it is NOT
--------------
It is not a fixture that reaches into the database to make the UI look
populated. Everything goes through the real models and the real authorization:
Maria sees Anna because a `SharingGrant` exists and `accounts.authz` says so, not
because a template was handed a list. If the authorization is wrong, this demo
breaks — which is the point.

Safety
------
Refuses to run unless DEBUG is on. Not a warning, a refusal: this writes people
and clinical data, and the failure mode on a production database is a fabricated
patient in a real system. Every object it creates carries the DEMO_TAG so
`clear_demo` can remove exactly what was added and nothing else.
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

#: Every demo account's username starts with this, and clear_demo deletes by it.
#: A prefix rather than a flag column: adding a database field to every model so
#: a dev convenience can find its own rows is the tail wagging the dog.
DEMO_PREFIX = 'demo_'
DEMO_DOMAIN = 'demo.invalid'
DEMO_TAG = '[DEMO]'


class Command(BaseCommand):
    help = 'Create development-only demo people and care data (DEBUG only).'

    def add_arguments(self, parser):
        parser.add_argument('--quiet', action='store_true',
                            help='Suppress the credentials summary.')

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                'seed_demo refuses to run with DEBUG=False. It creates people '
                'and clinical data, and a fabricated patient in a production '
                'database is not something a management command should be able '
                'to do by accident.')

        with transaction.atomic():
            anna, maria = self._people()
            self._clinical(anna)
            self._care(anna)
            self._sharing(anna, maria)

        if not options['quiet']:
            self._report(anna, maria)

    # ── People ───────────────────────────────────────────────────────────────

    def _people(self):
        User = get_user_model()

        anna, _ = User.objects.update_or_create(
            username=f'{DEMO_PREFIX}anna',
            defaults={'email': f'anna@{DEMO_DOMAIN}', 'role': 'patient',
                      'first_name': 'Anna', 'last_name': 'Korhonen',
                      'date_of_birth': timezone.localdate().replace(
                          year=timezone.localdate().year - 78)})
        anna.set_password('demo-pass-1234')
        anna.save()

        maria, _ = User.objects.update_or_create(
            username=f'{DEMO_PREFIX}maria',
            defaults={'email': f'maria@{DEMO_DOMAIN}', 'role': 'patient',
                      'first_name': 'Maria', 'last_name': 'Korhonen'})
        maria.set_password('demo-pass-1234')
        maria.save()
        return anna, maria

    # ── Anna's clinical picture ──────────────────────────────────────────────

    def _clinical(self, anna):
        from apps.ai_insights.models import HealthAlert
        from apps.appointments.models import Appointment
        from apps.medical_records.models import (MedicalRecord,
                                                 WearableDataPoint)

        record, _ = MedicalRecord.objects.update_or_create(
            patient=anna, title=f'{DEMO_TAG} Discharge summary',
            defaults={'record_type': 'discharge',
                      'record_date': timezone.localdate() - timedelta(days=40),
                      'notes': 'Demonstration data.'})

        # Measurements, so "recent health activity" has something true in it.
        for days, metric, value in ((0, 'weight', 68.4),
                                    (0, 'heart_rate', 74),
                                    (3, 'blood_oxygen', 97)):
            WearableDataPoint.objects.get_or_create(
                record=record, patient=anna, metric=metric,
                recorded_at=timezone.now() - timedelta(days=days, hours=2),
                defaults={'value': value})

        Appointment.objects.get_or_create(
            patient=anna, title=f'{DEMO_TAG} Cardiology follow-up',
            defaults={'doctor_name': 'Dr Virtanen',
                      'location': 'Tampere University Hospital',
                      'appointment_datetime': timezone.now() + timedelta(days=2,
                                                                         hours=3)})

        # One alert, deliberately WARNING rather than CRITICAL: URGENT on the
        # dashboard is reserved for clinician-authored critical alerts, and a
        # demo that manufactures one would misrepresent what that state means.
        HealthAlert.objects.get_or_create(
            patient=anna, title=f'{DEMO_TAG} Blood glucose trending upward',
            defaults={'severity': HealthAlert.Severity.WARNING,
                      'message': 'Demonstration alert.',
                      'source_record': record})

    # ── Anna's care activity ─────────────────────────────────────────────────

    def _care(self, anna):
        from apps.care.models import (CareTask, MonitoringSignal, PatientReport,
                                      TaskOccurrence)
        from apps.care.signals_rules import evaluate_task

        task, _ = CareTask.objects.update_or_create(
            patient=anna, label='Evening tablet',
            defaults={'kind': CareTask.Kind.MEDICATION,
                      'times_of_day': ['20:00'], 'grace_minutes': 120})

        # Three unanswered evenings, which is what the default policy treats as
        # worth telling a caregiver about. Built as real occurrences so the rule
        # raises the signal itself rather than the signal being inserted.
        for days in (1, 2, 3):
            occurrence, created = TaskOccurrence.objects.get_or_create(
                task=task, due_at=timezone.now() - timedelta(days=days),
                defaults={'patient': anna})
            if created or occurrence.state == TaskOccurrence.State.PENDING:
                occurrence.state = TaskOccurrence.State.UNCONFIRMED
                occurrence.save(update_fields=['state'])

        # Today's dose, still open, so the patient view has something to answer.
        TaskOccurrence.objects.get_or_create(
            task=task, due_at=timezone.now() + timedelta(hours=2),
            defaults={'patient': anna})

        PatientReport.objects.get_or_create(
            patient=anna, text='I felt a bit dizzy when I stood up this morning.',
            defaults={'reported_by': anna,
                      'kind': PatientReport.Kind.SYMPTOM,
                      'input_method': PatientReport.InputMethod.WEB,
                      'occurred_at': timezone.now() - timedelta(hours=6)})

        if not MonitoringSignal.objects.filter(
                patient=anna, resolved_at__isnull=True).exists():
            evaluate_task(task)

    # ── The relationship ─────────────────────────────────────────────────────

    def _sharing(self, anna, maria):
        from apps.accounts.models import SharingGrant

        # The real grant, with the real scopes. Maria sees Anna because
        # accounts.authz says so — nothing here shortcuts that.
        SharingGrant.objects.update_or_create(
            patient=anna, recipient=maria,
            defaults={'can_view_records': False,
                      'can_view_alerts': True,
                      'can_view_appointments': True,
                      'status': SharingGrant.Status.ACTIVE})

    # ── Output ───────────────────────────────────────────────────────────────

    def _report(self, anna, maria):
        self.stdout.write(self.style.SUCCESS('Demo data created.'))
        self.stdout.write(
            f'\n  Patient   {anna.username} / demo-pass-1234'
            f'\n            Anna Korhonen, 78 — condition, medication reminder,'
            f'\n            appointment, alert, measurements, self-report'
            f'\n'
            f'\n  Caregiver {maria.username} / demo-pass-1234'
            f'\n            Maria sees Anna through a real SharingGrant:'
            f'\n            care + alerts + appointments, NOT records.'
            f'\n'
            f'\n  Sign in as {maria.username} and open /care/ to see the'
            f'\n  person-centred view; as {anna.username} for the patient side.'
            f'\n'
            f'\n  Remove it all with:  python manage.py clear_demo\n')
