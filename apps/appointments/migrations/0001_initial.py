import uuid
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
            name='Appointment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('title', models.CharField(max_length=255)),
                ('doctor_name', models.CharField(blank=True, max_length=255)),
                ('location', models.CharField(blank=True, max_length=500)),
                ('appointment_datetime', models.DateTimeField()),
                ('notes', models.TextField(blank=True)),
                ('remind_24h', models.BooleanField(default=True)),
                ('remind_3h', models.BooleanField(default=False)),
                ('remind_2h', models.BooleanField(default=False)),
                ('remind_1h', models.BooleanField(default=True)),
                ('reminded_24h', models.BooleanField(default=False)),
                ('reminded_3h', models.BooleanField(default=False)),
                ('reminded_2h', models.BooleanField(default=False)),
                ('reminded_1h', models.BooleanField(default=False)),
                ('is_completed', models.BooleanField(default=False)),
                ('is_cancelled', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('patient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='appointments',
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                'ordering': ['appointment_datetime'],
            },
        ),
    ]
