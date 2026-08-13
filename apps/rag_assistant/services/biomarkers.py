"""
Biomarker vocabulary — the single source of truth.

This table used to exist twice: once in trajectory_service and once in
query_understanding. They drifted, and the classifier copy ended up missing six
biomarkers (blood_pressure, heart_rate, platelets, urea, wbc, weight). The
trajectory service could therefore recognise a biomarker the classifier could
not, so recency questions about those six were routed as though no biomarker had
been named.

Copying the missing entries across would have fixed the symptom and left the
duplication in place to drift again, so the second table is gone: both modules
now import from here.

Matching goes through services/text_match.py, which handles regular plurals
("platelet" matches "platelets") while keeping the boundary protections that stop
`bp` matching "bpm" or `creat` matching "recreate".

Ordering matters: more specific aliases must precede shorter, more ambiguous ones,
because detect() returns the first canonical name whose alias set matches.
"""
from typing import Dict, List, Optional

from apps.rag_assistant.services.text_match import matches

#: Canonical biomarker name -> aliases as they appear in queries and in
#: ParsedLabValue.parameter_name.
BIOMARKERS: Dict[str, List[str]] = {
    'creatinine': [
        'creatinine', 'serum creatinine', 'creat', 'kidney function',
    ],
    'hba1c': [
        'hba1c', 'hemoglobin a1c', 'haemoglobin a1c', 'glycated hemoglobin',
        'glycated haemoglobin', 'a1c',
    ],
    'egfr': [
        'egfr', 'gfr', 'glomerular filtration rate', 'estimated gfr',
    ],
    'glucose': [
        'fasting glucose', 'blood glucose', 'blood sugar', 'glucose',
        'fbs', 'rbs', 'random blood sugar',
    ],
    'cholesterol': [
        'total cholesterol', 'ldl cholesterol', 'hdl cholesterol',
        'ldl', 'hdl', 'cholesterol', 'triglyceride',
    ],
    'hemoglobin': [
        'hemoglobin', 'haemoglobin', 'hgb', 'hb level',
    ],
    'blood_pressure': [
        'systolic', 'diastolic', 'blood pressure', 'bp',
    ],
    'heart_rate': [
        'heart rate', 'resting heart rate', 'pulse rate', 'bpm',
    ],
    'weight': [
        'body weight', 'weight', 'bmi',
    ],
    'tsh': [
        'thyroid stimulating hormone', 'tsh', 'thyroid function',
    ],
    'vitamin_d': [
        '25-hydroxyvitamin d', '25(oh)d', 'vitamin d', 'vit d',
    ],
    'sodium': ['sodium', 'serum sodium', 'na+'],
    'potassium': ['potassium', 'serum potassium', 'k+'],
    'urea': ['blood urea nitrogen', 'bun', 'urea'],
    'wbc': ['white blood cell', 'white blood count', 'wbc', 'leukocyte'],
    'platelets': ['platelet count', 'platelet', 'plt', 'thrombocyte'],
}

#: Canonical names, for tests and for callers that need to enumerate coverage.
CANONICAL_NAMES = tuple(BIOMARKERS)


def detect(text: str) -> Optional[str]:
    """
    Canonical biomarker named in *text*, or None.

    Used by both the query classifier and the trajectory service, so the two can
    no longer disagree about what counts as a biomarker.
    """
    if not text:
        return None
    lowered = text.lower()
    for canonical, aliases in BIOMARKERS.items():
        if any(matches(alias, lowered) for alias in aliases):
            return canonical
    return None


def aliases_for(canonical: str) -> List[str]:
    """Aliases for a canonical name; empty list when unknown."""
    return BIOMARKERS.get(canonical, [])
