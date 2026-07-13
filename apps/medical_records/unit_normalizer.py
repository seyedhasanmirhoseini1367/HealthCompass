"""
Unit normalization for lab values.

Finnish labs (FIMLAB, HUSlab) report in SI units (µmol/L, mmol/L).
Most internal thresholds and reference ranges in this codebase use
conventional US units (mg/dL). Without conversion a creatinine of
112 µmol/L looks like 112 mg/dL — 80× the critical threshold.

Strategy:
- Normalize to a single canonical unit at ingestion time.
- Store the original value+unit in separate fields so the UI can
  display what the lab actually reported.
- All trajectory / threshold comparisons use the canonical value.

Canonical units chosen to match the cold-start reference ranges in
load_knowledge_base.py and the seed data in seed_trajectory_patient.py.
"""

from typing import Optional, Tuple

# (parameter_key, input_unit) → (canonical_unit, conversion_fn)
# parameter_key is lowercase, stripped of spaces and special chars.
_CONVERSIONS: dict = {
    # Creatinine: µmol/L → mg/dL
    ('creatinine', 'µmol/l'):  ('mg/dL', lambda x: x / 88.4),
    ('creatinine', 'umol/l'):  ('mg/dL', lambda x: x / 88.4),
    ('creatinine', 'μmol/l'):  ('mg/dL', lambda x: x / 88.4),
    # Glucose: mmol/L → mg/dL
    ('glucose',    'mmol/l'):  ('mg/dL', lambda x: x * 18.016),
    ('fasting glucose', 'mmol/l'): ('mg/dL', lambda x: x * 18.016),
    ('blood glucose',   'mmol/l'): ('mg/dL', lambda x: x * 18.016),
    ('blood sugar',     'mmol/l'): ('mg/dL', lambda x: x * 18.016),
    # Cholesterol / lipids: mmol/L → mg/dL
    ('total cholesterol', 'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('cholesterol',       'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('ldl',               'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('ldl cholesterol',   'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('hdl',               'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('hdl cholesterol',   'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('triglycerides',     'mmol/l'): ('mg/dL', lambda x: x * 88.57),
    ('triglyceride',      'mmol/l'): ('mg/dL', lambda x: x * 88.57),
    # Hemoglobin: mmol/L → g/dL
    ('hemoglobin', 'mmol/l'): ('g/dL', lambda x: x / 0.6206),
    ('haemoglobin','mmol/l'): ('g/dL', lambda x: x / 0.6206),
    ('hgb',        'mmol/l'): ('g/dL', lambda x: x / 0.6206),
    ('hb',         'mmol/l'): ('g/dL', lambda x: x / 0.6206),
    # uric acid: µmol/L → mg/dL
    ('uric acid',  'µmol/l'): ('mg/dL', lambda x: x / 59.48),
    ('uric acid',  'umol/l'): ('mg/dL', lambda x: x / 59.48),
    # Calcium: mmol/L → mg/dL
    ('calcium',    'mmol/l'): ('mg/dL', lambda x: x * 4.008),
    # Phosphate: mmol/L → mg/dL
    ('phosphate',  'mmol/l'): ('mg/dL', lambda x: x * 3.097),
    ('phosphorus', 'mmol/l'): ('mg/dL', lambda x: x * 3.097),
    # Bilirubin: µmol/L → mg/dL
    ('bilirubin',        'µmol/l'): ('mg/dL', lambda x: x / 17.1),
    ('total bilirubin',  'µmol/l'): ('mg/dL', lambda x: x / 17.1),
    ('bilirubin',        'umol/l'): ('mg/dL', lambda x: x / 17.1),
    ('total bilirubin',  'umol/l'): ('mg/dL', lambda x: x / 17.1),
    # Vitamin D: nmol/L → ng/mL
    ('vitamin d',         'nmol/l'): ('ng/mL', lambda x: x / 2.496),
    ('25-hydroxyvitamin d','nmol/l'): ('ng/mL', lambda x: x / 2.496),
    ('25(oh)d',           'nmol/l'): ('ng/mL', lambda x: x / 2.496),
}


def normalize(
    parameter_name: str,
    value_str: str,
    unit: str,
) -> Tuple[Optional[float], str, str]:
    """
    Return (canonical_value, canonical_unit, original_unit).

    If no conversion is defined, canonical_value is the raw float parse
    of value_str (or None if unparseable) and canonical_unit == unit.
    The original_unit is always preserved unchanged.
    """
    original_unit = unit

    # Try to parse the raw value
    try:
        raw = float(value_str.replace(',', '.'))
    except (ValueError, AttributeError):
        return None, unit, original_unit

    # Look up conversion
    param_key = parameter_name.lower().strip()
    unit_key  = unit.lower().strip()
    key = (param_key, unit_key)

    if key in _CONVERSIONS:
        canonical_unit, fn = _CONVERSIONS[key]
        try:
            canonical_value = round(fn(raw), 4)
        except Exception:
            canonical_value = raw
            canonical_unit  = unit
    else:
        canonical_value = raw
        canonical_unit  = unit

    return canonical_value, canonical_unit, original_unit
