"""
Unit normalization for lab values.

Finnish labs (FIMLAB, HUSlab) report in SI units (µmol/L, mmol/L, g/L).
Most internal thresholds and reference ranges in this codebase use
conventional US units (mg/dL, g/dL, %).

Without conversion:
  - creatinine 112 µmol/L reads as 112 mg/dL  (80× critical threshold)
  - hemoglobin 145 g/L reads as 145 g/dL       (10× normal range)
  - HbA1c 42 mmol/mol reads as 42%              (3× diabetic threshold — lethal misread)

Strategy:
- Normalize to a single canonical unit at ingestion time.
- Store the original value+unit so the UI can display what the lab reported.
- All trajectory / threshold comparisons use the canonical value.

Canonical units match the cold-start reference ranges in load_knowledge_base.py
and the seed data in seed_trajectory_patient.py.
"""

from typing import Optional, Tuple

# (parameter_key, input_unit) → (canonical_unit, conversion_fn)
# parameter_key is lowercase, stripped of spaces and special chars.
_CONVERSIONS: dict = {
    # ── Creatinine: µmol/L → mg/dL ───────────────────────────────────────────
    ('creatinine', 'µmol/l'):  ('mg/dL', lambda x: x / 88.4),
    ('creatinine', 'umol/l'):  ('mg/dL', lambda x: x / 88.4),
    ('creatinine', 'μmol/l'):  ('mg/dL', lambda x: x / 88.4),

    # ── Glucose: mmol/L → mg/dL ───────────────────────────────────────────────
    ('glucose',         'mmol/l'): ('mg/dL', lambda x: x * 18.016),
    ('fasting glucose', 'mmol/l'): ('mg/dL', lambda x: x * 18.016),
    ('blood glucose',   'mmol/l'): ('mg/dL', lambda x: x * 18.016),
    ('blood sugar',     'mmol/l'): ('mg/dL', lambda x: x * 18.016),

    # ── Cholesterol / lipids: mmol/L → mg/dL ──────────────────────────────────
    ('total cholesterol', 'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('cholesterol',       'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('ldl',               'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('ldl cholesterol',   'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('hdl',               'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('hdl cholesterol',   'mmol/l'): ('mg/dL', lambda x: x * 38.67),
    ('triglycerides',     'mmol/l'): ('mg/dL', lambda x: x * 88.57),
    ('triglyceride',      'mmol/l'): ('mg/dL', lambda x: x * 88.57),

    # ── Hemoglobin ────────────────────────────────────────────────────────────
    # FIMLAB/HUSlab report in g/L (e.g. 145 g/L) — NOT mmol/L.
    # Dutch/Danish convention uses mmol/L (kept for completeness).
    # g/L → g/dL: divide by 10.
    ('hemoglobin',  'g/l'):    ('g/dL', lambda x: x / 10),
    ('haemoglobin', 'g/l'):    ('g/dL', lambda x: x / 10),
    ('hgb',         'g/l'):    ('g/dL', lambda x: x / 10),
    ('hb',          'g/l'):    ('g/dL', lambda x: x / 10),
    # mmol/L → g/dL (Dutch/Danish labs)
    ('hemoglobin',  'mmol/l'): ('g/dL', lambda x: x / 0.6206),
    ('haemoglobin', 'mmol/l'): ('g/dL', lambda x: x / 0.6206),
    ('hgb',         'mmol/l'): ('g/dL', lambda x: x / 0.6206),
    ('hb',          'mmol/l'): ('g/dL', lambda x: x / 0.6206),

    # ── HbA1c ────────────────────────────────────────────────────────────────
    # Finland (FIMLAB/HUSlab) reports HbA1c in mmol/mol (IFCC scale).
    # Internal thresholds use % (NGSP scale, e.g. diabetic threshold = 6.5%).
    # IFCC → NGSP: % = 0.0915 × mmol/mol + 2.15
    # Example: 42 mmol/mol → 6.0%  (normal);  53 mmol/mol → 7.0% (diabetic)
    # Without conversion 42 mmol/mol reads as 42% — a lethal misread.
    ('hba1c',              'mmol/mol'): ('%', lambda x: round(0.0915 * x + 2.15, 2)),
    ('hemoglobin a1c',     'mmol/mol'): ('%', lambda x: round(0.0915 * x + 2.15, 2)),
    ('haemoglobin a1c',    'mmol/mol'): ('%', lambda x: round(0.0915 * x + 2.15, 2)),
    ('glycated hemoglobin','mmol/mol'): ('%', lambda x: round(0.0915 * x + 2.15, 2)),
    ('a1c',                'mmol/mol'): ('%', lambda x: round(0.0915 * x + 2.15, 2)),

    # ── Uric acid: µmol/L → mg/dL ─────────────────────────────────────────────
    ('uric acid', 'µmol/l'): ('mg/dL', lambda x: x / 59.48),
    ('uric acid', 'umol/l'): ('mg/dL', lambda x: x / 59.48),

    # ── Calcium: mmol/L → mg/dL ───────────────────────────────────────────────
    ('calcium', 'mmol/l'): ('mg/dL', lambda x: x * 4.008),

    # ── Phosphate: mmol/L → mg/dL ─────────────────────────────────────────────
    ('phosphate',  'mmol/l'): ('mg/dL', lambda x: x * 3.097),
    ('phosphorus', 'mmol/l'): ('mg/dL', lambda x: x * 3.097),

    # ── Bilirubin: µmol/L → mg/dL ─────────────────────────────────────────────
    ('bilirubin',       'µmol/l'): ('mg/dL', lambda x: x / 17.1),
    ('total bilirubin', 'µmol/l'): ('mg/dL', lambda x: x / 17.1),
    ('bilirubin',       'umol/l'): ('mg/dL', lambda x: x / 17.1),
    ('total bilirubin', 'umol/l'): ('mg/dL', lambda x: x / 17.1),

    # ── Vitamin D: nmol/L → ng/mL ─────────────────────────────────────────────
    ('vitamin d',          'nmol/l'): ('ng/mL', lambda x: x / 2.496),
    ('25-hydroxyvitamin d','nmol/l'): ('ng/mL', lambda x: x / 2.496),
    ('25(oh)d',            'nmol/l'): ('ng/mL', lambda x: x / 2.496),
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
