import json
import os
import random as _random

import pandas as _pd
from django.shortcuts import render
from apps.accounts.safe_json import script_safe_json

_DATA_BASE  = getattr(__import__('django.conf', fromlist=['settings']).settings, 'ICU_DEMO_DATA_PATH', '')
_INDEX_PATH = os.path.join(_DATA_BASE, 'index.csv') if _DATA_BASE else ''

_VITAL_ITEMIDS = {
    'hr':   [220045],
    'map':  [220052, 220181],
    'spo2': [220277],
    'rr':   [220210],
}
_LAB_ITEMIDS = {
    'creatinine': [50912, 220615],
    'lactate':    [50813, 225668],
    'wbc':        [51301, 220546],
    'platelets':  [51265, 227457],
}
_SNAPSHOT_ITEMS = {
    'Creatinine':  ([50912, 220615],  (0.6,  1.2,  'mg/dL')),
    'WBC':         ([51301, 220546],  (4.5,  11.0, 'K/uL')),
    'Platelets':   ([51265, 227457],  (150,  400,  'K/uL')),
    'Hemoglobin':  ([51222],          (12,   17,   'g/dL')),
    'Lactate':     ([50813, 225668],  (0.5,  2.0,  'mmol/L')),
    'Glucose':     ([50931, 226537],  (70,   110,  'mg/dL')),
    'BUN':         ([51006, 225624],  (7,    20,   'mg/dL')),
    'Potassium':   ([50971, 227442],  (3.5,  5.0,  'mEq/L')),
    'Sodium':      ([50983, 220645],  (136,  145,  'mEq/L')),
    'Bicarbonate': ([50882, 227443],  (22,   29,   'mEq/L')),
    'INR':         ([51237],          (0.8,  1.2,  '')),
    'Bilirubin':   ([50885],          (0.2,  1.2,  'mg/dL')),
}


def _icu_last(df, itemids):
    sub = df[df['itemid'].isin(itemids)].dropna(subset=['value'])
    if sub.empty:
        return None
    try:
        return float(sub.sort_values('delta_hours').iloc[-1]['value'])
    except Exception:
        return None


def _icu_series(df, itemids, max_pts=60):
    sub = (df[df['itemid'].isin(itemids) & (df['delta_hours'] >= 0)]
           .dropna(subset=['value'])
           .sort_values('delta_hours'))
    if sub.empty:
        return []
    if len(sub) > max_pts:
        idx = [int(i * (len(sub) - 1) / (max_pts - 1)) for i in range(max_pts)]
        sub = sub.iloc[idx]
    return [[round(float(r['delta_hours']), 2), round(float(r['value']), 2)]
            for _, r in sub.iterrows()]


def _icu_compute_sofa(df):
    crea  = _icu_last(df, [50912, 220615])
    plt   = _icu_last(df, [51265, 227457])
    bili  = _icu_last(df, [50885])
    map_  = _icu_last(df, [220052, 220181])
    pao2  = _icu_last(df, [50821])
    fio2  = _icu_last(df, [223835])
    spo2  = _icu_last(df, [220277])
    gcs_e = _icu_last(df, [220739])
    gcs_v = _icu_last(df, [223900])
    gcs_m = _icu_last(df, [223901])

    resp = None
    if pao2 and fio2 and fio2 > 0:
        pf = pao2 / (fio2 / 100 if fio2 > 1 else fio2)
        resp = 4 if pf < 100 else 3 if pf < 200 else 2 if pf < 300 else 1 if pf < 400 else 0
    elif spo2:
        resp = 2 if spo2 < 90 else 1 if spo2 < 95 else 0

    coag  = (4 if plt < 20 else 3 if plt < 50 else 2 if plt < 100 else 1 if plt < 150 else 0) if plt else None
    liver = (4 if bili >= 12 else 3 if bili >= 6 else 2 if bili >= 2 else 1 if bili >= 1.2 else 0) if bili else None
    cardio = (1 if map_ < 70 else 0) if map_ else None

    neuro = None
    if gcs_e is not None and gcs_v is not None and gcs_m is not None:
        gcs = int(gcs_e + gcs_v + gcs_m)
        neuro = 4 if gcs < 6 else 3 if gcs < 9 else 2 if gcs < 12 else 1 if gcs < 14 else 0

    renal = (4 if crea >= 5 else 3 if crea >= 3.5 else 2 if crea >= 2 else 1 if crea >= 1.2 else 0) if crea else None

    scores = {
        'Respiratory': resp, 'Coagulation': coag,
        'Liver': liver,      'Cardiovascular': cardio,
        'Neurological': neuro, 'Renal': renal,
    }
    total = sum(v for v in scores.values() if v is not None)
    return scores, total


def _icu_sofa_color(s):
    if s is None:
        return '#475569'
    return ('#22c55e', '#84cc16', '#f59e0b', '#f97316', '#ef4444')[min(s, 4)]


def _icu_events(df, n=15):
    icu = df[df['delta_hours'] >= 0].sort_values('delta_hours', ascending=False).head(n)
    out = []
    for _, r in icu.iterrows():
        try:
            val = f"{float(r['value']):.1f}"
        except Exception:
            val = '—'
        unit = str(r['unit'] or '').strip()
        out.append({'delta': f"T+{r['delta_hours']:.1f}h", 'source': str(r['source']),
                    'label': str(r['label']), 'value': f"{val} {unit}".strip()})
    return out


def _icu_lab_snapshot(df):
    rows = []
    for name, (ids, (lo, hi, unit)) in _SNAPSHOT_ITEMS.items():
        val = _icu_last(df, ids)
        if val is None:
            continue
        flag = 'danger' if (val < lo * 0.7 or val > hi * 1.5) else 'warning' if (val < lo or val > hi) else ''
        rows.append({'name': name, 'value': round(val, 2), 'unit': unit,
                     'range': f"{lo}–{hi}", 'flag': flag})
    return rows


def _icu_unit_abbr(full):
    s = str(full)
    return s.split('(')[-1].replace(')', '').strip() if '(' in s else s[:12]


def _icu_mock_eeg(seed=7):
    _random.seed(seed)
    labels = [f'{i}:00' for i in range(24)]
    prob = [round(_random.uniform(0.01, 0.15), 3) for _ in range(18)]
    prob += [round(_random.uniform(0.55, 0.82), 3) for _ in range(3)]
    prob += [round(_random.uniform(0.05, 0.20), 3) for _ in range(3)]
    return labels, prob


def _icu_mock_context():
    _random.seed(42)
    vitals = {
        'hr':   [[i * 0.5, round(92 + _random.uniform(-8, 8), 1)] for i in range(60)],
        'map':  [[i * 0.5, round(63 + _random.uniform(-8, 6), 1)] for i in range(60)],
        'spo2': [[i * 0.5, round(91 + _random.uniform(-3, 5), 1)] for i in range(60)],
        'rr':   [[i * 0.5, round(22 + _random.uniform(-3, 5), 1)] for i in range(60)],
    }
    labs = {
        'creatinine': [[i * 4, round(2.1 + i * 0.12 + _random.uniform(-0.1, 0.1), 2)] for i in range(8)],
        'lactate':    [[i * 4, round(2.8 - i * 0.05 + _random.uniform(-0.1, 0.1), 2)] for i in range(8)],
        'wbc':        [[i * 4, round(13 + _random.uniform(-2, 3), 1)] for i in range(8)],
        'platelets':  [[i * 4, round(148 - i * 2 + _random.uniform(-5, 5), 0)] for i in range(8)],
    }
    sofa_raw = {'Respiratory': 3, 'Coagulation': 1, 'Liver': 1,
                'Cardiovascular': 2, 'Neurological': 4, 'Renal': 4}
    sofa_display = [{'name': k, 'score': v, 'color': _icu_sofa_color(v)} for k, v in sofa_raw.items()]
    # Synthetic demo patient. Identifiers are deliberately prefixed DEMO- and
    # outside any real dataset's range.
    #
    # The values used here previously matched MIMIC-IV's subject_id/stay_id
    # ranges and its characteristic date-shifting, i.e. credentialed-dataset
    # content committed to a public repository. They are not repeated here: a
    # comment quoting them would leave the identifiers in the file it removed
    # them from. The surrounding vitals were always synthetic
    # (_random.uniform), so nothing clinical is lost by making the identity
    # synthetic too.
    patient = {
        'subject_id': 'DEMO-900001', 'stay_id': 'DEMO-900002',
        'age': 67, 'gender': 'Male',
        'unit': 'Medical ICU (MICU)', 'unit_short': 'MICU',
        'intime': '2026-07-03 22:45', 'los_days': 3.25,
        'admission_type': 'EW EMER.', 'n_events': 1842,
        'hospital_expire_flag': 1, 'discharge_location': 'DIED',
    }
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
    lab_snap = [
        {'name': 'Creatinine', 'value': 5.5,  'unit': 'mg/dL',  'range': '0.6–1.2', 'flag': 'danger'},
        {'name': 'Lactate',    'value': 4.2,  'unit': 'mmol/L', 'range': '0.5–2.0', 'flag': 'danger'},
        {'name': 'WBC',        'value': 13.6, 'unit': 'K/uL',   'range': '4.5–11',  'flag': 'warning'},
        {'name': 'Platelets',  'value': 148,  'unit': 'K/uL',   'range': '150–400', 'flag': 'warning'},
        {'name': 'Hemoglobin', 'value': 9.2,  'unit': 'g/dL',   'range': '12–17',   'flag': 'danger'},
        {'name': 'BUN',        'value': 107,  'unit': 'mg/dL',  'range': '7–20',    'flag': 'danger'},
        {'name': 'Sodium',     'value': 132,  'unit': 'mEq/L',  'range': '136–145', 'flag': 'warning'},
        {'name': 'Glucose',    'value': 188,  'unit': 'mg/dL',  'range': '70–110',  'flag': 'warning'},
    ]
    eeg_labels, eeg_prob = _icu_mock_eeg()
    return {
        'real_data': False, 'patients': [], 'selected_id': None, 'patient': patient,
        'vitals': {k: script_safe_json(v) for k, v in vitals.items()},
        'labs':   {k: script_safe_json(v) for k, v in labs.items()},
        'sofa_scores': sofa_display, 'sofa_total': sum(sofa_raw.values()),
        'lab_snap': lab_snap, 'events': events,
        'eeg_labels': script_safe_json(eeg_labels), 'eeg_prob': script_safe_json(eeg_prob),
    }


def icu_demo(request):
    if not (_DATA_BASE and os.path.isfile(_INDEX_PATH)):
        return render(request, 'ai_insights/icu/demo.html', _icu_mock_context())

    index = _pd.read_csv(_INDEX_PATH)
    patients = []
    for _, row in index.iterrows():
        abbr = _icu_unit_abbr(str(row['first_careunit']))
        flag = int(row['hospital_expire_flag'])
        outcome = '☠ Died' if flag else '✓ Survived'
        patients.append({
            'stay_id': int(row['stay_id']),
            'label': (f"Subj {row['subject_id']} · {row['age']}"
                      f"{'M' if row['gender']=='M' else 'F'} · {abbr} · "
                      f"LOS {round(float(row['los']),1)}d · {outcome}"),
            'hospital_expire_flag': flag,
        })

    try:
        selected_id = int(request.GET.get('stay', patients[0]['stay_id']))
    except Exception:
        selected_id = patients[0]['stay_id']

    row = index[index['stay_id'] == selected_id].iloc[0]
    fname = os.path.join(_DATA_BASE, 'all_stays', str(row['file_path']))
    df = _pd.read_csv(fname)
    df['value'] = _pd.to_numeric(df['value'], errors='coerce')

    abbr = _icu_unit_abbr(str(row['first_careunit']))
    patient = {
        'subject_id': int(row['subject_id']), 'stay_id': selected_id,
        'age': int(row['age']), 'gender': 'Male' if row['gender'] == 'M' else 'Female',
        'unit': str(row['first_careunit']), 'unit_short': abbr,
        'intime': str(row['intime'])[:16], 'los_days': round(float(row['los']), 1),
        'admission_type': str(row['admission_type']), 'n_events': int(row['n_events']),
        'hospital_expire_flag': int(row['hospital_expire_flag']),
        'discharge_location': str(row['discharge_location']),
    }

    vitals = {k: _icu_series(df, v)             for k, v in _VITAL_ITEMIDS.items()}
    labs   = {k: _icu_series(df, v, max_pts=30) for k, v in _LAB_ITEMIDS.items()}
    sofa_raw, sofa_total = _icu_compute_sofa(df)
    sofa_display = [{'name': k, 'score': v, 'color': _icu_sofa_color(v)} for k, v in sofa_raw.items()]
    eeg_labels, eeg_prob = _icu_mock_eeg(seed=selected_id % 97)

    ctx = {
        'real_data': True, 'patients': patients, 'selected_id': selected_id,
        'patient': patient,
        'vitals': {k: script_safe_json(v) for k, v in vitals.items()},
        'labs':   {k: script_safe_json(v) for k, v in labs.items()},
        'sofa_scores': sofa_display, 'sofa_total': sofa_total,
        'lab_snap': _icu_lab_snapshot(df), 'events': _icu_events(df),
        'eeg_labels': script_safe_json(eeg_labels), 'eeg_prob': script_safe_json(eeg_prob),
    }
    return render(request, 'ai_insights/icu/demo.html', ctx)
