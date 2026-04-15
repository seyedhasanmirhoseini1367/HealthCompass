# rag_assistant/management/commands/seed_trajectory_patient.py
"""
Management command: seed_trajectory_patient

Creates a fully-featured synthetic patient ("Sara M.") whose medical history
demonstrates the temporal-blindness gap in standard RAG systems and the
improvement provided by the TrajectoryService.

Sara's story (kidney disease + diabetes progression over 12 months):
  Jan 2025 — Annual check-up, all normal
  Mar 2025 — Mild creatinine rise, HbA1c enters pre-diabetic range
  Jun 2025 — Creatinine abnormal, metformin dose increased
  Sep 2025 — eGFR drops to stage-3a CKD, nephrology referral
  Dec 2025 — CKD stage 3b confirmed, switched to insulin

Usage:
    python manage.py seed_trajectory_patient
    python manage.py seed_trajectory_patient --clear   # delete and re-create
    python manage.py seed_trajectory_patient --no-index  # skip RAG indexing
    python manage.py seed_trajectory_patient --cold-start  # also create new.patient (no records)
"""
import logging
from datetime import date

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)
User = get_user_model()

# ── Patient profile ────────────────────────────────────────────────────────────

SARA_USERNAME  = 'sara.m'
SARA_EMAIL     = 'sara.m@healthcompass.dev'
SARA_PASSWORD  = 'SaraM@2025!'
SARA_FIRSTNAME = 'Sara'
SARA_LASTNAME  = 'Mirtaheri'

COLD_USERNAME  = 'new.patient'
COLD_EMAIL     = 'new.patient@healthcompass.dev'
COLD_PASSWORD  = 'NewPatient@2025!'


# ── Record data definitions ────────────────────────────────────────────────────

RECORDS = [
    # ── Visit 1: January 2025 ── All normal baseline ──────────────────────────
    {
        'record_type':  'lab_result',
        'title':        'Annual Check-Up Lab Results',
        'record_date':  date(2025, 1, 15),
        'notes':        'Routine annual check-up. Patient reports no symptoms.',
        'raw_text':     (
            'Annual check-up laboratory panel for Sara Mirtaheri. '
            'All values within normal reference ranges. '
            'Continue current healthy lifestyle. No follow-up required.'
        ),
        'lab_values': [
            {'parameter_name': 'Creatinine',       'value': '0.9',  'unit': 'mg/dL',     'reference_range': '0.6-1.1',   'is_abnormal': False, 'is_critical': False},
            {'parameter_name': 'HbA1c',             'value': '5.4',  'unit': '%',          'reference_range': '<5.7%',     'is_abnormal': False, 'is_critical': False},
            {'parameter_name': 'Fasting Glucose',   'value': '92',   'unit': 'mg/dL',     'reference_range': '70-100',    'is_abnormal': False, 'is_critical': False},
            {'parameter_name': 'eGFR',              'value': '88',   'unit': 'mL/min/1.73m²', 'reference_range': '>60',  'is_abnormal': False, 'is_critical': False},
            {'parameter_name': 'Total Cholesterol', 'value': '185',  'unit': 'mg/dL',     'reference_range': '<200',      'is_abnormal': False, 'is_critical': False},
            {'parameter_name': 'LDL',               'value': '110',  'unit': 'mg/dL',     'reference_range': '<130',      'is_abnormal': False, 'is_critical': False},
        ],
    },
    # ── Visit 2: March 2025 ── Mild rise, enters pre-diabetic range ───────────
    {
        'record_type':  'lab_result',
        'title':        'Follow-Up Lab Panel',
        'record_date':  date(2025, 3, 20),
        'notes':        (
            'Creatinine slightly elevated above previous baseline. '
            'HbA1c now in pre-diabetic range (5.7-6.4%). '
            'Advised lifestyle modifications and dietary review. '
            'Follow-up in 3 months.'
        ),
        'raw_text':     (
            'Follow-up laboratory results for Sara Mirtaheri. '
            'Creatinine has risen from 0.9 to 1.1 mg/dL since January 2025. '
            'While still within the normal reference range, this upward trend '
            'warrants monitoring. HbA1c at 5.8% indicates pre-diabetic glucose '
            'regulation. Dietary counselling recommended.'
        ),
        'lab_values': [
            {'parameter_name': 'Creatinine',       'value': '1.1',  'unit': 'mg/dL',     'reference_range': '0.6-1.1',   'is_abnormal': False, 'is_critical': False},
            {'parameter_name': 'HbA1c',             'value': '5.8',  'unit': '%',          'reference_range': '<5.7%',     'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'Fasting Glucose',   'value': '105',  'unit': 'mg/dL',     'reference_range': '70-100',    'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'eGFR',              'value': '80',   'unit': 'mL/min/1.73m²', 'reference_range': '>60',  'is_abnormal': False, 'is_critical': False},
        ],
    },
    # ── Visit 3: June 2025 ── Abnormal creatinine, medication increased ────────
    {
        'record_type':  'lab_result',
        'title':        'Mid-Year Metabolic Panel',
        'record_date':  date(2025, 6, 10),
        'notes':        (
            'Creatinine now above normal range at 1.4 mg/dL. '
            'Renal function declining — eGFR dropped to 62. '
            'HbA1c worsening at 6.2%. Metformin dose increased to 1000mg twice daily. '
            'Strict low-protein, low-sodium diet advised. '
            'Urgent repeat labs in 3 months.'
        ),
        'raw_text':     (
            'Mid-year metabolic panel for Sara Mirtaheri. '
            'Creatinine has increased to 1.4 mg/dL, now above the normal upper limit. '
            'This represents a 56% increase from the January 2025 baseline of 0.9 mg/dL. '
            'eGFR has declined to 62 mL/min/1.73m², approaching CKD Stage 2 threshold. '
            'HbA1c at 6.2% confirms continued progression toward diabetic range. '
            'Metformin dose adjusted upward to manage glycaemic control.'
        ),
        'lab_values': [
            {'parameter_name': 'Creatinine',       'value': '1.4',  'unit': 'mg/dL',     'reference_range': '0.6-1.1',   'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'HbA1c',             'value': '6.2',  'unit': '%',          'reference_range': '<5.7%',     'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'Fasting Glucose',   'value': '118',  'unit': 'mg/dL',     'reference_range': '70-100',    'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'eGFR',              'value': '62',   'unit': 'mL/min/1.73m²', 'reference_range': '>60',  'is_abnormal': False, 'is_critical': False},
        ],
    },
    # ── Prescription: June 2025 ── Metformin dose increase ────────────────────
    {
        'record_type':  'prescription',
        'title':        'Metformin Dose Adjustment',
        'record_date':  date(2025, 6, 10),
        'notes':        'Dose increased due to inadequate glycaemic control. Monitor renal function closely given rising creatinine.',
        'raw_text':     (
            'Prescription update for Sara Mirtaheri — June 2025. '
            'Metformin (Glucophage) dose increased from 500mg once daily to 1000mg twice daily. '
            'Indication: Type 2 diabetes mellitus (pre-diabetic trajectory confirmed). '
            'Caution: Patient has mildly declining renal function (creatinine 1.4 mg/dL, eGFR 62). '
            'Monitor creatinine and eGFR at each follow-up. '
            'Discontinue if eGFR falls below 30 mL/min/1.73m².'
        ),
        'lab_values': [],
    },
    # ── Visit 4: September 2025 ── CKD stage 3a, nephrology referral ──────────
    {
        'record_type':  'lab_result',
        'title':        'Renal Function Review',
        'record_date':  date(2025, 9, 5),
        'notes':        (
            'Creatinine now 1.7 mg/dL — significant deterioration. '
            'eGFR has fallen to 48 mL/min/1.73m², placing patient in CKD Stage 3a. '
            'HbA1c 6.8% — diabetes worsening despite metformin. '
            'Urgent referral to nephrology placed. '
            'Metformin to be reviewed given declining eGFR.'
        ),
        'raw_text':     (
            'Renal function review for Sara Mirtaheri — September 2025. '
            'Creatinine has risen to 1.7 mg/dL (baseline January 2025: 0.9 mg/dL; increase of +89%). '
            'eGFR: 48 mL/min/1.73m² — this meets CKD Stage 3a criteria (eGFR 45-59). '
            'HbA1c 6.8% indicates Type 2 diabetes is now established. '
            'Trajectory shows consistent 0.2-0.3 mg/dL increase in creatinine every 3 months '
            'since January 2025. Nephrologist referral initiated for further evaluation. '
            'Patient counselled regarding CKD implications and kidney-protective diet.'
        ),
        'lab_values': [
            {'parameter_name': 'Creatinine',       'value': '1.7',  'unit': 'mg/dL',     'reference_range': '0.6-1.1',   'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'HbA1c',             'value': '6.8',  'unit': '%',          'reference_range': '<5.7%',     'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'Fasting Glucose',   'value': '134',  'unit': 'mg/dL',     'reference_range': '70-100',    'is_abnormal': True,  'is_critical': False},
            {'parameter_name': 'eGFR',              'value': '48',   'unit': 'mL/min/1.73m²', 'reference_range': '>60',  'is_abnormal': True,  'is_critical': False},
        ],
    },
    # ── Diagnosis note: September 2025 ── Nephrology referral ─────────────────
    {
        'record_type':  'diagnosis',
        'title':        'Nephrology Referral — CKD Stage 3a',
        'record_date':  date(2025, 9, 5),
        'notes':        'Patient referred to Dr. Elena Sousa (Nephrology) for ongoing CKD management.',
        'raw_text':     (
            'Diagnosis and referral note — Sara Mirtaheri, September 2025. '
            'Diagnosis: Chronic Kidney Disease (CKD) Stage 3a (eGFR 45-59 mL/min/1.73m²). '
            'Comorbidity: Type 2 Diabetes Mellitus (poorly controlled, HbA1c 6.8%). '
            'The patient has demonstrated a consistent upward trajectory in serum creatinine '
            'from 0.9 mg/dL (January 2025) to 1.7 mg/dL (September 2025), a +89% increase '
            'over 8 months. '
            'The associated decline in eGFR from 88 to 48 mL/min/1.73m² confirms progressive '
            'glomerular dysfunction. '
            'Referral placed to nephrologist for: assessment of CKD aetiology, optimisation of '
            'blood pressure control (target <130/80), review of metformin safety, and consideration '
            'of SGLT-2 inhibitor for renoprotective benefit.'
        ),
        'lab_values': [],
    },
    # ── Visit 5: December 2025 ── CKD stage 3b confirmed, insulin started ─────
    {
        'record_type':  'lab_result',
        'title':        'Q4 Metabolic and Renal Panel',
        'record_date':  date(2025, 12, 3),
        'notes':        (
            'Creatinine 2.1 mg/dL — CRITICAL. eGFR 35 mL/min/1.73m²: CKD Stage 3b. '
            'HbA1c 7.2% confirms established diabetes. '
            'Metformin discontinued due to eGFR < 45. '
            'Insulin glargine 10 units nightly commenced. '
            'Urgent nephrology follow-up scheduled.'
        ),
        'raw_text':     (
            'Q4 laboratory panel for Sara Mirtaheri — December 2025. '
            'Creatinine: 2.1 mg/dL — marked elevation (reference 0.6-1.1 mg/dL). '
            'This represents a 133% increase from the January 2025 baseline of 0.9 mg/dL '
            'over 11 months. '
            'eGFR: 35 mL/min/1.73m² — patient now meets CKD Stage 3b criteria (eGFR 30-44). '
            'HbA1c: 7.2% — Type 2 diabetes mellitus, inadequately controlled. '
            'Fasting Glucose: 152 mg/dL — significantly elevated. '
            'Trajectory summary: creatinine 0.9 → 1.1 → 1.4 → 1.7 → 2.1 mg/dL over 11 months '
            '(rate of increase approximately 0.11 mg/dL per month). '
            'eGFR trajectory: 88 → 80 → 62 → 48 → 35 mL/min/1.73m² (declining ~5 units/month). '
            'Metformin contraindicated at this eGFR level — discontinued. '
            'Insulin glargine initiated for glycaemic control.'
        ),
        'lab_values': [
            {'parameter_name': 'Creatinine',       'value': '2.1',  'unit': 'mg/dL',        'reference_range': '0.6-1.1',   'is_abnormal': True, 'is_critical': True},
            {'parameter_name': 'HbA1c',             'value': '7.2',  'unit': '%',             'reference_range': '<5.7%',     'is_abnormal': True, 'is_critical': False},
            {'parameter_name': 'Fasting Glucose',   'value': '152',  'unit': 'mg/dL',        'reference_range': '70-100',    'is_abnormal': True, 'is_critical': False},
            {'parameter_name': 'eGFR',              'value': '35',   'unit': 'mL/min/1.73m²','reference_range': '>60',       'is_abnormal': True, 'is_critical': True},
            {'parameter_name': 'Total Cholesterol', 'value': '218',  'unit': 'mg/dL',        'reference_range': '<200',      'is_abnormal': True, 'is_critical': False},
            {'parameter_name': 'LDL',               'value': '142',  'unit': 'mg/dL',        'reference_range': '<130',      'is_abnormal': True, 'is_critical': False},
        ],
    },
    # ── Prescription: December 2025 ── Insulin started ────────────────────────
    {
        'record_type':  'prescription',
        'title':        'Insulin Glargine — New Prescription',
        'record_date':  date(2025, 12, 3),
        'notes':        'Metformin discontinued due to eGFR < 45. Insulin glargine commenced for glycaemic control.',
        'raw_text':     (
            'New prescription for Sara Mirtaheri — December 2025. '
            'DISCONTINUED: Metformin 1000mg (contraindicated, eGFR 35 mL/min/1.73m²). '
            'NEW: Insulin glargine (Lantus) 10 units subcutaneously at bedtime. '
            'Indication: Type 2 diabetes mellitus, poorly controlled (HbA1c 7.2%). '
            'Titration: Increase by 2 units every 3 days if fasting glucose remains above 130 mg/dL. '
            'Patient educated on insulin injection technique and hypoglycaemia management. '
            'Blood glucose monitoring: twice daily (fasting + bedtime).'
        ),
        'lab_values': [],
    },
    # ── Diagnosis note: December 2025 ── CKD 3b confirmed ────────────────────
    {
        'record_type':  'diagnosis',
        'title':        'CKD Stage 3b Assessment and Management Plan',
        'record_date':  date(2025, 12, 3),
        'notes':        'CKD Stage 3b confirmed by nephrology. Annual review of kidney function required.',
        'raw_text':     (
            'Nephrology follow-up assessment — Sara Mirtaheri, December 2025. '
            'Diagnosis confirmed: Chronic Kidney Disease (CKD) Stage 3b. '
            'eGFR: 35 mL/min/1.73m² (CKD Stage 3b range: 30-44). '
            'Primary aetiology: Diabetic nephropathy (Type 2 Diabetes Mellitus, HbA1c 7.2%). '
            'Longitudinal creatinine trajectory (2025): '
            '  January 2025: 0.9 mg/dL (normal) → '
            '  March 2025: 1.1 mg/dL → '
            '  June 2025: 1.4 mg/dL (abnormal) → '
            '  September 2025: 1.7 mg/dL (CKD 3a) → '
            '  December 2025: 2.1 mg/dL (CKD 3b, critical). '
            'Management plan: '
            '1. Blood pressure target < 130/80 mmHg (currently on lisinopril 10mg). '
            '2. Insulin glargine for glycaemic control, target HbA1c < 7%. '
            '3. Renal dietitian referral for low-protein diet (0.8g/kg/day). '
            '4. Avoid NSAIDs and nephrotoxic agents. '
            '5. Renal function review every 3 months. '
            '6. Refer for renal transplant assessment if eGFR falls below 20.'
        ),
        'lab_values': [],
    },
]


class Command(BaseCommand):
    help = (
        'Seed a demo patient (sara.m) with 12 months of deteriorating renal '
        'and metabolic records to demonstrate temporal RAG trajectory reasoning.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete existing sara.m patient and all associated records before seeding.',
        )
        parser.add_argument(
            '--no-index',
            action='store_true',
            dest='no_index',
            help='Skip RAG indexing after creating records (faster, for DB-only testing).',
        )
        parser.add_argument(
            '--cold-start',
            action='store_true',
            dest='cold_start',
            help='Also create a new.patient account with zero records to demo cold-start.',
        )

    def handle(self, *args, **options):
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== HealthCompass: Trajectory Demo Seed ===\n'))

        # ── Optional teardown ──────────────────────────────────────────────────
        if options['clear']:
            deleted = User.objects.filter(username=SARA_USERNAME).delete()
            self.stdout.write(f'  Cleared existing sara.m data: {deleted}')

        # ── Create / get Sara M. ───────────────────────────────────────────────
        sara, created = User.objects.get_or_create(
            username=SARA_USERNAME,
            defaults={
                'email':      SARA_EMAIL,
                'first_name': SARA_FIRSTNAME,
                'last_name':  SARA_LASTNAME,
                'role':       'patient',
                'is_approved': True,
            },
        )
        if created:
            sara.set_password(SARA_PASSWORD)
            sara.save()
            self.stdout.write(self.style.SUCCESS(f'  Created patient: {SARA_USERNAME}  (password: {SARA_PASSWORD})'))
        else:
            self.stdout.write(f'  Patient {SARA_USERNAME} already exists — adding missing records.')

        # ── Create medical records ─────────────────────────────────────────────
        from medical_records.models import MedicalRecord, ParsedLabValue

        new_records = []
        for rd in RECORDS:
            # Skip if a record with this title and date already exists
            exists = MedicalRecord.objects.filter(
                patient=sara,
                title=rd['title'],
                record_date=rd['record_date'],
            ).exists()
            if exists:
                self.stdout.write(f'  Skip (exists): {rd["title"]} — {rd["record_date"]}')
                continue

            record = MedicalRecord.objects.create(
                patient     = sara,
                record_type = rd['record_type'],
                title       = rd['title'],
                record_date = rd['record_date'],
                notes       = rd.get('notes', ''),
                raw_text    = rd.get('raw_text', ''),
                source      = 'manual_upload',
            )

            # ParsedLabValue rows
            for lv in rd.get('lab_values', []):
                ParsedLabValue.objects.create(
                    record          = record,
                    parameter_name  = lv['parameter_name'],
                    value           = lv['value'],
                    unit            = lv['unit'],
                    reference_range = lv.get('reference_range', ''),
                    is_abnormal     = lv.get('is_abnormal', False),
                    is_critical     = lv.get('is_critical', False),
                )

            new_records.append(record)
            flag = '  [!]' if any(lv.get('is_critical') for lv in rd.get('lab_values', [])) else ''
            self.stdout.write(
                f'  Created: [{rd["record_type"]:12}] {rd["title"]} — {rd["record_date"]}{flag}'
            )

        self.stdout.write(self.style.SUCCESS(f'\n  Total new records created: {len(new_records)}'))

        # ── RAG indexing ───────────────────────────────────────────────────────
        if not options['no_index'] and new_records:
            self.stdout.write('\n  Indexing records into RAG vector store...')
            from rag_assistant.services.rag_service import RAGService
            svc = RAGService()
            total_chunks = 0
            for rec in new_records:
                n = svc.index_record(rec)
                total_chunks += n
                self.stdout.write(f'    Indexed: {rec.title} -> {n} chunks')
            self.stdout.write(self.style.SUCCESS(f'  Total chunks indexed: {total_chunks}'))
        elif options['no_index']:
            self.stdout.write('  Skipped RAG indexing (--no-index).')
        else:
            self.stdout.write('  No new records to index.')

        # ── Optional cold-start patient ────────────────────────────────────────
        if options['cold_start']:
            cold, cold_created = User.objects.get_or_create(
                username=COLD_USERNAME,
                defaults={
                    'email':       COLD_EMAIL,
                    'first_name':  'New',
                    'last_name':   'Patient',
                    'role':        'patient',
                    'is_approved': True,
                },
            )
            if cold_created:
                cold.set_password(COLD_PASSWORD)
                cold.save()
                self.stdout.write(self.style.SUCCESS(
                    f'\n  Created cold-start patient: {COLD_USERNAME}  (password: {COLD_PASSWORD})'
                ))
            else:
                self.stdout.write(f'\n  Cold-start patient {COLD_USERNAME} already exists.')

        # ── Summary ────────────────────────────────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('\n=== Seeding Complete ==='))
        self.stdout.write('')
        self.stdout.write('  Demo patient credentials:')
        self.stdout.write(f'    Username : {SARA_USERNAME}')
        self.stdout.write(f'    Password : {SARA_PASSWORD}')
        self.stdout.write('')
        self.stdout.write('  Suggested test queries (log in as sara.m):')
        self.stdout.write('    "Is my kidney function getting better or worse?"')
        self.stdout.write('    "How has my HbA1c changed over time?"')
        self.stdout.write('    "Is my creatinine trending in the right direction?"')
        self.stdout.write('    "Show me my eGFR history"')
        self.stdout.write('    "What medications am I on?"')
        self.stdout.write('')
