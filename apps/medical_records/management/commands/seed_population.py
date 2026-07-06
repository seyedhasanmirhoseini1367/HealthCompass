import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import CustomUser, PatientProfile
from apps.medical_records.models import MedicalRecord, ParsedLabValue

# ── Biomarker definitions ────────────────────────────────────────────────────
# Names and units intentionally match what real uploaded records use
# (name, unit, reference_range, mean, std, low_abnormal, high_abnormal)
BIOMARKERS = [
    ('Fasting Glucose',   'mg/dL',       '70–99',     92.0,  8.0,   70.0,  99.0),
    ('Total Cholesterol', 'mg/dL',       '< 200',    185.0, 20.0,   None, 200.0),
    ('LDL',               'mg/dL',       '< 130',    105.0, 18.0,   None, 130.0),
    ('HbA1c',             '%',           '< 5.7',      5.3,  0.35,  None,   5.7),
    ('Hemoglobin',        'g/dL',        '12.0–17.5', 14.0,  0.8,   12.0,  17.5),
    ('Creatinine',        'mg/dL',       '0.6–1.2',    0.92, 0.12,   0.6,   1.2),
    ('Triglycerides',     'mg/dL',       '< 150',    105.0, 25.0,   None, 150.0),
    ('WBC',               'x10^9/L',     '4.0–10.0',   6.2,  0.9,   4.0,  10.0),
    ('ALT (Liver)',        'U/L',         '7–40',      22.0,  6.0,   7.0,  40.0),
    ('Sodium',            'mmol/L',      '136–145',  140.0,  2.5,  136.0, 145.0),
]

FIRST_NAMES = [
    'Mikael','Juhani','Olavi','Eino','Tapio','Veikko','Erkki','Kalevi','Seppo',
    'Matti','Anna','Liisa','Maria','Helena','Aino','Kaarina','Riitta','Tuulikki',
    'Sirkka','Maija','Pekka','Timo','Juha','Jari','Markku','Heikki','Antti',
    'Kari','Petri','Sami','Erika','Saara','Hanna','Laura','Tiina','Päivi','Merja',
    'Pirjo','Leena','Tuula','Aleksi','Ville','Joona','Oskari','Emmi','Siiri',
    'Aada','Anni','Elina','Nora',
]
LAST_NAMES = [
    'Korhonen','Virtanen','Mäkinen','Nieminen','Mäkelä','Hämäläinen','Leinonen',
    'Heikkinen','Koskinen','Järvinen','Lehtinen','Lehtonen','Saarinen','Salonen',
    'Turunen','Niskanen','Väisänen','Miettinen','Laitinen','Helin','Huttunen',
    'Lahtinen','Pitkänen','Leppänen','Kärkkäinen','Hiltunen','Keränen','Repo',
    'Rantanen','Ojala',
]


def _gauss_clamp(mean, std, low, high):
    for _ in range(10):
        v = random.gauss(mean, std)
        if low <= v <= high:
            return round(v, 2)
    return round(max(low, min(high, mean)), 2)


def _make_value(biomarker, year_offset):
    name, unit, ref, mean, std, lo, hi = biomarker
    # slight natural drift over time (aging / lifestyle)
    drift = year_offset * random.uniform(-0.01, 0.02) * mean
    adj_mean = mean + drift
    lo_eff = lo if lo is not None else adj_mean - 4 * std
    hi_eff = hi if hi is not None else adj_mean + 4 * std
    val = _gauss_clamp(adj_mean, std, lo_eff - std, hi_eff + std)

    is_abnormal = False
    if lo is not None and val < lo:
        is_abnormal = True
    if hi is not None and val > hi:
        is_abnormal = True

    return str(val), unit, ref, is_abnormal


class Command(BaseCommand):
    help = 'Seed ~75 synthetic patient users with quarterly lab results (2020–2025)'

    def add_arguments(self, parser):
        parser.add_argument('--count', type=int, default=75)
        parser.add_argument('--clear', action='store_true',
                            help='Delete all seed_ users before seeding')

    def handle(self, *args, **options):
        count = options['count']

        if options['clear']:
            deleted, _ = CustomUser.objects.filter(
                username__startswith='seed_').delete()
            self.stdout.write(self.style.WARNING(
                f'Cleared {deleted} existing seed users.'))

        # quarters: Jan, Apr, Jul, Oct  ×  2020–2025  = 24 dates
        quarters = []
        for yr in range(2020, 2026):
            for month in (1, 4, 7, 10):
                quarters.append(date(yr, month, 15))

        created_users = 0
        total_records = 0
        total_values  = 0

        with transaction.atomic():
            for i in range(1, count + 1):
                username = f'seed_{i:03d}'
                if CustomUser.objects.filter(username=username).exists():
                    continue

                first = random.choice(FIRST_NAMES)
                last  = random.choice(LAST_NAMES)
                dob   = date(
                    random.randint(1950, 1995),
                    random.randint(1, 12),
                    random.randint(1, 28),
                )
                user = CustomUser.objects.create_user(
                    username=username,
                    email=f'{username}@example.com',
                    password='seed_pass_123',
                    first_name=first,
                    last_name=last,
                    role=CustomUser.Role.PATIENT,
                    date_of_birth=dob,
                    is_approved=True,
                )
                PatientProfile.objects.create(
                    user=user,
                    blood_type=random.choice(
                        ['A+','A-','B+','B-','AB+','AB-','O+','O-']),
                )
                created_users += 1

                # Build records + values in bulk
                records_to_create = []
                for q in quarters:
                    records_to_create.append(MedicalRecord(
                        patient=user,
                        record_type=MedicalRecord.RecordType.LAB_RESULT,
                        source=MedicalRecord.Source.KANTA_XML,
                        title=f'Lab Results {q.strftime("%b %Y")}',
                        record_date=q,
                    ))
                records = MedicalRecord.objects.bulk_create(records_to_create)
                total_records += len(records)

                values_to_create = []
                for rec, q in zip(records, quarters):
                    year_offset = q.year - 2020
                    for bio in BIOMARKERS:
                        val, unit, ref, is_abnormal = _make_value(bio, year_offset)
                        values_to_create.append(ParsedLabValue(
                            record=rec,
                            parameter_name=bio[0],
                            value=val,
                            unit=unit,
                            reference_range=ref,
                            is_abnormal=is_abnormal,
                        ))
                ParsedLabValue.objects.bulk_create(values_to_create)
                total_values += len(values_to_create)

                if i % 10 == 0:
                    self.stdout.write(f'  ... {i}/{count} users done')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {created_users} users · {total_records} records · '
            f'{total_values} lab values created.'
        ))
