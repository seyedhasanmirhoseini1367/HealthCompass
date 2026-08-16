"""
External processing guard — the single boundary where patient data may leave
HealthCompass.

Every outbound call that carries patient data is registered here as a named
EgressPoint, and every such call site asks this module for permission first.
Registering the points in one table (rather than scattering `if not
has_consent(...)` through the codebase) means the answer to "what do we send,
to whom, under which consent?" is readable in one place — which is also exactly
what a privacy review or a records-of-processing entry needs.

Not every external call needs consent. `knowledge.embed` sends curated public
clinical articles with no patient involvement, so it is registered with
purpose=None and is deliberately never blocked. The consent requirement follows
the data, not the fact that an HTTP request leaves the process.

Enforcement is per-point and controlled by settings.CONSENT_ENFORCED_EGRESS so a
deployment can stage the rollout; see that setting for the reasoning.
"""
from dataclasses import dataclass
from typing import Dict, Optional

from django.conf import settings

from .consent import ConsentRequired, has_consent
from .models import ConsentPurpose


@dataclass(frozen=True)
class EgressPoint:
    """One place where data crosses the boundary out of HealthCompass."""
    id:               str
    provider:         str
    data_category:    str
    phi:              bool
    patient_specific: bool
    purpose:          Optional[str]   # None → no consent required
    note:             str = ''

    @property
    def requires_consent(self) -> bool:
        return self.purpose is not None


def _p(id, provider, data_category, phi, patient_specific, purpose, note=''):
    return EgressPoint(id, provider, data_category, phi, patient_specific, purpose, note)


EXTERNAL = ConsentPurpose.EXTERNAL_LLM

#: Every outbound call carrying data of any kind. Keep this exhaustive — an
#: unregistered egress point is an unreviewed one.
EGRESS_POINTS: Dict[str, EgressPoint] = {p.id: p for p in [

    # ── RAG pipeline ─────────────────────────────────────────────────────────
    _p('rag.generation', 'Groq / Google / Anthropic / OpenAI',
       'retrieved record excerpts + the patient question', True, True, EXTERNAL,
       'Reached only via RAGService, which gates before the pipeline starts.'),

    _p('rag.embed_documents', 'Google (Gemini embeddings)',
       'text of the patient\'s own records, chunked', True, True, EXTERNAL,
       'Triggered by UPLOAD via the post_save signal, not by asking a question — '
       'so it is not covered by the RAGService gate.'),

    _p('rag.embed_query', 'Google (Gemini embeddings)',
       'the patient question', True, True, EXTERNAL),

    _p('rag.rerank', 'Groq',
       'up to 300 characters of each candidate record chunk', True, True, EXTERNAL),

    _p('rag.classify', 'Groq',
       'the patient question plus recent conversation turns', True, True, EXTERNAL,
       'A health question is itself health data, even before any record is attached.'),

    # ── Document ingestion ───────────────────────────────────────────────────
    _p('records.parse', 'Google (Gemini)',
       'extracted text of an uploaded medical document', True, True, EXTERNAL,
       'Degrades to regex extraction when not permitted.'),

    _p('records.ocr', 'Google (Gemini vision)',
       'the uploaded document image itself', True, True, EXTERNAL,
       'No local OCR fallback exists; the operation cannot proceed without consent.'),

    # ── Predictions / insights ───────────────────────────────────────────────
    _p('insights.interpretation', 'Groq / Google',
       'model inputs and the patient\'s risk result', True, True, EXTERNAL,
       'Degrades to the built-in static interpretation when not permitted.'),

    _p('insights.seizure_proxy', 'hasanai.net (third party, not an LLM vendor)',
       'the raw uploaded EEG signal file and its filename', True, True, EXTERNAL,
       'Sent unauthenticated; retention by the receiving service is undocumented. '
       'See docs/ARCHITECTURE.md — External Data Egress.'),

    _p('insights.seizure_interpretation', 'Google / Anthropic',
       'seizure classification result for this recording', True, True, EXTERNAL),

    # ── Not patient data ─────────────────────────────────────────────────────
    _p('knowledge.embed', 'Google (Gemini embeddings)',
       'curated public clinical articles (Käypä hoito, Terveyskirjasto, THL)',
       False, False, None,
       'No patient involvement — run by an operator indexing public reference '
       'material. Deliberately never blocked.'),
]}


#: Points that were already effectively enforced by the RAGService gate before
#: this registry existed — everything reached *by asking a question*. Listed
#: explicitly rather than matched on the `rag.` prefix, because
#: `rag.embed_documents` is reached by *uploading a record* and so was never
#: covered by that gate. Treating it as pre-enforced would silently turn on new
#: production blocking the moment this registry shipped.
RAG_PIPELINE_POINTS = frozenset({
    'rag.generation',
    'rag.embed_query',
    'rag.rerank',
    'rag.classify',
})


class ExternalProcessingGuard:
    """
    Permission check for a registered egress point.

    Usage at a call site, always BEFORE the data is handed to the provider:

        ExternalProcessingGuard.check(user, 'records.ocr')   # raises
        if ExternalProcessingGuard.allows(user, 'records.parse'):  # degrades
    """

    @staticmethod
    def point(point_id: str) -> EgressPoint:
        try:
            return EGRESS_POINTS[point_id]
        except KeyError:
            # Fail loud: an unregistered id means a call site was added without
            # a privacy decision being recorded for it.
            raise ValueError(f'Unregistered egress point: {point_id!r}')

    @staticmethod
    def is_enforced(point_id: str) -> bool:
        """Whether this deployment currently blocks on *point_id*."""
        point = ExternalProcessingGuard.point(point_id)
        if not point.requires_consent:
            return False
        enforced = getattr(settings, 'CONSENT_ENFORCED_EGRESS', [])
        if 'all' in enforced:
            return True
        if 'rag' in enforced and point_id in RAG_PIPELINE_POINTS:
            return True
        return point_id in enforced

    @staticmethod
    def allows(user, point_id: str) -> bool:
        """
        True when the transfer may proceed.

        Points that need no consent, and points not currently enforced by this
        deployment, return True. Otherwise the user must hold current consent.
        """
        point = ExternalProcessingGuard.point(point_id)
        if not point.requires_consent:
            return True
        if not ExternalProcessingGuard.is_enforced(point_id):
            return True
        return has_consent(user, point.purpose)

    @staticmethod
    def check(user, point_id: str) -> None:
        """Raise ConsentRequired unless the transfer may proceed."""
        if ExternalProcessingGuard.allows(user, point_id):
            return
        point = ExternalProcessingGuard.point(point_id)
        raise ConsentRequired(point.purpose, _denial_message(point))


# ── Which providers may receive PHI at all ────────────────────────────────────
#
# Consent is granted per PURPOSE ("sending my health data to external AI
# providers"), not per company. The generation fallback chain is Groq → Gemini →
# Anthropic → OpenAI, so that single consent authorised four organisations, and
# which one actually received a patient's records depended on nothing more than
# which API key happened to be configured and which provider answered first.
#
# A patient agreeing to "external AI processing" has not agreed to an arbitrary
# set of vendors chosen at runtime. This is the server-side half of the answer:
# an allowlist the deployment controls, enforced inside the fallback loop so a
# provider that is not on it is skipped rather than tried.
#
# The default is every provider the code already used, so this changes no
# behaviour on its own. What it changes is that the set is now stated, and
# restricting it is a configuration change rather than a code change.
DEFAULT_PHI_LLM_PROVIDERS = ('groq', 'gemini', 'anthropic', 'openai')


def phi_llm_providers() -> frozenset:
    """The providers this deployment permits to receive patient data."""
    configured = getattr(settings, 'PHI_LLM_PROVIDERS', None)
    if configured is None:
        configured = DEFAULT_PHI_LLM_PROVIDERS
    return frozenset(str(p).strip().lower() for p in configured if str(p).strip())


def phi_provider_allowed(provider: str) -> bool:
    """
    Whether *provider* may be handed patient data.

    Asked inside the fallback loop rather than once before it: a chain that
    checked only its first choice would fall through to an unvetted provider the
    moment the permitted one was slow or down, which is exactly when nobody is
    watching.
    """
    return (provider or '').strip().lower() in phi_llm_providers()


def _denial_message(point: EgressPoint) -> str:
    return (
        f'This step sends your health data to {point.provider} for processing, '
        f'which needs your consent. You can grant or withdraw it at any time in '
        f'Privacy & Consent settings.'
    )


def egress_matrix() -> list:
    """The full table, for documentation and for the privacy settings UI."""
    return [{
        'id':               p.id,
        'provider':         p.provider,
        'data_category':    p.data_category,
        'phi':              p.phi,
        'patient_specific': p.patient_specific,
        'purpose':          p.purpose,
        'requires_consent': p.requires_consent,
        'enforced':         ExternalProcessingGuard.is_enforced(p.id),
        'note':             p.note,
    } for p in EGRESS_POINTS.values()]
