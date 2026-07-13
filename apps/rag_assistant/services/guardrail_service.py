# rag_assistant/services/guardrail_service.py
"""
Safety Guardrail Service — post-generation response validation.

Problem (from PhD proposal):
  LLMs in clinical contexts sometimes generate specific medication dosage
  recommendations, definitive diagnoses, or alarming statements without
  adequate safety qualifiers. For a patient-facing health assistant this
  is unsafe and potentially harmful.

Solution (Safety Layer contribution):
  After generation, apply pattern-matching rules to detect three classes
  of unsafe content and append mandatory safety qualifiers.

Rules:
  1. DOSAGE_RECOMMENDATION — response tells patient to take a specific dose
     → append strong medication disclaimer
  2. DEFINITIVE_DIAGNOSIS  — response states patient *has* a condition
     → soften language + append diagnosis disclaimer
  3. EMERGENCY_INDICATOR   — response uses emergency language
     → append urgent care prompt

All rule triggers are logged for monitoring (visible in the MLOps dashboard
via the query log — future extension: store triggered_rules in QueryLog).
"""
import logging
import re
from typing import List, Tuple

logger = logging.getLogger(__name__)


# ── Pre-query emergency patterns (fire BEFORE RAG pipeline) ───────────────────
# These match acute symptoms and self-harm intent that must never enter retrieval.

_PRE_QUERY_EMERGENCY_RE = re.compile(
    r'\b('
    r"chest pain|chest tightness|shortness of breath|can't breathe|cannot breathe|"
    r"can't seem to breathe|struggling to breathe|"
    r'heart attack|stroke|seizure|'
    r'loss of consciousness|lost consciousness|unconscious|fainted|fainting|'
    r'severe bleeding|bleeding heavily|'
    r'overdose|overdosed|accidental poisoning|'
    r'suicidal|want to die|kill myself|'
    r'end my life|ending my life|'
    r'hurt myself|hurting myself|harm myself|harming myself|'
    r"don't want to be alive|do not want to be alive|"
    r'self.?harm|self harm|cutting myself|'
    r'need an ambulance|call an ambulance'
    r')\b',
    re.IGNORECASE,
)

_EMERGENCY_GATE_RESPONSE = """\
⚠️ **This sounds like it may be a medical emergency.**

If you or someone nearby is in immediate danger or experiencing acute symptoms, please act now:

- 🚨 **Call emergency services immediately** — 911 (US) · 999 (UK) · 112 (EU)
- 🏥 **Go to your nearest Emergency Department** — do not wait
- 📞 **Do not rely on AI** — I cannot provide emergency medical care

---

**Mental health crisis?**
- 🇺🇸 US: Call or text **988** (Suicide & Crisis Lifeline, 24/7)
- 🇬🇧 UK: Call **116 123** (Samaritans, free, 24/7)
- 🌍 International: [findahelpline.com](https://findahelpline.com)

---

*I'm an AI health assistant and cannot respond to emergencies. \
Please seek immediate professional help.*"""


# ── Post-generation pattern definitions ──────────────────────────────────────

# Rule 1: Specific numeric dosage recommendation
# Matches: "take 500mg of metformin", "increase to 10 units", "reduce by 2mg"
_DOSAGE_RE = re.compile(
    r'\b(take|start|begin|increase|decrease|reduce|stop|discontinue|'
    r'double|halve|switch to)\b'
    r'.{0,40}'
    r'\b\d+\s*(mg|mcg|ug|ml|iu|units?|tablets?|capsules?|pills?|drops?)\b',
    re.IGNORECASE,
)

# Rule 2: Definitive diagnosis statement
# Matches: "you have diabetes", "you are developing kidney failure"
_DIAGNOSIS_CONDITIONS = (
    r'diabetes|hypertension|kidney failure|renal failure|heart failure|'
    r'ckd|chronic kidney disease|liver cirrhosis|cancer|tumou?r|'
    r'hypothyroidism|hyperthyroidism|anemia|anaemia|stroke|'
    r'coronary artery disease|atrial fibrillation|sepsis'
)
_DIAGNOSIS_RE = re.compile(
    r'\byou\s+(have|are developing|are suffering from|'
    r'are diagnosed with|definitely have|now have|clearly have)\b'
    r'.{0,80}'
    r'\b(?:' + _DIAGNOSIS_CONDITIONS + r')\b',
    re.IGNORECASE,
)
# Pattern used to soften the language in-place.
# Lookahead requires the condition name within 80 chars so that benign
# phrases like "you have three lab results" are not touched.
_DIAGNOSIS_SOFTEN_RE = re.compile(
    r'\byou\s+(have|are developing|are suffering from|'
    r'are diagnosed with|definitely have|now have|clearly have)\b'
    r'(?=.{0,80}\b(?:' + _DIAGNOSIS_CONDITIONS + r')\b)',
    re.IGNORECASE | re.DOTALL,
)

# Rule 3: Emergency / alarming language
_EMERGENCY_RE = re.compile(
    r'\b(medical emergency|call 911|call 999|call an ambulance|'
    r'go to.*(?:er|emergency room|a&e|accident and emergency)|'
    r'seek immediate|immediately seek|critically abnormal|'
    r'life.?threatening|dangerously (high|low))\b',
    re.IGNORECASE,
)


# ── Disclaimer texts ───────────────────────────────────────────────────────────

_DOSAGE_DISCLAIMER = (
    "\n\n> **Medication Safety Note:** The dosage information above is for "
    "educational context only. Never adjust, start, or stop any medication "
    "without first consulting your prescribing physician or a licensed "
    "pharmacist. Incorrect dosing can be dangerous."
)

_DIAGNOSIS_DISCLAIMER = (
    "\n\n> **Diagnostic Note:** Only a qualified healthcare professional "
    "performing a full clinical assessment can make or confirm a diagnosis. "
    "Lab values alone are not sufficient for a definitive diagnosis. "
    "Please discuss these results with your doctor."
)

_EMERGENCY_DISCLAIMER = (
    "\n\n> **Urgent Reminder:** If you are currently experiencing acute "
    "symptoms alongside these results, please contact your doctor immediately "
    "or go to the nearest emergency department. Do not delay care."
)

_SOFT_CONSULT_REMINDER = (
    "\n\n*Always consult your healthcare provider before making any "
    "medical decisions based on this information.*"
)


# ── Service class ──────────────────────────────────────────────────────────────

class GuardrailService:
    """
    Applies post-generation safety rules to LLM responses.

    Usage:
        safe_text, rules_fired = GuardrailService().apply(response_text)
    """

    # ── Pre-query safety gate (call BEFORE retrieval) ────────────────────────
    @staticmethod
    def check_pre_query(query: str) -> Tuple[bool, str]:
        """
        Check the raw user query BEFORE any RAG pipeline fires.

        Returns:
            (is_emergency, emergency_response)
            is_emergency   — True when emergency / self-harm language detected
            emergency_response — ready-to-display markdown string (empty if safe)
        """
        if _PRE_QUERY_EMERGENCY_RE.search(query):
            logger.warning('Pre-query safety gate triggered: emergency/self-harm detected')
            return True, _EMERGENCY_GATE_RESPONSE
        return False, ''

    def apply(self, response: str) -> Tuple[str, List[str]]:
        """
        Validate *response* against all safety rules.

        Returns:
            safe_response   — response with any needed disclaimers appended
            triggered_rules — list of rule names that fired (for logging)
        """
        triggered: List[str] = []

        # ── Rule 1: Dosage recommendation ──────────────────────────────────
        if _DOSAGE_RE.search(response):
            response += _DOSAGE_DISCLAIMER
            triggered.append('dosage_recommendation')
            logger.info('Guardrail fired: dosage_recommendation')

        # ── Rule 2: Definitive diagnosis ────────────────────────────────────
        if _DIAGNOSIS_RE.search(response):
            # Soften the language in-place before appending disclaimer
            response = _DIAGNOSIS_SOFTEN_RE.sub(
                lambda m: f'your results may suggest you {m.group(1)}',
                response,
            )
            response += _DIAGNOSIS_DISCLAIMER
            triggered.append('definitive_diagnosis')
            logger.info('Guardrail fired: definitive_diagnosis')

        # ── Rule 3: Emergency language ──────────────────────────────────────
        if _EMERGENCY_RE.search(response):
            response += _EMERGENCY_DISCLAIMER
            triggered.append('emergency_indicator')
            logger.info('Guardrail fired: emergency_indicator')

        # ── Soft reminder ────────────────────────────────────────────────────
        # Only add generic reminder when no stronger disclaimer was injected
        # AND the response doesn't already end with one.
        if not triggered:
            tail = response[-400:]
            already_reminded = bool(re.search(
                r'(consult|healthcare provider|your doctor|physician|medical advice)',
                tail,
                re.IGNORECASE,
            ))
            if not already_reminded:
                response += _SOFT_CONSULT_REMINDER

        return response, triggered
