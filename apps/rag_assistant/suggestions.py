"""
Starter questions for an empty chat, built from what the user actually has.

A fixed list asked "What are my current medications?" of people with no
records at all, which the assistant could only answer with "I don't know".
Every question here is one the data can answer: a lab question only when there
are lab values, a named analyte only when that analyte exists for this user.
"""
from __future__ import annotations

MAX = 4

GENERAL = [
    'What does HbA1c measure?',
    'How can I lower my blood pressure?',
    'When should a cough be checked by a doctor?',
    'What is a normal resting heart rate?',
]

PROFESSIONAL = [
    'Summarise the current guideline for type 2 diabetes.',
    'What are first-line treatments for hypertension?',
    'Which lab tests monitor kidney function?',
    'What are red flags for headache?',
]


def starter_questions(user) -> list[str]:
    if not getattr(user, 'is_patient', False):
        return PROFESSIONAL[:MAX]

    from apps.medical_records.models import ParsedLabValue

    out: list[str] = []
    labs = ParsedLabValue.objects.filter(patient=user)
    if labs.exists():
        out.append('What do my latest lab results show?')
        abnormal = (labs.filter(is_abnormal=True)
                    .order_by('-measured_at', '-pk')
                    .values_list('parameter_name', flat=True).first())
        if abnormal:
            out.append(f'What does my {abnormal} result mean?')
        # A trend needs at least two measurements of the same analyte.
        from django.db.models import Count
        repeated = (labs.values('parameter_name')
                    .annotate(n=Count('pk')).filter(n__gte=2)
                    .order_by('-n').values_list('parameter_name', flat=True).first())
        if repeated:
            out.append(f'How has my {repeated} changed over time?')
        if not abnormal:
            out.append('Do I have any abnormal values?')
    elif user.medical_records.exists():
        out.append('Summarise my uploaded records.')

    for q in GENERAL:
        if len(out) >= MAX:
            break
        out.append(q)
    # Order kept, duplicates dropped.
    return list(dict.fromkeys(out))[:MAX]
