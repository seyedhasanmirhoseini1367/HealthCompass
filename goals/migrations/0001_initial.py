import datetime
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='HealthGoal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('goal_type', models.CharField(
                    choices=[
                        ('weight', 'Body Weight (kg)'), ('bp', 'Blood Pressure (mmHg)'),
                        ('steps', 'Daily Steps'), ('sleep', 'Sleep (hours/night)'),
                        ('pain', 'Pain Level (0–10)'), ('feeling', 'Overall Feeling (1–10)'),
                        ('medication', 'Medication Adherence (%)'), ('custom', 'Custom'),
                    ],
                    default='custom', max_length=15,
                )),
                ('description', models.TextField(blank=True)),
                ('target_value', models.FloatField(
                    help_text='Numeric target (e.g. 75 for 75 kg)')),
                ('unit', models.CharField(blank=True, max_length=30,
                    help_text='e.g. kg, steps, hours')),
                ('start_value', models.FloatField(blank=True, null=True,
                    help_text='Starting value when goal was created')),
                ('start_date', models.DateField(default=datetime.date.today)),
                ('target_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(
                    choices=[
                        ('active', 'Active'), ('achieved', 'Achieved'),
                        ('paused', 'Paused'), ('abandoned', 'Abandoned'),
                    ],
                    default='active', max_length=15,
                )),
                ('lower_is_better', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('patient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='health_goals',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='GoalEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField(default=datetime.date.today)),
                ('value', models.FloatField()),
                ('note', models.CharField(blank=True, max_length=300)),
                ('logged_at', models.DateTimeField(auto_now_add=True)),
                ('goal', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='entries',
                    to='goals.healthgoal',
                )),
            ],
            options={'ordering': ['date'], 'unique_together': {('goal', 'date')}},
        ),
    ]
