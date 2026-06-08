import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('treatments', '__first__'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Appointment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('appointment_type', models.CharField(
                    choices=[
                        ('general', 'General / GP'), ('specialist', 'Specialist'),
                        ('follow_up', 'Follow-up'), ('lab', 'Lab / Blood test'),
                        ('imaging', 'Imaging / Scan'), ('dental', 'Dental'),
                        ('mental', 'Mental health'), ('physio', 'Physiotherapy'),
                        ('other', 'Other'),
                    ],
                    default='general', max_length=20,
                )),
                ('doctor_name', models.CharField(blank=True, max_length=150)),
                ('specialty', models.CharField(blank=True, max_length=100)),
                ('location', models.CharField(blank=True, max_length=300,
                    help_text='Hospital, clinic name or address')),
                ('date', models.DateField()),
                ('time', models.TimeField(blank=True, null=True)),
                ('duration_minutes', models.PositiveSmallIntegerField(default=30)),
                ('status', models.CharField(
                    choices=[
                        ('scheduled', 'Scheduled'), ('completed', 'Completed'),
                        ('cancelled', 'Cancelled'), ('rescheduled', 'Rescheduled'),
                    ],
                    default='scheduled', max_length=15,
                )),
                ('notes', models.TextField(blank=True,
                    help_text='Preparation notes, questions for doctor, etc.')),
                ('outcome', models.TextField(blank=True,
                    help_text='What was decided / next steps after the appointment')),
                ('reminder_sent', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('patient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='appointments',
                    to=settings.AUTH_USER_MODEL,
                )),
                ('linked_course', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='appointments',
                    to='treatments.treatmentcourse',
                )),
            ],
            options={'ordering': ['date', 'time']},
        ),
    ]
