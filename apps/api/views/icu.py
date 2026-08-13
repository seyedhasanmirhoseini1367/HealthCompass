import io

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..throttling import PredictionThrottle
from healthcompass.errors import client_error


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def icu_dashboard_api(request):
    """Return ICU demo context as JSON for the mobile app."""
    import random  # local Random() below — never the module-level RNG

    # random.Random(...) rather than random.seed(...): seeding the

    # module-level RNG is process-wide state, so a concurrent request

    # elsewhere using random would get its sequence reset.

    _random = random.Random(42)
    vitals = {
        'hr':   [[i * 0.5, round(92 + _random.uniform(-8, 8), 1)]  for i in range(60)],
        'map':  [[i * 0.5, round(63 + _random.uniform(-8, 6), 1)]  for i in range(60)],
        'spo2': [[i * 0.5, round(91 + _random.uniform(-3, 5), 1)]  for i in range(60)],
        'rr':   [[i * 0.5, round(22 + _random.uniform(-3, 5), 1)]  for i in range(60)],
    }
    labs = {
        'creatinine': [[i * 4, round(2.1 + i * 0.12 + _random.uniform(-0.1, 0.1), 2)] for i in range(8)],
        'lactate':    [[i * 4, round(2.8 - i * 0.05 + _random.uniform(-0.1, 0.1), 2)] for i in range(8)],
        'wbc':        [[i * 4, round(13  + _random.uniform(-2, 3), 1)]                 for i in range(8)],
        'platelets':  [[i * 4, round(148 - i * 2 + _random.uniform(-5, 5), 0)]         for i in range(8)],
    }
    sofa = [
        {'organ': 'Respiratory',    'score': 3, 'max': 4},
        {'organ': 'Coagulation',    'score': 1, 'max': 4},
        {'organ': 'Liver',          'score': 1, 'max': 4},
        {'organ': 'Cardiovascular', 'score': 2, 'max': 4},
        {'organ': 'Neurological',   'score': 4, 'max': 4},
        {'organ': 'Renal',          'score': 4, 'max': 4},
    ]
    lab_snap = [
        {'name': 'Creatinine', 'value': 5.5,  'unit': 'mg/dL',  'ref': '0.6–1.2',  'flag': 'danger'},
        {'name': 'Lactate',    'value': 4.2,  'unit': 'mmol/L', 'ref': '0.5–2.0',  'flag': 'danger'},
        {'name': 'WBC',        'value': 13.6, 'unit': 'K/uL',   'ref': '4.5–11',   'flag': 'warning'},
        {'name': 'Platelets',  'value': 148,  'unit': 'K/uL',   'ref': '150–400',  'flag': 'warning'},
        {'name': 'Hemoglobin', 'value': 9.2,  'unit': 'g/dL',   'ref': '12–17',    'flag': 'danger'},
        {'name': 'BUN',        'value': 107,  'unit': 'mg/dL',  'ref': '7–20',     'flag': 'danger'},
        {'name': 'Sodium',     'value': 132,  'unit': 'mEq/L',  'ref': '136–145',  'flag': 'warning'},
        {'name': 'Glucose',    'value': 188,  'unit': 'mg/dL',  'ref': '70–110',   'flag': 'warning'},
    ]
    events = [
        {'delta': 'T+27.5h', 'source': 'CHART', 'label': 'Heart Rate',     'value': '94 bpm'},
        {'delta': 'T+27.2h', 'source': 'CHART', 'label': 'MAP',            'value': '54 mmHg'},
        {'delta': 'T+26.0h', 'source': 'INPUT', 'label': 'Norepinephrine', 'value': '0.08 mcg/kg/min'},
        {'delta': 'T+24.0h', 'source': 'OUTPUT','label': 'Foley Urine',    'value': '28 mL'},
        {'delta': 'T+22.0h', 'source': 'LAB',   'label': 'Creatinine',     'value': '5.5 mg/dL'},
        {'delta': 'T+20.0h', 'source': 'LAB',   'label': 'Lactate',        'value': '4.2 mmol/L'},
        {'delta': 'T+18.0h', 'source': 'LAB',   'label': 'WBC',            'value': '13.6 K/uL'},
        {'delta': 'T+12.0h', 'source': 'INPUT', 'label': 'NS 500mL',       'value': '500 mL'},
        {'delta': 'T+6.0h',  'source': 'LAB',   'label': 'Platelet Count', 'value': '148 K/uL'},
    ]
    return Response({
        # Synthetic demo patient — identifiers deliberately prefixed DEMO- and
        # outside any real dataset's range. The values used here previously
        # matched MIMIC-IV's identifier ranges and date-shifting; they are not
        # repeated in this comment. Kept in sync with
        # apps/ai_insights/views/icu.py.
        'patient': {
            'subject_id': 'DEMO-900001', 'stay_id': 'DEMO-900002',
            'age': 67, 'gender': 'Male',
            'unit': 'Medical ICU (MICU)',
            'intime': '2026-07-03 22:45', 'los_days': 3.25,
            'admission_type': 'Emergency', 'n_events': 1842,
            'outcome': 'Deceased',
        },
        'sofa': sofa,
        'sofa_total': sum(s['score'] for s in sofa),
        'vitals': vitals,
        'labs': labs,
        'lab_snap': lab_snap,
        'events': events,
        'note': 'Demo data — MIMIC-IV subset (de-identified)',
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([PredictionThrottle])
def seizure_realtime_analyze(request):
    """
    Accept a parquet/csv EEG file. Run the local ONNX ensemble across
    overlapping 10-second windows and return a time-series of predictions.
    """
    f = request.FILES.get('signal_file')
    if not f:
        return Response({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        import pandas as pd
        raw  = f.read()
        name = f.name.lower()
        df = (pd.read_parquet(io.BytesIO(raw)) if name.endswith('.parquet')
              else pd.read_csv(io.StringIO(raw.decode('utf-8', errors='replace'))))
        if 'EKG' in df.columns:
            df = df.drop(columns=['EKG'])
        df = df.iloc[:, :19]

        from apps.ai_insights.inference.seizure_inference import predict
        total    = len(df)
        fs       = 200
        win      = fs * 10
        step     = fs * 5
        timeline = []

        for start in range(0, max(total - win + 1, 1), step):
            chunk = df.iloc[start:start + win]
            if len(chunk) < win // 2:
                break
            data_dict = {col: chunk[col].astype(float).tolist() for col in chunk.columns}
            res = predict(data_dict, variant='ensemble')
            timeline.append({
                'time_sec':   round(start / fs, 1),
                'label':      res.get('label', ''),
                'confidence': round(res.get('confidence', 0.0), 4),
                'is_seizure': 'seizure' in res.get('label', '').lower(),
            })

        seizure_count = sum(1 for t in timeline if t['is_seizure'])
        total_windows = len(timeline)
        seizure_pct   = round(100 * seizure_count / total_windows, 1) if total_windows else 0

        return Response({
            'timeline':        timeline,
            'total_windows':   total_windows,
            'seizure_windows': seizure_count,
            'seizure_pct':     seizure_pct,
            'duration_sec':    round(total / fs, 1),
            'summary':         ('Seizure activity detected' if seizure_pct >= 30 else
                                'Possible ictal activity' if seizure_pct >= 10 else
                                'No significant seizure activity detected'),
        })
    except Exception as exc:
        return Response(client_error(exc, context='icu'), status=status.HTTP_500_INTERNAL_SERVER_ERROR)
