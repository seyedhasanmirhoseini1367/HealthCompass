"""
HealthCompass RAG golden dataset — Sara Mirtaheri (seed patient).

Each item describes one test question with the ground-truth the retrieval
pipeline should produce.  Metrics computed against this dataset:

  Context Recall    — fraction of expected_source_titles found in retrieved chunks
  Context Precision — fraction of retrieved chunks that belong to an expected source
  Answer Completeness — fraction of expected_facts that appear in the model answer
                        (proxy for Faithfulness without an LLM judge)
  Question Coverage  — fraction of question keywords that appear in the model answer
                        (proxy for Answer Relevancy)

Field schema
------------
id                    : unique slug
question              : the query text
category              : trajectory | lab_point | medication | diagnosis | general
expected_route        : route the graph should classify this query as
expected_source_titles: titles of MedicalRecord rows that SHOULD be retrieved
                        (partial substring match is used — "Q4 Metabolic" matches
                        "Q4 Metabolic and Renal Panel")
expected_facts        : substrings that must appear in a correct answer
                        (numbers, dates, key clinical terms)
unexpected_facts      : strings that MUST NOT appear in the answer
                        (hallucination or confusion markers)
min_chunks            : minimum acceptable retrieved chunk count (sanity check)
notes                 : human rationale for including this item
"""

from typing import Any, Dict, List

Item = Dict[str, Any]

GOLDEN_DATASET: List[Item] = [

    # ── Trajectory (temporal / trend) ─────────────────────────────────────────
    {
        "id": "traj-01",
        "question": "Is my creatinine getting worse over time?",
        "category": "trajectory",
        "expected_route": "trajectory",
        "expected_source_titles": [
            "Annual Check-Up Lab Results",
            "Follow-Up Lab Panel",
            "Mid-Year Metabolic Panel",
            "Renal Function Review",
            "Q4 Metabolic and Renal Panel",
        ],
        "expected_facts": ["0.9", "2.1", "1.1", "1.4", "1.7"],
        "unexpected_facts": ["improving", "normal now"],
        "min_chunks": 3,
        "notes": "Core trajectory scenario — five visits show steady creatinine rise",
    },
    {
        "id": "traj-02",
        "question": "How has my eGFR changed over the past year?",
        "category": "trajectory",
        "expected_route": "trajectory",
        "expected_source_titles": [
            "Annual Check-Up Lab Results",
            "Follow-Up Lab Panel",
            "Mid-Year Metabolic Panel",
            "Renal Function Review",
            "Q4 Metabolic and Renal Panel",
        ],
        "expected_facts": ["88", "35", "48", "62", "80"],
        "unexpected_facts": ["improving", "increased"],
        "min_chunks": 3,
        "notes": "eGFR decline mirrors creatinine — must come from the same trajectory context",
    },
    {
        "id": "traj-03",
        "question": "Has my HbA1c been getting better or worse?",
        "category": "trajectory",
        "expected_route": "trajectory",
        "expected_source_titles": [
            "Annual Check-Up Lab Results",
            "Follow-Up Lab Panel",
            "Mid-Year Metabolic Panel",
            "Renal Function Review",
            "Q4 Metabolic and Renal Panel",
        ],
        "expected_facts": ["5.4", "5.8", "6.2", "6.8", "7.2"],
        "unexpected_facts": ["normal", "improving"],
        "min_chunks": 3,
        "notes": "HbA1c worsening from pre-diabetic to diabetic across all 5 lab visits",
    },
    {
        "id": "traj-04",
        "question": "Walk me through my kidney function journey since January",
        "category": "trajectory",
        "expected_route": "trajectory",
        "expected_source_titles": [
            "Annual Check-Up Lab Results",
            "Q4 Metabolic and Renal Panel",
        ],
        "expected_facts": ["January", "0.9", "2.1"],
        "unexpected_facts": [],
        "min_chunks": 3,
        "notes": "Open narrative; tests that the trajectory path spans Jan-to-Dec",
    },
    {
        "id": "traj-05",
        "question": "How fast is my creatinine rising each month?",
        "category": "trajectory",
        "expected_route": "trajectory",
        "expected_source_titles": [
            "Annual Check-Up Lab Results",
            "Mid-Year Metabolic Panel",
            "Q4 Metabolic and Renal Panel",
        ],
        "expected_facts": ["0.9", "2.1"],
        "unexpected_facts": [],
        "min_chunks": 3,
        "notes": "Rate-of-change question; answer should contain start/end values",
    },
    {
        "id": "traj-06",
        "question": "Is my blood sugar under control based on my recent results?",
        "category": "trajectory",
        "expected_route": "trajectory",
        "expected_source_titles": [
            "Q4 Metabolic and Renal Panel",
            "Renal Function Review",
            "Mid-Year Metabolic Panel",
        ],
        "expected_facts": ["7.2", "HbA1c"],
        "unexpected_facts": ["normal", "well controlled"],
        "min_chunks": 2,
        "notes": "HbA1c 7.2% indicates poorly controlled diabetes — answer must reflect this",
    },

    # ── Point-in-time lab questions ────────────────────────────────────────────
    {
        "id": "lab-01",
        "question": "What is my most recent creatinine value?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Q4 Metabolic and Renal Panel"],
        "expected_facts": ["2.1"],
        "unexpected_facts": ["0.9", "normal"],
        "min_chunks": 1,
        "notes": "Simple point-in-time — must return Dec 2025 value, not Jan baseline",
    },
    {
        "id": "lab-02",
        "question": "What was my eGFR at the September appointment?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Renal Function Review"],
        "expected_facts": ["48"],
        "unexpected_facts": ["35", "88"],
        "min_chunks": 1,
        "notes": "Date-specific retrieval — Sep 2025, eGFR 48",
    },
    {
        "id": "lab-03",
        "question": "What were my lab results in January 2025?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Annual Check-Up Lab Results"],
        "expected_facts": ["0.9", "5.4", "92", "88"],
        "unexpected_facts": ["2.1", "abnormal"],
        "min_chunks": 1,
        "notes": "Earliest visit — all values normal; answer should not mention later abnormals",
    },
    {
        "id": "lab-04",
        "question": "Were any of my December results flagged as critical?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Q4 Metabolic and Renal Panel"],
        "expected_facts": ["2.1", "35"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "Creatinine 2.1 and eGFR 35 were both marked is_critical=True",
    },
    {
        "id": "lab-05",
        "question": "What was my HbA1c in June?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Mid-Year Metabolic Panel"],
        "expected_facts": ["6.2"],
        "unexpected_facts": ["7.2", "5.4"],
        "min_chunks": 1,
        "notes": "Middle of the progression — HbA1c 6.2% in Jun 2025",
    },
    {
        "id": "lab-06",
        "question": "What is my cholesterol level?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Q4 Metabolic and Renal Panel"],
        "expected_facts": ["218"],
        "unexpected_facts": ["185"],
        "min_chunks": 1,
        "notes": "Most recent cholesterol is 218 (Dec), not 185 (Jan) — recency matters",
    },
    {
        "id": "lab-07",
        "question": "What was my fasting glucose in the mid-year check?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Mid-Year Metabolic Panel"],
        "expected_facts": ["118"],
        "unexpected_facts": ["92", "152"],
        "min_chunks": 1,
        "notes": "Glucose in June was 118 — not January (92) or December (152)",
    },
    {
        "id": "lab-08",
        "question": "Was my creatinine within the normal reference range in March?",
        "category": "lab_point",
        "expected_route": "lab_results",
        "expected_source_titles": ["Follow-Up Lab Panel"],
        "expected_facts": ["1.1"],
        "unexpected_facts": ["2.1", "critical"],
        "min_chunks": 1,
        "notes": "In March creatinine was 1.1 — at the upper boundary of normal (0.6-1.1)",
    },

    # ── Medication questions ────────────────────────────────────────────────────
    {
        "id": "med-01",
        "question": "What medications am I currently prescribed?",
        "category": "medication",
        "expected_route": "medications",
        "expected_source_titles": ["Insulin Glargine — New Prescription"],
        "expected_facts": ["insulin glargine", "10 units"],
        "unexpected_facts": ["metformin 1000mg"],
        "min_chunks": 1,
        "notes": "Current prescription is insulin; metformin was DISCONTINUED",
    },
    {
        "id": "med-02",
        "question": "When was my metformin dose increased?",
        "category": "medication",
        "expected_route": "medications",
        "expected_source_titles": ["Metformin Dose Adjustment"],
        "expected_facts": ["June", "1000mg"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "Dose increased in Jun 2025 — prescription record is the source",
    },
    {
        "id": "med-03",
        "question": "Why was metformin stopped?",
        "category": "medication",
        "expected_route": "medications",
        "expected_source_titles": ["Insulin Glargine — New Prescription"],
        "expected_facts": ["eGFR", "35", "contraindicated"],
        "unexpected_facts": ["normal", "no reason"],
        "min_chunks": 1,
        "notes": "Metformin contraindicated when eGFR < 45 — answer must mention eGFR threshold",
    },
    {
        "id": "med-04",
        "question": "What is my current insulin dose and when do I take it?",
        "category": "medication",
        "expected_route": "medications",
        "expected_source_titles": ["Insulin Glargine — New Prescription"],
        "expected_facts": ["10 units", "bedtime"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "Insulin glargine 10 units at bedtime — from Dec 2025 prescription",
    },
    {
        "id": "med-05",
        "question": "Are there any cautions mentioned about my medication and kidney function?",
        "category": "medication",
        "expected_route": "medications",
        "expected_source_titles": ["Metformin Dose Adjustment"],
        "expected_facts": ["renal", "eGFR", "creatinine"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "Jun 2025 prescription explicitly warns: monitor renal function",
    },
    {
        "id": "med-06",
        "question": "What medication was I put on instead of metformin?",
        "category": "medication",
        "expected_route": "medications",
        "expected_source_titles": ["Insulin Glargine — New Prescription"],
        "expected_facts": ["insulin", "glargine"],
        "unexpected_facts": ["metformin"],
        "min_chunks": 1,
        "notes": "Dec 2025 replacement: insulin glargine started when metformin discontinued",
    },

    # ── Diagnosis / clinical notes ─────────────────────────────────────────────
    {
        "id": "diag-01",
        "question": "What is my current CKD stage?",
        "category": "diagnosis",
        "expected_route": "diagnosis",
        "expected_source_titles": ["CKD Stage 3b Assessment and Management Plan"],
        "expected_facts": ["3b", "35"],
        "unexpected_facts": ["Stage 3a", "Stage 4"],
        "min_chunks": 1,
        "notes": "Dec 2025 nephrology confirmed CKD Stage 3b (eGFR 35)",
    },
    {
        "id": "diag-02",
        "question": "When was I first referred to a nephrologist?",
        "category": "diagnosis",
        "expected_route": "diagnosis",
        "expected_source_titles": ["Nephrology Referral — CKD Stage 3a"],
        "expected_facts": ["September", "2025"],
        "unexpected_facts": ["December", "June"],
        "min_chunks": 1,
        "notes": "Nephrology referral was September 2025, not December (follow-up) or earlier",
    },
    {
        "id": "diag-03",
        "question": "What is causing my kidney problems?",
        "category": "diagnosis",
        "expected_route": "diagnosis",
        "expected_source_titles": ["CKD Stage 3b Assessment and Management Plan"],
        "expected_facts": ["diabetic nephropathy", "diabetes"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "Aetiology in Dec nephrology note: diabetic nephropathy from Type 2 DM",
    },
    {
        "id": "diag-04",
        "question": "What is my blood pressure target?",
        "category": "diagnosis",
        "expected_route": "diagnosis",
        "expected_source_titles": ["CKD Stage 3b Assessment and Management Plan"],
        "expected_facts": ["130/80"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "BP target <130/80 mmHg is explicitly stated in management plan",
    },
    {
        "id": "diag-05",
        "question": "What are the key points of my management plan?",
        "category": "diagnosis",
        "expected_route": "diagnosis",
        "expected_source_titles": ["CKD Stage 3b Assessment and Management Plan"],
        "expected_facts": ["insulin", "diet", "kidney"],
        "unexpected_facts": [],
        "min_chunks": 1,
        "notes": "Management plan covers insulin, low-protein diet, BP control, avoid NSAIDs",
    },
    {
        "id": "diag-06",
        "question": "What was the first diagnosis related to my kidneys?",
        "category": "diagnosis",
        "expected_route": "diagnosis",
        "expected_source_titles": ["Nephrology Referral — CKD Stage 3a"],
        "expected_facts": ["Stage 3a", "September"],
        "unexpected_facts": ["Stage 3b"],
        "min_chunks": 1,
        "notes": "First kidney diagnosis was CKD 3a in Sep 2025; 3b was Dec 2025",
    },

    # ── General medical knowledge (no personal records needed) ─────────────────
    {
        "id": "gen-01",
        "question": "What is the normal range for creatinine in adults?",
        "category": "general",
        "expected_route": "general",
        "expected_source_titles": [],
        "expected_facts": ["0.6", "1.1"],
        "unexpected_facts": [],
        "min_chunks": 0,
        "notes": "General knowledge — reference range from population data, not Sara's records",
    },
    {
        "id": "gen-02",
        "question": "What does CKD Stage 3 mean?",
        "category": "general",
        "expected_route": "general",
        "expected_source_titles": [],
        "expected_facts": ["eGFR", "30", "60"],
        "unexpected_facts": [],
        "min_chunks": 0,
        "notes": "CKD Stage 3: eGFR 30-59; should come from general knowledge base",
    },
    {
        "id": "gen-03",
        "question": "What is HbA1c and why is it measured?",
        "category": "general",
        "expected_route": "general",
        "expected_source_titles": [],
        "expected_facts": ["blood sugar", "glucose", "diabetes"],
        "unexpected_facts": [],
        "min_chunks": 0,
        "notes": "General explanation of HbA1c — blood glucose control over 3 months",
    },
    {
        "id": "gen-04",
        "question": "What is eGFR and what does a low value mean for kidney function?",
        "category": "general",
        "expected_route": "general",
        "expected_source_titles": [],
        "expected_facts": ["kidney", "filter"],
        "unexpected_facts": [],
        "min_chunks": 0,
        "notes": "General eGFR explanation — glomerular filtration rate, kidney function marker",
    },
]
