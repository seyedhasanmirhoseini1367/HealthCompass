# ai_insights/management/commands/seed_demo_models.py
"""
Creates demo AIModel entries so the website UI can be reviewed without
training real models. All entries use the rule-based fallback (no model
file uploaded), so the "Run" button will produce a plausible demo result.

Usage:
    python manage.py seed_demo_models
    python manage.py seed_demo_models --clear   # remove all demo models first
"""
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

User = get_user_model()

DEMO_MODELS = [

    # ── Tabular ───────────────────────────────────────────────────────────────
    {
        'name':        'Diabetes Risk Predictor',
        'slug':        'diabetes-risk-predictor',
        'category':    'diabetes',
        'input_type':  'tabular',
        'description': (
            'Estimates the probability of Type 2 diabetes based on clinical '
            'measurements. Combines fasting glucose, BMI, HbA1c, and lifestyle '
            'factors using a gradient-boosted ensemble trained on 50,000 patients.'
        ),
        'input_schema': {
            'glucose_mmol':  {'label': 'Fasting Glucose (mmol/L)',  'type': 'float', 'min': 3,   'max': 25},
            'hba1c':         {'label': 'HbA1c (%)',                 'type': 'float', 'min': 4,   'max': 15},
            'bmi':           {'label': 'BMI',                       'type': 'float', 'min': 15,  'max': 60},
            'age':           {'label': 'Age',                       'type': 'int',   'min': 18,  'max': 100},
            'family_history':{'label': 'Family history (1=Yes, 0=No)', 'type': 'int', 'min': 0, 'max': 1},
        },
        'output_schema': {
            'labels': {'0': 'Low risk', '1': 'High risk'},
            'description': 'Binary diabetes risk classification',
        },
        'interpretation_guide': (
            'This model predicts Type 2 diabetes risk. '
            'Low risk (<45%) means current markers are within normal ranges. '
            'High risk (≥45%) means elevated markers warrant further clinical evaluation. '
            'HbA1c above 6.5% and fasting glucose above 7 mmol/L are diagnostic thresholds. '
            'Always refer the patient to their GP for confirmatory testing.'
        ),
    },
    {
        'name':        'Cardiovascular Risk Score (SCORE2)',
        'slug':        'cardiovascular-risk-score',
        'category':    'cardiovascular',
        'input_type':  'tabular',
        'description': (
            'Estimates 10-year risk of fatal or non-fatal cardiovascular events. '
            'Based on the ESC SCORE2 framework using age, sex, systolic blood '
            'pressure, total cholesterol, HDL, and smoking status.'
        ),
        'input_schema': {
            'age':          {'label': 'Age',                          'type': 'int',   'min': 18, 'max': 100},
            'systolic_bp':  {'label': 'Systolic Blood Pressure (mmHg)', 'type': 'int', 'min': 90, 'max': 220},
            'cholesterol':  {'label': 'Total Cholesterol (mmol/L)',   'type': 'float', 'min': 2,  'max': 12},
            'hdl':          {'label': 'HDL Cholesterol (mmol/L)',     'type': 'float', 'min': 0.5,'max': 4},
            'smoker':       {'label': 'Current smoker (1=Yes, 0=No)', 'type': 'int',   'min': 0,  'max': 1},
            'diabetes':     {'label': 'Diabetes (1=Yes, 0=No)',       'type': 'int',   'min': 0,  'max': 1},
        },
        'output_schema': {
            'labels': {'0': 'Low risk (<5%)', '1': 'Moderate risk (5–10%)', '2': 'High risk (>10%)'},
        },
        'interpretation_guide': (
            'SCORE2 estimates 10-year cardiovascular risk. '
            'Low risk means lifestyle changes alone are usually sufficient. '
            'Moderate risk may warrant statin therapy discussion. '
            'High risk requires immediate clinical evaluation and likely medication. '
            'Always verify with a cardiologist before any treatment decision.'
        ),
    },
    {
        'name':        'Chronic Kidney Disease Stage Classifier',
        'slug':        'ckd-stage-classifier',
        'category':    'general',
        'input_type':  'tabular',
        'description': (
            'Classifies CKD stage (1–5) from eGFR, serum creatinine, albumin, '
            'and urine ACR. Helps patients understand their kidney function '
            'trajectory and when to expect specialist referral.'
        ),
        'input_schema': {
            'egfr':       {'label': 'eGFR (mL/min/1.73m²)', 'type': 'float', 'min': 1,   'max': 120},
            'creatinine': {'label': 'Creatinine (μmol/L)',  'type': 'float', 'min': 40,  'max': 1200},
            'albumin':    {'label': 'Albumin (g/L)',         'type': 'float', 'min': 15,  'max': 55},
            'acr':        {'label': 'Urine ACR (mg/mmol)',   'type': 'float', 'min': 0,   'max': 300},
            'age':        {'label': 'Age',                   'type': 'int',   'min': 18,  'max': 100},
        },
        'output_schema': {
            'labels': {
                '0': 'Stage 1 — Normal/High (eGFR ≥90)',
                '1': 'Stage 2 — Mildly decreased (60–89)',
                '2': 'Stage 3a — Mild-moderate (45–59)',
                '3': 'Stage 3b — Moderate-severe (30–44)',
                '4': 'Stage 4 — Severely decreased (15–29)',
                '5': 'Stage 5 — Kidney failure (<15)',
            },
        },
        'interpretation_guide': (
            'CKD staging uses eGFR and albuminuria to categorise kidney function. '
            'Stages 1–2 with normal ACR require monitoring only. '
            'Stage 3+ warrants nephrology referral and cardiovascular risk management. '
            'Stage 5 requires preparation for renal replacement therapy.'
        ),
    },

    # ── Medical Image ─────────────────────────────────────────────────────────
    {
        'name':        'Chest X-Ray Pathology Detector',
        'slug':        'chest-xray-pathology-detector',
        'category':    'general',
        'input_type':  'image',
        'description': (
            'Analyses chest X-ray images (JPG/PNG/DICOM preview) for 14 common '
            'pathologies including pneumonia, pleural effusion, cardiomegaly, and '
            'consolidation. Based on a DenseNet-121 architecture trained on CheXNet.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {
                '0': 'No Finding',
                '1': 'Pneumonia',
                '2': 'Pleural Effusion',
                '3': 'Cardiomegaly',
                '4': 'Consolidation',
            },
        },
        'interpretation_guide': (
            'This model screens chest X-rays for common thoracic pathologies. '
            'A positive finding does NOT confirm a diagnosis — it flags areas '
            'that a radiologist should review. '
            'Always have X-ray results interpreted by a qualified radiologist and clinician.'
        ),
    },
    {
        'name':        'Diabetic Retinopathy Grader',
        'slug':        'diabetic-retinopathy-grader',
        'category':    'diabetes',
        'input_type':  'image',
        'description': (
            'Grades diabetic retinopathy severity (No DR → Proliferative DR) from '
            'fundus photographs. Designed for diabetic eye screening programs. '
            'Accepts high-resolution fundus images in JPG or PNG format.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {
                '0': 'No diabetic retinopathy',
                '1': 'Mild NPDR',
                '2': 'Moderate NPDR',
                '3': 'Severe NPDR',
                '4': 'Proliferative DR',
            },
        },
        'interpretation_guide': (
            'Diabetic retinopathy grading: Grade 0 = no signs detected, annual screening sufficient. '
            'Grade 1–2 = monitoring every 6 months. '
            'Grade 3+ = urgent ophthalmology referral. '
            'This is a screening tool — a qualified ophthalmologist must review all positive results.'
        ),
    },
    {
        'name':        'Brain MRI Tumour Segmentation',
        'slug':        'brain-mri-tumour-segmentation',
        'category':    'neurology',
        'input_type':  'image',
        'description': (
            'Detects and estimates the location of potential tumour regions in '
            'brain MRI scans (T1-weighted axial slices). Uses a U-Net architecture '
            'trained on the BraTS benchmark dataset. Upload a PNG/JPG slice.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {'0': 'No abnormality detected', '1': 'Potential abnormality — review required'},
        },
        'interpretation_guide': (
            'This model screens brain MRI slices for potential abnormal tissue regions. '
            'A positive result means the model detected patterns consistent with pathological tissue. '
            'This is not a diagnosis. All findings must be reviewed by a neuroradiologist.'
        ),
    },

    # ── Signals / EEG / ECG ───────────────────────────────────────────────────
    {
        'name':        'EEG Seizure Detector',
        'slug':        'eeg-seizure-detector',
        'category':    'neurology',
        'input_type':  'eeg_csv',
        'description': (
            'Detects ictal (seizure) activity in EEG recordings. Upload a CSV file '
            'where each column is an electrode channel and each row is a time sample '
            '(expected sampling rate: 256 Hz). Processes 10-second epochs using a '
            '1D-CNN trained on the CHB-MIT Scalp EEG dataset.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {'0': 'No seizure activity', '1': 'Seizure activity detected'},
        },
        'interpretation_guide': (
            'This model detects epileptiform activity in scalp EEG. '
            'A positive result should be reviewed by a neurologist alongside the raw recording. '
            'False positives can occur from muscle artefacts or movement. '
            'Never adjust anti-epileptic medication based solely on this result.'
        ),
        'handler_slug': 'eeg_csv',
    },
    {
        'name':        'ECG Arrhythmia Classifier',
        'slug':        'ecg-arrhythmia-classifier',
        'category':    'cardiovascular',
        'input_type':  'eeg_csv',
        'description': (
            'Classifies ECG rhythm strips into 5 categories: Normal Sinus Rhythm, '
            'Atrial Fibrillation, First-degree AV Block, Left Bundle Branch Block, '
            'and Premature Ventricular Contraction. Upload a single-lead CSV '
            '(500 Hz, minimum 10 seconds). Trained on the PhysioNet MIT-BIH dataset.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {
                '0': 'Normal sinus rhythm',
                '1': 'Atrial fibrillation',
                '2': 'First-degree AV block',
                '3': 'Left bundle branch block',
                '4': 'Premature ventricular contraction',
            },
        },
        'interpretation_guide': (
            'ECG arrhythmia classification from a single-lead strip. '
            'AF detection sensitivity is ~94% in clean recordings. '
            'Any arrhythmia finding requires clinical correlation and a 12-lead ECG. '
            'This tool is not cleared for diagnostic use — consult a cardiologist.'
        ),
    },
    {
        'name':        'PPG-Based Stress Level Estimator',
        'slug':        'ppg-stress-estimator',
        'category':    'wearable',
        'input_type':  'eeg_csv',
        'description': (
            'Estimates physiological stress level from a photoplethysmography (PPG) '
            'signal exported from a wearable (Fitbit, Garmin, Apple Watch). Upload a '
            'CSV with columns: timestamp, ppg_value. Analyses HRV features to '
            'classify low / medium / high stress.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {'0': 'Low stress', '1': 'Moderate stress', '2': 'High stress'},
        },
        'interpretation_guide': (
            'Stress estimation from PPG HRV analysis. '
            'Low stress: SDNN > 50ms, good autonomic balance. '
            'Moderate stress: reduced HRV, consider rest and breathing exercises. '
            'High stress: significantly suppressed HRV, may benefit from medical review '
            'if sustained over multiple days.'
        ),
    },

    # ── Wearable / Tabular file ───────────────────────────────────────────────
    {
        'name':        'Sleep Quality Analyser',
        'slug':        'sleep-quality-analyser',
        'category':    'wearable',
        'input_type':  'parquet',
        'description': (
            'Analyses nightly sleep data exported from wearable devices. Upload a '
            'CSV or Parquet file with columns: timestamp, heart_rate, movement, '
            'sleep_stage. Returns a sleep quality score and identifies periods of '
            'disrupted or insufficient deep sleep.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {
                '0': 'Poor sleep quality',
                '1': 'Moderate sleep quality',
                '2': 'Good sleep quality',
            },
        },
        'interpretation_guide': (
            'Sleep quality scoring from wearable data. '
            'Poor: <15% deep sleep or >5 awakenings — consider sleep hygiene review. '
            'Moderate: some fragmentation but adequate duration. '
            'Good: consistent sleep architecture with adequate deep and REM stages. '
            'Persistent poor sleep warrants a sleep study referral.'
        ),
    },
    {
        'name':        'Wearable Activity & Recovery Score',
        'slug':        'wearable-activity-recovery',
        'category':    'wearable',
        'input_type':  'parquet',
        'description': (
            'Computes a daily recovery score (0–100) from 7 days of wearable data: '
            'resting heart rate, HRV, steps, sleep duration, and activity intensity. '
            'Similar to the Oura Ring readiness score. Upload a CSV or Parquet '
            'file exported from your device.'
        ),
        'input_schema': {},
        'output_schema': {
            'labels': {
                '0': 'Low recovery — rest recommended',
                '1': 'Moderate recovery — light activity only',
                '2': 'High recovery — ready for intense training',
            },
        },
        'interpretation_guide': (
            'Recovery scoring synthesises multiple physiological signals. '
            'Low recovery: resting HR elevated, HRV suppressed — prioritise sleep and avoid intense exercise. '
            'Moderate: ready for moderate activity. '
            'High: optimal window for high-intensity training or competition.'
        ),
    },

    # ── Oncology ──────────────────────────────────────────────────────────────
    {
        'name':        'Breast Cancer Risk Stratifier',
        'slug':        'breast-cancer-risk-stratifier',
        'category':    'oncology',
        'input_type':  'tabular',
        'description': (
            'Estimates lifetime breast cancer risk using the Tyrer-Cuzick model '
            'factors: age, family history, reproductive history, BMI, and HRT use. '
            'Helps clinicians decide on enhanced surveillance or chemoprevention.'
        ),
        'input_schema': {
            'age':             {'label': 'Age',                                'type': 'int',   'min': 20,  'max': 80},
            'family_history':  {'label': 'First-degree family history (0/1)',  'type': 'int',   'min': 0,   'max': 1},
            'bmi':             {'label': 'BMI',                                'type': 'float', 'min': 15,  'max': 50},
            'hrt_use':         {'label': 'Current HRT use (0/1)',              'type': 'int',   'min': 0,   'max': 1},
            'age_first_child': {'label': 'Age at first child (0 if none)',     'type': 'int',   'min': 0,   'max': 50},
        },
        'output_schema': {
            'labels': {
                '0': 'Average risk (<15% lifetime)',
                '1': 'Moderate risk (15–30% lifetime)',
                '2': 'High risk (>30% lifetime)',
            },
        },
        'interpretation_guide': (
            'Lifetime breast cancer risk stratification. '
            'Average risk: standard screening guidelines apply. '
            'Moderate risk: consider annual mammography from age 40. '
            'High risk: refer to genetics clinic; consider MRI screening and chemoprevention discussion.'
        ),
    },

]


class Command(BaseCommand):
    help = 'Seed the database with demo AI models for UI review'

    def add_arguments(self, parser):
        parser.add_argument(
            '--clear',
            action='store_true',
            help='Delete all existing demo models before seeding',
        )

    def handle(self, *args, **options):
        from apps.ai_insights.models import AIModel

        if options['clear']:
            slugs = [m['slug'] for m in DEMO_MODELS]
            deleted, _ = AIModel.objects.filter(slug__in=slugs).delete()
            self.stdout.write(self.style.WARNING(f'Deleted {deleted} demo models'))

        # Find or create a superuser to own the models
        admin = User.objects.filter(is_superuser=True).first()
        if not admin:
            self.stdout.write(self.style.ERROR(
                'No superuser found. Run "python manage.py createsuperuser" first.'
            ))
            return

        created = 0
        updated = 0
        for data in DEMO_MODELS:
            handler_slug = data.pop('handler_slug', '')
            obj, is_new = AIModel.objects.update_or_create(
                slug=data['slug'],
                defaults={
                    **data,
                    'data_scientist': admin,
                    'status':         'active',
                    'handler_slug':   handler_slug,
                    'handler_config': {},
                },
            )
            if is_new:
                created += 1
                self.stdout.write(f'  Created: {obj.name}')
            else:
                updated += 1
                self.stdout.write(f'  Updated: {obj.name}')

        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {created} created, {updated} updated. '
            f'Visit /insights/ to review the UI.'
        ))
