import datetime
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('care', '0002_escalation_system'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='PainLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=datetime.date.today)),
                ('body_area', models.CharField(
                    max_length=20,
                    choices=[
                        ('head',       'Head'),
                        ('neck',       'Neck / Throat'),
                        ('chest',      'Chest'),
                        ('back_upper', 'Upper Back'),
                        ('back_lower', 'Lower Back'),
                        ('abdomen',    'Abdomen'),
                        ('arm_left',   'Left Arm / Shoulder'),
                        ('arm_right',  'Right Arm / Shoulder'),
                        ('leg_left',   'Left Leg / Hip'),
                        ('leg_right',  'Right Leg / Hip'),
                        ('other',      'Other'),
                    ],
                )),
                ('pain_type', models.CharField(
                    blank=True, max_length=15,
                    choices=[
                        ('sharp',     'Sharp'),
                        ('dull',      'Dull / Aching'),
                        ('burning',   'Burning'),
                        ('throbbing', 'Throbbing'),
                        ('stabbing',  'Stabbing'),
                        ('cramping',  'Cramping'),
                        ('pressure',  'Pressure / Tightness'),
                        ('other',     'Other'),
                    ],
                )),
                ('severity', models.PositiveSmallIntegerField(
                    help_text='1 (mild) to 10 (severe)')),
                ('trigger', models.CharField(blank=True, max_length=200,
                    help_text='What triggered or worsened the pain?')),
                ('notes', models.CharField(blank=True, max_length=400)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('patient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='pain_logs',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-date', '-created_at']},
        ),
    ]
