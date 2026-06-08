"""
Migration: escalation system — adds new fields to CareCircle, CareGiver,
DailyCheckIn and creates the EscalationLog model.

The CareCircle.quick_token field is unique, so we use a two-step approach:
  1. Add as nullable (no unique constraint)
  2. RunPython to populate UUIDs for existing rows
  3. AlterField → not-null + unique
"""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _populate_quick_tokens(apps, schema_editor):
    CareCircle = apps.get_model('care', 'CareCircle')
    for circle in CareCircle.objects.filter(quick_token__isnull=True):
        circle.quick_token = uuid.uuid4()
        circle.save(update_fields=['quick_token'])


class Migration(migrations.Migration):

    dependencies = [
        ('care', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ── CareCircle new fields ─────────────────────────────────────────────
        migrations.AddField(
            model_name='carecircle',
            name='usual_checkin_hour',
            field=models.PositiveSmallIntegerField(
                default=9,
                help_text='Hour of day (0–23, server timezone) when patient usually checks in',
            ),
        ),
        migrations.AddField(
            model_name='carecircle',
            name='checkin_window_hours',
            field=models.PositiveSmallIntegerField(
                default=2,
                help_text='Grace period in hours after usual check-in time before first alert',
            ),
        ),
        migrations.AddField(
            model_name='carecircle',
            name='daily_summary_enabled',
            field=models.BooleanField(
                default=True,
                help_text='Send a daily summary email to all caregivers at 8 pm',
            ),
        ),
        migrations.AddField(
            model_name='carecircle',
            name='last_active_at',
            field=models.DateTimeField(
                blank=True, null=True,
                help_text='Last time the patient visited any Care page (passive signal)',
            ),
        ),
        # Step 1: add quick_token as nullable
        migrations.AddField(
            model_name='carecircle',
            name='quick_token',
            field=models.UUIDField(null=True, blank=True),
        ),
        # Step 2: populate unique UUIDs for all existing rows
        migrations.RunPython(_populate_quick_tokens, migrations.RunPython.noop),
        # Step 3: make non-nullable + unique
        migrations.AlterField(
            model_name='carecircle',
            name='quick_token',
            field=models.UUIDField(
                default=uuid.uuid4, unique=True, editable=False,
                help_text='Token for no-login one-tap "I am fine" check-in link',
            ),
        ),

        # ── CareGiver new fields ──────────────────────────────────────────────
        migrations.AddField(
            model_name='caregiver',
            name='phone_number',
            field=models.CharField(
                blank=True, max_length=30,
                help_text='Include country code, e.g. +358501234567',
            ),
        ),
        migrations.AddField(
            model_name='caregiver',
            name='notify_email',
            field=models.BooleanField(
                default=True,
                help_text='Send email alerts for missed check-ins (Tier 2+)',
            ),
        ),
        migrations.AddField(
            model_name='caregiver',
            name='notify_sms',
            field=models.BooleanField(
                default=False,
                help_text='Send SMS alerts (requires phone_number and Twilio config)',
            ),
        ),
        migrations.AddField(
            model_name='caregiver',
            name='notify_priority',
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text='Lower = notified first when multiple caregivers exist',
            ),
        ),
        migrations.AddField(
            model_name='caregiver',
            name='quiet_start',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True,
                help_text='Quiet hours start (0–23). Leave blank to disable.',
            ),
        ),
        migrations.AddField(
            model_name='caregiver',
            name='quiet_end',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True,
                help_text='Quiet hours end (0–23). Wraps midnight correctly.',
            ),
        ),
        migrations.AlterModelOptions(
            name='caregiver',
            options={'ordering': ['notify_priority', 'name']},
        ),

        # ── DailyCheckIn new field ────────────────────────────────────────────
        migrations.AddField(
            model_name='dailycheckin',
            name='is_quick',
            field=models.BooleanField(
                default=False,
                help_text='Quick "I am fine" tap — no detailed scores',
            ),
        ),

        # ── EscalationLog new model ───────────────────────────────────────────
        migrations.CreateModel(
            name='EscalationLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('tier', models.PositiveSmallIntegerField(help_text='Escalation tier (2–4)')),
                ('channel', models.CharField(
                    choices=[
                        ('email',         'Email alert'),
                        ('sms',           'SMS alert'),
                        ('summary_email', 'Daily summary email'),
                    ],
                    max_length=20,
                )),
                ('date', models.DateField(
                    help_text='Which check-in date this alert is for')),
                ('sent_at', models.DateTimeField(auto_now_add=True)),
                ('acknowledged_at', models.DateTimeField(blank=True, null=True)),
                ('acknowledged_by', models.CharField(blank=True, max_length=150)),
                ('acknowledge_token', models.UUIDField(
                    default=uuid.uuid4, unique=True,
                    help_text='Used in one-click email acknowledge links',
                )),
                ('is_dismissed', models.BooleanField(default=False)),
                ('patient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='escalation_logs',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('caregiver', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='escalation_logs',
                    to='care.caregiver',
                )),
            ],
            options={
                'ordering': ['-sent_at'],
            },
        ),
        migrations.AddIndex(
            model_name='escalationlog',
            index=models.Index(fields=['patient', 'date'], name='care_escala_patient_date_idx'),
        ),
        migrations.AddIndex(
            model_name='escalationlog',
            index=models.Index(fields=['acknowledge_token'], name='care_escala_ack_token_idx'),
        ),
    ]
