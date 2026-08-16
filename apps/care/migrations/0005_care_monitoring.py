"""
Creates the care monitoring tables.

Numbered 0005, not 0001, and that is deliberate.

A different `care` app lived in this project until July 2026 — care circles,
caregivers, pain logs — and although its code is gone from every branch, the
production database still records its migrations 0001-0004 as applied. Django
identifies a migration by (app_label, name), so a new `care/0001_initial.py`
looks *already applied* to that database: the tables are never created, and the
next migration runs against nothing. That is exactly how the 2026-08-16 deploy
failed, with `relation "care_caretask" does not exist`.

Starting at 0005 sidesteps the collision without touching the old app's rows or
its tables, which are left exactly as they are — they may hold real patient
check-ins and pain logs, and deleting them to tidy up migration history is not
a trade this project makes.
"""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('medical_records', '0010_condition_description_optional'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CareTask',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('medication', 'Medication'), ('measurement', 'Measurement'), ('activity', 'Activity'), ('other', 'Other')], default='medication', max_length=16)),
                ('label', models.CharField(max_length=120)),
                ('times_of_day', models.JSONField(default=list, help_text='List of "HH:MM" local times this is due, e.g. ["08:00", "20:00"].')),
                ('grace_minutes', models.PositiveIntegerField(default=180, help_text='Minutes after the due time before an unanswered occurrence is recorded as unconfirmed (NOT as missed).')),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('medication_statement', models.ForeignKey(blank=True, help_text='Advisory link to the document that mentioned this medication. Never used to derive the schedule.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='care_tasks', to='medical_records.medicationstatement')),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='care_tasks', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['label'],
            },
        ),
        migrations.CreateModel(
            name='PatientReport',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('reported_by_role', models.CharField(choices=[('patient', 'The patient'), ('caregiver', 'A caregiver')], default='patient', max_length=16)),
                ('kind', models.CharField(choices=[('symptom', 'Symptom'), ('wellbeing', 'General wellbeing'), ('observation', 'Other observation')], default='symptom', max_length=16)),
                ('input_method', models.CharField(choices=[('web', 'Typed in the app'), ('voice', 'Spoken'), ('phone', 'Telephone'), ('api', 'Mobile app')], default='web', max_length=8)),
                ('text', models.TextField(help_text="The report in the reporter's own words. Not normalised.")),
                ('transcript', models.TextField(blank=True, default='', help_text='Raw speech-to-text output, when the report arrived by voice.')),
                ('transcript_confidence', models.FloatField(blank=True, help_text='Recogniser confidence, when the provider reports one. NULL means unknown — never assume high confidence.', null=True)),
                ('occurred_at', models.DateTimeField(blank=True, help_text='When the reporter says it happened. NULL when they did not say.', null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='care_reports', to=settings.AUTH_USER_MODEL)),
                ('reported_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='care_reports_made', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='TaskOccurrence',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('due_at', models.DateTimeField()),
                ('state', models.CharField(choices=[('pending', 'Waiting'), ('confirmed', 'Confirmed done'), ('skipped', 'Deliberately skipped'), ('missed', 'Reported as missed'), ('unconfirmed', 'No response — unknown')], default='pending', max_length=16)),
                ('responded_at', models.DateTimeField(blank=True, null=True)),
                ('response_input', models.CharField(blank=True, choices=[('web', 'Typed in the app'), ('voice', 'Spoken'), ('phone', 'Telephone'), ('api', 'Mobile app')], default='', max_length=8)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='care_occurrences', to=settings.AUTH_USER_MODEL)),
                ('responded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='care_responses', to=settings.AUTH_USER_MODEL)),
                ('task', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='occurrences', to='care.caretask')),
            ],
            options={
                'ordering': ['-due_at'],
            },
        ),
        migrations.CreateModel(
            name='MonitoringSignal',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('kind', models.CharField(choices=[('repeated_unconfirmed', 'Repeated unconfirmed tasks'), ('reported_symptom', 'Patient reported a symptom'), ('reported_missed', 'Patient reported missing a task')], max_length=32)),
                ('severity', models.CharField(choices=[('info', 'For information'), ('attention', 'Worth a look'), ('urgent', 'Needs attention now')], default='attention', max_length=16)),
                ('window_start', models.DateTimeField()),
                ('window_end', models.DateTimeField()),
                ('subject_key', models.CharField(blank=True, default='', help_text='Stable key for the thing this is about, used to avoid raising the same signal twice while it is still true.', max_length=200)),
                ('rule', models.CharField(blank=True, default='', max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
                ('patient', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='care_signals', to=settings.AUTH_USER_MODEL)),
                ('reports', models.ManyToManyField(blank=True, related_name='signals', to='care.patientreport')),
                ('occurrences', models.ManyToManyField(blank=True, related_name='signals', to='care.taskoccurrence')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='caretask',
            index=models.Index(fields=['patient', 'is_active'], name='care_careta_patient_895405_idx'),
        ),
        migrations.AddIndex(
            model_name='patientreport',
            index=models.Index(fields=['patient', '-created_at'], name='care_patien_patient_7adef9_idx'),
        ),
        migrations.AddIndex(
            model_name='taskoccurrence',
            index=models.Index(fields=['patient', '-due_at'], name='care_taskoc_patient_5cfe3c_idx'),
        ),
        migrations.AddIndex(
            model_name='taskoccurrence',
            index=models.Index(fields=['state', 'due_at'], name='care_taskoc_state_9c01b6_idx'),
        ),
        migrations.AddConstraint(
            model_name='taskoccurrence',
            constraint=models.UniqueConstraint(fields=('task', 'due_at'), name='unique_occurrence_per_task_time'),
        ),
        migrations.AddIndex(
            model_name='monitoringsignal',
            index=models.Index(fields=['patient', '-created_at'], name='care_monito_patient_703cf5_idx'),
        ),
        migrations.AddIndex(
            model_name='monitoringsignal',
            index=models.Index(fields=['patient', 'kind', 'subject_key', 'resolved_at'], name='care_monito_patient_ba23fe_idx'),
        ),
    ]
