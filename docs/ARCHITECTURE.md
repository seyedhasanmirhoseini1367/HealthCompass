# HealthCompass — As-Built Architecture

> **Status:** Phase 1 audit, generated 2026-08-12. Describes what the repository
> *actually contains*, not what the implementation plan proposes.
> Companion documents: [MODEL_MAPPING.md](MODEL_MAPPING.md), [PLAN_STATUS.md](PLAN_STATUS.md).

---

## 1. Scope of this repository

This repo is the **Django backend + server-rendered web application**. It also
exposes a JWT-authenticated REST API under `/api/v1/` consumed by a **mobile
client that lives in a separate repository**.

Consequently:

- Mobile *implementation* (local DB, offline mode, biometrics, capture UI) is **out of scope here**.
- Mobile-*facing* backend concerns — API contract, JWT auth, ownership checks,
  sync/offline affordances, push notifications — **are in scope**.

## 2. Stack

| Layer | Technology | Evidence |
|---|---|---|
| Framework | Django 5.2, Python 3.11 | `requirements.txt:4`, `.python-version` |
| Database | PostgreSQL via `dj-database-url`; SQLite fallback for dev | `healthcompass/settings.py:95-100` |
| Web UI | Django templates + WhiteNoise static | `settings.py:73-88`, `settings.py:121` |
| API | DRF + SimpleJWT + django-cors-headers | `settings.py:319-343` |
| Auth | Custom user + `EmailOrUsernameBackend` + allauth (Google OAuth) | `settings.py:20-24`, `settings.py:237-245` |
| Object storage | Optional S3-compatible (Cloudflare R2 / MinIO); falls back to local `MEDIA_ROOT` | `settings.py:135-148` |
| Cache / rate-limit backend | Redis when `CACHE_URL` set, else LocMem | `settings.py:254-267` |
| Orchestration | LangGraph 0.2.28 | `requirements.txt:23`, `apps/rag_assistant/graph/` |
| Embeddings | Gemini `models/gemini-embedding-001` (3072-dim) | `settings.py:185` |
| Generation | Groq → Gemini → Anthropic → OpenAI fallback chain | `settings.py:205-208` |
| ML inference | ONNX Runtime (data-scientist model marketplace) | `requirements.txt:9`, `apps/ai_insights/inference/` |
| Push | Firebase Cloud Messaging | `settings.py:181`, `apps/notifications/firebase.py` |
| Deploy | Railway (gunicorn, `startup.sh`, `railway.toml`, `railway.cron.toml`) | repo root |

## 3. Django apps

```
apps/
├── accounts/         5-role identity, profiles, doctor↔patient links, emergency card
├── medical_records/  MedicalRecord ingestion (PDF / text / Kanta XML / wearable CSV / OCR scan)
├── ai_insights/      ONNX model marketplace, predictions, health alerts, ICU & seizure demos
├── rag_assistant/    Vector store, retrieval, LangGraph pipeline, chat sessions, guardrails
├── dashboard/        Landing page, patient dashboard, doctor monitoring views
├── notifications/    In-app notifications + FCM device registry
├── appointments/     Appointments with tiered reminder dispatch
└── api/              Mobile REST API (models.py is empty — serializers over other apps)
```

## 4. Data model (as built)

### 4.1 Identity — `apps/accounts/models.py`

- **`CustomUser`** (`AbstractUser`) — `role` ∈ {patient, doctor, data_scientist,
  hospital_admin, admin}, `email` unique+nullable, `is_approved` gate for
  professional roles, `date_of_birth`, `phone_number`, `profile_picture`.
- **Four profile models**, each `OneToOneField` → user: `PatientProfile`,
  `DoctorProfile`, `DataScientistProfile`, `HospitalAdminProfile`.
- **`PatientProfile`** additionally holds `national_id` as an
  `EncryptedCharField` (Fernet, key derived from `SECRET_KEY`) plus the public
  emergency-card controls `emergency_token` (UUID) and `emergency_card_enabled`.
- **`Consent`** — append-only record of consent decisions, one active row per
  (user, purpose) enforced by a partial unique constraint. See §7.
- **`PatientDoctorRelationship`** — `unique_together (patient, doctor)`, `is_active`,
  `linked_by`. This is the authorization edge for doctor access.
- **`DoctorAccessLog`** — append-only record of doctor→patient data access.
- **`EmergencyCardView`** — append-only log of public emergency-card views;
  stores `ip_hash` (SHA-256), never a raw IP.

### 4.2 Clinical data — `apps/medical_records/models.py`

- **`MedicalRecord`** (UUID PK) — `record_type` ∈ {lab_result, prescription,
  diagnosis, vaccination, imaging, wearable, discharge, other};
  `source` ∈ {kanta_xml, wearable_csv, manual_upload}; `file`, `raw_text`,
  `parsed_data` (JSON), `record_date`, `uploaded_at`, `updated_at`, `is_flagged`.
  Indexed on `(patient, -record_date)`.
- **`ParsedLabValue`** — per-analyte rows with a genuine unit-normalization layer:
  `canonical_value`, `original_unit`, `unit_known`. Rows with `unit_known=False`
  are excluded from trajectory/threshold comparison. Also `reference_range`,
  `is_abnormal`, `is_critical`, `measured_at`.
- **`WearableDataPoint`** — `metric`, `value`, `unit`, `recorded_at`.

### 4.3 RAG store — `apps/rag_assistant/models.py`

- **`MedicalDocument`** (UUID PK) — one logical document per record per patient;
  FK to `MedicalRecord` (nullable), `document_type`, `content`, `metadata` JSON.
- **`MedicalChunk`** (UUID PK) — `content`, `chunk_index`,
  `embedding` as a `BinaryField` (float32 bytes **stored in the database**,
  not in an external index), `metadata` JSON.
  `unique_together (document, chunk_index)`.
  Carries **embedding provenance** via `EmbeddingProvenanceMixin`:
  `embedding_model`, `embedding_model_version`, `embedding_dimensions`,
  `embedded_at` (see §6).
- **`GeneralKnowledgeChunk`** — curated Finnish public sources
  (Käypä hoito, Terveyskirjasto, THL), same embedding representation.
- **`ChatSession`** / **`QueryLog`** — conversation storage. `QueryLog` holds a
  **query+response pair in a single row**, plus observability fields:
  `sources` (JSON), `llm_provider`, `retrieved_chunks_count`, `safety_routed`,
  `triggered_rules`, `query_mode`, `chart_data`.

> `rag_vector_store/` on disk is **empty**. All vectors live in
> `MedicalChunk.embedding` / `GeneralKnowledgeChunk.embedding`, which means
> `ON DELETE CASCADE` from `MedicalRecord` already removes embeddings — there is
> no orphaned external index to reconcile.

### 4.4 Insights — `apps/ai_insights/models.py`

`AIModel` (submitted by data scientists, ONNX file, approval workflow,
`input_schema`/`output_schema`/`handler_config` JSON) → `ModelPrediction`
(per-patient run with `risk_score` and LLM `interpretation`) → `HealthAlert`
(severity, optional `source_record` FK).

### 4.5 Support — appointments / notifications

`Appointment` (UUID PK, per-offset `remind_*` / `reminded_*` boolean pairs
driven by a Railway cron), `Notification`, `FCMDevice`.

## 5. RAG pipeline (as built)

`apps/rag_assistant/graph/graph.py` defines a single LangGraph `StateGraph`:

```
safety_gate_node
   ├─(blocked)─────────────────────────────────► END
   └─► understand_node ─► router_node
                            ├─► cold_start_node
                            ├─► trajectory_node
                            ├─► lab_results_node
                            ├─► medications_node
                            ├─► wearable_node
                            ├─► diagnosis_node
                            ├─► records_node
                            └─► general_node
                                     │
                                     ▼
                                    END
```

The graph stops before generation. `stream_graph()` then calls
`generate_streaming()` directly, so tokens can only come from generation and
never from an internal node such as QueryUnderstanding.

A fuller graph with `generate_node`, `verify_node` and a retry-on-empty-retrieval
loop used to be compiled alongside this one. Nothing invoked it, so its
verify/retry step never ran; it has been removed. Empty retrieval is not retried
anywhere today.

Supporting services in `apps/rag_assistant/services/`:

| Service | Responsibility |
|---|---|
| `document_processor.py` | Record → `MedicalDocument` → word-window chunks (200 words / 40 overlap) |
| `embedding_service.py` | Gemini embedding calls, batch indexing, per-patient vector loading |
| `retrieval_service.py` | Hybrid BM25 + cosine, intent-weighted blending, time decay, context-type boost, MMR, optional LLM rerank |
| `query_understanding.py` | Intent classification (keyword-first, LLM fallback), biomarker detection, follow-up detection |
| `trajectory_service.py` | Temporal/longitudinal reasoning, trend computation, Chart.js payloads, reference bands |
| `general_knowledge_service.py` | Retrieval over curated Finnish clinical sources |
| `generation_service.py` | 4-provider fallback, 4 system prompts (personal / general / hybrid / trajectory), sync + streaming |
| `guardrail_service.py` | `check_pre_query` safety gate + post-generation disclaimer/rule application |

Retrieval tuning lives entirely in `RAG_CONFIG` (`settings.py:184-211`).

## 5b. RAG pipeline — the actual request path

Traced 2026-08-12. Every stage below is a real call site, not an intended design.

```
question
  │
  ├─ RAGService.ask/stream_ask ......... consent gate (external_llm)
  ▼
safety_gate_node .................... GuardrailService.check_pre_query — deterministic
  │                                    rules; emergency/self-harm ends here, pre-LLM
  ▼
understand_node ..................... query_understanding.understand()
  │                                    keyword-first; Groq LLM only for short follow-ups
  │                                    → mode, route, intent, is_temporal, biomarker
  ▼
router_node ......................... picks ONE domain node from the route
  │                                    cold-start override when no chunks are indexed
  ▼
domain node (one of 8)
  │  trajectory ..... TrajectoryService — ORM over ParsedLabValue, ordered by measured_at
  │  lab_results / medications / wearable / diagnosis / records
  │              ..... RetrievalService.retrieve(patient, query, document_type=…)
  │  general ........ GeneralKnowledgeService — curated public corpus
  │  cold_start ..... population reference text, no patient data
  ▼
RetrievalService.retrieve  (the non-trajectory path)
  1. load_patient_embeddings(patient)   scoped by patient; incompatible vectors dropped
  2. embed(query)                        Gemini RETRIEVAL_QUERY
  3. BM25 over chunk text                rank_bm25, max-normalised
  4. cosine over the vector matrix
  5. hybrid = α·BM25 + β·cosine          α,β chosen by intent (INTENT_WEIGHTS)
  6. time decay                          only for records older than 365 days, ≤15%
  7. context-type boost                  +0.08 when doc_type matches intent
  8. threshold                           drop below SIM_THRESHOLD (0.15)
  9. MMR                                 diversity, λ=0.6, widened to top_k×5
 10. LLM rerank                          Groq; falls back to Stage-1 order on any error
  ▼
generate_streaming() ................ _resolve_context_and_prompt → one of 4 system
  │                                    prompts; context fenced as untrusted data
  │                                    Groq → Gemini → Anthropic → OpenAI → static
  ▼
guardrail softening ................. diagnosis language softened across the whole
  │                                    stream; disclaimers appended once, at the end
  ▼
answer + sources (deduplicated per document)
```

**Configurable** (`settings.RAG_CONFIG`): `CHUNK_SIZE` 200, `CHUNK_OVERLAP` 40,
`TOP_K` 6, `BM25_WEIGHT` 0.35, `SEMANTIC_WEIGHT` 0.65, `TIME_DECAY_DAYS` 365,
`TIME_DECAY_FACTOR` 0.15, `MMR_LAMBDA` 0.6, `SIM_THRESHOLD` 0.15,
`INTENT_WEIGHTS`, `CONTEXT_TYPE_BOOST` 0.08, `RERANK_RECALL_FACTOR` 5,
`EMBEDDING_MODEL`, `EMBEDDING_DIM`, provider timeouts.

**Not configurable** — hardcoded in module constants: the temporal/route/intent
keyword lists, the biomarker alias table, reference bands, and the chunk
splitter's word-window strategy.

## 5c. RAG evaluation — baseline

Two complementary harnesses:

| Harness | Needs LLM | Measures | Output |
|---|---|---|---|
| `scripts/evaluation/run_deterministic_eval.py` | no | classification & routing over the eval dataset | `evaluation/rag_deterministic_baseline.json` |
| `scripts/evaluation/eval_harness.py --rag-quality` | yes | context precision/recall, answer completeness, safety interception | `evaluation/rag_quality_results.json` |
| `apps/rag_assistant/test_rag_eval.py` | no | 31 deterministic regression tests, part of the normal suite | — |

The dataset is `scripts/evaluation/rag_eval_dataset.py`: 15 property-based cases
across temporal-latest, temporal-previous, temporal-trend, factual, unanswerable,
conflicting, attribution and injection. Expectations are properties
(`must_contain`, `should_refuse`, `expected_newest`), never exact sentences.

### Deterministic metrics — before / after optimization (2026-08-12)

| Metric | Before | After |
|---|---|---|
| Route accuracy (8 asserted cases) | 25.0% | **100.0%** |
| Temporal recognition (9 asserted) | 22.2% | **100.0%** |
| `temporal_latest` route accuracy | 0% | **100%** |
| `temporal_previous` route accuracy | 0% | **100%** |
| `temporal_trend` route accuracy | 100% | 100% (unchanged) |
| Biomarker detection | 10/15 | 10/15 (unchanged) |

Non-temporal routes were re-checked and did not move: `factual`,
`unanswerable`, and both `injection` cases keep the routes they had before.

### Temporal classification — how recency is decided

`is_temporal` answers *whether* time matters; `temporal_mode` answers *which*
question was asked:

| Mode | Trigger | Context produced |
|---|---|---|
| `trend` | "over time", "getting worse", "history of my" | full ordered series + trend analysis |
| `latest` | "latest", "most recent", "current", "newest" | series **plus** an explicit line naming the newest value |
| `previous` | "previous", "prior", "the one before" | series **plus** a line naming the second-newest |
| `None` | not temporal | — |

Two guards keep this from over-firing:

- **Patient context required.** Recency words only count when the query names
  the patient ("my") or a biomarker, so "what are the latest clinical
  guidelines" stays a general-knowledge question.
- **Trajectory must be able to order it.** A recency question about medications
  or diagnoses keeps its own route, because trajectory reasons over numeric
  `ParsedLabValue` series and prescriptions are not one. It still carries the
  temporal flag into retrieval.

### Conflict detection

`services/conflict_service.py` groups a patient's lab values by analyte and
classifies each group deterministically:

| Status | Rule | Surfaced to the model? |
|---|---|---|
| `progression` | same analyte, **different** dates, different values | no — this is history, and the trajectory already shows it |
| `conflict` | same analyte, **same** date, different values | yes — both values with their sources |
| `duplicate` | same analyte, same date, same value | no |

No clinical rules are invented: there are no thresholds, no reference ranges and
no significance judgements. Values whose unit the normaliser could not resolve
(`unit_known=False`) are treated as incomparable and never reported as a
conflict. The notice states the disagreement and its sources and stops — nothing
in the data says which record is right.

### Chunk provenance

Every chunk records `start_offset` / `end_offset` — the exact character range in
`MedicalDocument.content` — and citations carry them when present. **Page and
section are deliberately absent:** the PDF parser joins pages into one string
before chunking, so page boundaries no longer exist at that point, and a
citation pointing at the wrong page is harder to catch than one offering none.

Continuation chunks (`chunk_index > 0`) are prefixed with a short
`[Title — date] (continued)` header, so a chunk reading "Glucose: 105 mg/dL"
still says when it was measured. Chunk boundaries are also nudged backwards
rather than ending on a label, so "Glucose:" is never separated from its value.

### LLM-dependent — golden dataset (30 items) — VALID

Run 2026-08-12 against the current implementation via
`eval_harness.py --rag-quality`. All 30 cases retrieved context, so these
numbers are usable.

| Metric | 2026-07-16 | 2026-08-12 |
|---|---|---|
| Context recall | 96.7% | 96.7% |
| Context precision | 32.8% | 32.8% |
| Answer completeness | 82.8% | **87.4%** |
| Question coverage | 86.9% | 84.4% |
| Route accuracy | 66.7% | 63.3% |
| Hallucination flags | 8 | 9 |

Retrieval metrics are identical because nothing was re-indexed — the stored
chunks are unchanged, so R3/R4 cannot yet affect them.

The route-accuracy drop is **one case**: `lab-01` *"What is my most recent
creatinine value?"* now routes `trajectory` instead of `lab_results`. That is
what R1 was built to do; the golden dataset was written before R1 and still
encodes the old expectation. Treat it as a stale label, not a behaviour
regression — but it is reported as measured.

### LLM-dependent — new dataset (15 items) — ⚠ PARTIALLY INVALID, DO NOT CITE

Run 2026-08-12 via `run_llm_eval.py`. **The Gemini free-tier embedding quota
(1,000 requests/day) was exhausted mid-run**, so 5 of 15 cases retrieved zero
chunks. Results are recorded in `evaluation/rag_llm_eval_results.json` with a
`validity` block marking exactly which cases are usable.

| Case group | Chunks | Status |
|---|---|---|
| 7 temporal cases | 2–8 | **valid** — the trajectory path is ORM-based and needs no query embedding |
| `unanswerable-future`, `temporal-newest-abnormal` | 5–8 | valid |
| `factual-glucose-any`, `unanswerable-no-such-test`, `conflict-medication-status`, both `injection-*` | **0** | **INVALID** |

**The injection and unanswerable "passes" are not quality measurements.** With
zero retrieved context there was no injected content to resist and nothing to
refuse from — the model declined because it had been given nothing. Citing those
results as evidence of prompt-injection resistance or refusal quality would be
wrong. Prompt-injection defence is separately covered by deterministic tests in
`apps/rag_assistant/test_prompt_injection.py`, which do not depend on quota.

`conflict-medication-status` also failed with an all-providers-unavailable
fallback message; that is an infrastructure artefact, not a conflict-handling
result.

### Known flaw in the temporal scoring rule — fix before the next run

The three `temporal_latest` cases were scored as failures, but the answers were
**correct**:

> *"your most recent glucose measurement is 7.8 mmol/L … rising from 5.1 mmol/L
> in March 2024 to 7.8 in May 2026"*

The dataset declares `must_not_contain: ['5.1']`, but R1b **deliberately**
includes the full series alongside the latest value, so mentioning the older
reading is correct behaviour. The expectation contradicts the implemented
design.

**Correction to apply before re-running** (not applied — the numbers cannot be
re-verified until quota returns):

- In `scripts/evaluation/rag_eval_dataset.py`, drop the superseded values from
  `must_not_contain` on `temporal-latest-glucose`, `temporal-most-recent-glucose`
  and `temporal-current-creatinine`.
- Score temporal correctness as *"the answer identifies `expected_newest` as the
  most recent value"* rather than *"no stale value appears anywhere"*.
  `run_llm_eval.py::score()` already computes `newest_ok`; the change is to stop
  requiring `not stale_used`.

Manually re-scored under the corrected rule, temporal correctness on the valid
cases is **7/7**. That figure is a hand check, not a harness result, and must be
reproduced by an actual run before being quoted.

### Next evaluation run — required scope

When embedding quota is available:

1. Re-run `eval_harness.py --rag-quality` (golden dataset) and
   `run_llm_eval.py` (new dataset) on the same day, budgeting ~75 embedding
   calls per full pass.
2. Apply the corrected temporal scoring above **first**.
3. Include **trajectory citation checks** — the attribution regression fixed on
   2026-08-13 means latest/previous/trend answers should now carry sources.
   Assert `n_sources > 0` for every temporal case; the previous run recorded
   `sources=0` throughout, which was the bug.
4. Re-run with the seeded patient re-indexed if R3/R4/R5 chunk changes are to be
   measured; otherwise retrieval metrics will again be unchanged by construction.
5. **Tune nothing until those results are reported.**

### Metrics deliberately not reported

- **Recall@k against a labelled relevance set** — no per-chunk relevance labels
  exist; `expected_source_titles` is document-level, so only document-level
  recall is meaningful.
- **Reranker effectiveness** — would need paired runs with the reranker on and
  off against the same embeddings; the reranker is a live Groq call, so this is
  not reproducible in the test suite.
- **Latency and provider-fallback frequency** — `QueryLog` records
  `llm_provider` per answer, so fallback frequency is derivable from production
  data, but there is no timing column and no metrics backend, so neither can be
  reported as a number today. See PLAN_STATUS.

## 6. Embedding provenance & compatibility

Added 2026-08-12 (migration `rag_assistant/0009`).

A stored vector is only comparable to a query vector from the **same** model.
Cross-model comparison still returns a cosine number, so an incompatible index
degrades silently. Previously the only record of the embedding model was the
global `RAG_CONFIG['EMBEDDING_MODEL']` setting — which is why the
`text-embedding-004` → `gemini-embedding-001` deprecation went unnoticed.

Every vector now records how it was produced:

| Field | Meaning |
|---|---|
| `embedding_model` | e.g. `models/gemini-embedding-001`; empty = written before tracking existed |
| `embedding_model_version` | provider-reported version when available (Gemini exposes none) |
| `embedding_dimensions` | vector length recorded at write time |
| `embedded_at` | when the vector was generated (not row creation) |

**Single decision point.** `classify_embedding()` in
`services/embedding_service.py` returns one of `ok` / `unknown` /
`model_mismatch` / `dim_mismatch`. Dimension is checked before model, and the
decoded byte length overrides the recorded column — the bytes are ground truth,
the column is an annotation.

**Enforcement points.** Both retrieval loaders filter through it:
`EmbeddingService.load_patient_embeddings()` and
`GeneralKnowledgeService.retrieve()`. Incompatible rows are excluded from the
similarity matrix and reported in one aggregated warning per load. The general
knowledge path previously had *no* dimension check at all, so mixed-length
vectors reached `np.array()` and produced a ragged array.

**Legacy rows are preserved.** A row with no recorded provenance whose
dimension matches the active model stays usable, so no re-embed was required.
Setting `EMBEDDING_STRICT_PROVENANCE=True` flips this: unknown provenance is
then excluded, making a model swap impossible to survive silently. The
intended sequence is *backfill → verify → enable strict*.

**Failing safe on model change.** Changing `RAG_CONFIG['EMBEDDING_MODEL']`
marks every stamped row `model_mismatch`, emptying retrieval rather than mixing
vector spaces. `active_embedding_model()` raises `ImproperlyConfigured` when the
key is blank instead of falling back to a hardcoded (potentially deprecated)
name.

### Operating it

```
manage.py embedding_status                          # what is in the index
manage.py embedding_status --fail-on-stale          # deploy/CI gate
manage.py backfill_embedding_provenance             # dry run
manage.py backfill_embedding_provenance --apply     # stamp legacy rows, no re-embed
manage.py reindex_all_embeddings --stale-only       # re-embed only what is incompatible
```

`backfill_embedding_provenance` never stamps a row whose actual vector length
disagrees with the claimed model, and leaves `embedded_at` NULL because the true
generation time is unknown.

## 7. Consent

Added 2026-08-12 (migration `accounts/0008_consent`).

### External Data Egress — the enforcement boundary

> **Implemented in code ≠ enabled in production.** All eleven points are wired
> and tested. Production still runs `CONSENT_ENFORCED_EGRESS=rag`, so only the
> four ask-a-question points actually block today. See
> [EGRESS_ROLLOUT.md](EGRESS_ROLLOUT.md) for the deployment change and rollback.

Every outbound call carrying data is registered as a named `EgressPoint` in
`apps/accounts/egress.py`. An unregistered egress point is an unreviewed one, so
`ExternalProcessingGuard` raises `ValueError` on an unknown id rather than
failing open.

| Point | Provider | Data sent | PHI | Consent | Behaviour when refused |
|---|---|---|---|---|---|
| `rag.generation` | Groq / Google / Anthropic / OpenAI | record excerpts + question | yes | `external_llm` | pipeline never starts (`RAGService` gate) |
| `rag.embed_documents` | Google | text of the patient's records | yes | `external_llm` | chunks left unembedded; **upload path, not covered by the RAG gate** |
| `rag.embed_query` | Google | the question | yes | `external_llm` | raises before the call |
| `rag.rerank` | Groq | ≤300 chars of each candidate chunk | yes | `external_llm` | Stage-1 ordering returned |
| `rag.classify` | Groq | question + recent turns | yes | `external_llm` | local keyword classifier |
| `records.parse` | Google | uploaded document text | yes | `external_llm` | regex/table extraction |
| `records.ocr` | Google | the uploaded document image | yes | `external_llm` | **no fallback** — operation stops |
| `insights.interpretation` | Groq / Google | model inputs + risk result | yes | `external_llm` | built-in static interpretation |
| `insights.seizure_proxy` | **hasanai.net** | raw EEG file + filename | yes | `external_llm` | request never made |
| `insights.seizure_interpretation` | Google / Anthropic | seizure classification | yes | `external_llm` | built-in static wording |
| `knowledge.embed` | Google | curated public clinical articles | **no** | **none** | never blocked |

`knowledge.embed` is the deliberate exception: an operator indexing Käypä hoito /
Terveyskirjasto / THL articles involves no patient. The consent requirement
follows the data, not the fact that an HTTP request leaves the process.

**The upload path was the real gap.** `rag.embed_documents`, `records.parse` and
`records.ocr` are triggered by *saving a record* — `rag_assistant/signals.py`
fires on `post_save` — so none of them ever passed through the `RAGService` gate.
Before this phase, uploading a document sent its text to Google regardless of
consent.

### Guard

```python
ExternalProcessingGuard.check(user, 'records.ocr')          # raises ConsentRequired
if ExternalProcessingGuard.allows(user, 'records.parse'):   # degrade instead
```

Checks run **before** the payload is read or built, so refused data never leaves
the process. Where a local fallback exists it is used, so refusing consent
degrades a feature rather than breaking ingestion.

`settings.CONSENT_ENFORCED_EGRESS` selects which points block: `rag` (default),
`all`, or an explicit list of ids for a staged rollout. Enforcement is per-point
so widening the boundary is a deliberate act, not a side effect of deploying.

Two guarantees do **not** wait on that switch, because they are never legitimate:

- `ocr_image()` takes `user` as a **required keyword argument** — omitting it
  raises `TypeError`, so a medical document cannot be shipped to Gemini by a
  caller that never decided whose it is. An anonymous or missing user is refused
  outright regardless of `CONSENT_ENFORCED_EGRESS`.
- `RAGService` is the **authoritative entry point to generation**. The consent
  gate lives in `ask()`/`stream_ask()` rather than inside `generation_service`,
  which is only sound while nothing reaches generation another way.
  `GenerationEntryBoundaryTests` parses the source tree with `ast` and fails if a
  call to `stream_graph`, `generate` or `generate_streaming` appears outside
  `rag_service.py` → `graph/graph.py` → `graph/nodes.py`.

### hasanai.net — third-party audit

The seizure proxy is the only egress point that is **not** an LLM vendor. What
leaves: the raw EEG signal file, its original (user-supplied) filename, and its
content type. What does not leave: any user id, session, JWT or account
identifier — the POST is unauthenticated. EEG recordings are health data under
GDPR Art. 9, and a filename is user-controlled so it may itself carry an
identifier.

Retention and processing terms at the receiving service are **undocumented**.
Until a data-processing agreement exists, `external_llm` is used as the consent
purpose — the label says "external AI providers", which fits an ensemble model
service, but the purpose is really *external AI processing*. If this service is
kept, it warrants either its own consent purpose or a DPA; both are recorded as
open risks in PLAN_STATUS.

The web view (`/insights/seizure/`) is `csrf_exempt` and does not require login,
so an anonymous caller reaches it. The guard denies `AnonymousUser` once the
point is enforced.

### Model

`Consent(user, purpose, version, status, granted_at, revoked_at, created_at, updated_at)`.

Five purposes, deliberately separate — there is no blanket "I agree":
`ai_processing`, `external_llm`, `document_processing`, `data_sharing`, `research`.
`external_llm` is split out from `ai_processing` because it is the only one that
sends data to third-party processors.

Granting writes a row. Revoking stamps `revoked_at` and flips `status` on that
row; granting again writes a **new** row. History is never overwritten. A
partial unique constraint (`status='granted'`) permits at most one active
consent per (user, purpose) while allowing revoked rows to accumulate.

### Versioning

`settings.CONSENT_VERSIONS` holds the current version per purpose. `has_consent()`
matches on version, so consent recorded against superseded wording stops counting
and the UI shows *Needs renewal*. Re-granting at a new version revokes the old row
first, so the trail shows one agreement ending and the next beginning.

### Enforcement

Default-deny — absence of a record is absence of consent. The gate lives in
`RAGService.ask()` and `stream_ask()`, the only two entries to the pipeline for
both web and mobile, so no view can forget it. `ask()` returns the normal 6-tuple
with `provider='consent_required'`; `stream_ask()` emits ordinary token/meta/done
SSE events so existing clients render the message in the transcript.

`settings.CONSENT_REQUIRED_PURPOSES` (env-overridable) controls which purposes
actually block; it currently lists only `external_llm`. The others are recorded
and displayed but gate nothing yet — enforcing `document_processing` would
require an ingestion path that works without the external parser, which does not
exist.

> **Deployment note:** enforcement is default-deny, so existing users are blocked
> from the AI assistant until they grant consent. That is the legally correct
> behaviour — consent must be affirmative and cannot be back-filled — but it is a
> visible change. Ship the consent UI first, then set `CONSENT_REQUIRED_PURPOSES`.

### Surfaces

- Web: `/accounts/consent/` — per-purpose grant/withdraw plus the full history table.
- API: `GET /api/v1/consent/`, `POST /api/v1/consent/grant/`,
  `POST /api/v1/consent/revoke/`, `GET /api/v1/consent/history/`.
  All scoped to `request.user`; no endpoint accepts a user id.

`apps/accounts/consent.py` never logs which purposes a user has accepted or
declined — those choices are themselves personal data.

## 8. Data export (GDPR Art. 15 / 20)

Added 2026-08-12. `apps/accounts/export.py`. **No migration** — export reads
existing models only.

### Format

A single ZIP, generated on request and never stored:

```
manifest.json           export_version, generated_at, subject, categories, files, exclusions
user.json               account, profiles, verified emails, connected providers
medical_records.json    records + lab values + wearable points + file references
conversations.json      chat sessions and every question/answer pair
appointments.json       consent.json          access_history.json
insights.json           health alerts, and submitted AI models for data scientists
predictions.json        notifications.json    rag_index.json
files/medical_records/<record-uuid>.<ext>
files/predictions/<prediction-uuid>.<ext>
files/profile/profile_picture.<ext>
```

Records, sessions, predictions and appointments carry UUID primary keys, which
are used as the relationship keys inside the archive. The user's **internal
numeric id is never disclosed**; the subject is identified by username and email.
Child rows with integer keys (lab values, consent) are nested under their parent
rather than exposing the key.

### Scope decisions

| Included | Why |
|---|---|
| Consent history, including revocations | The user's own record of how their data was processed |
| Clinician accesses **to** this user's records | Transparency — the Kanta-style "who read my data" list |
| Linked clinicians (name, specialty, hospital) | Minimum needed for the access log to be meaningful |
| The user's own `national_id` | Their personal data; Art. 15 covers it. Flagged in the manifest |
| RAG chunk text | Derived from their records, but still their data |

| Excluded | Why |
|---|---|
| Password hash | Credential — never disclosed, even to its owner |
| Session / JWT / OAuth tokens | Authentication credentials, not personal data |
| `emergency_token` | A capability secret; a leaked export must not be replayable against the public emergency card |
| FCM device tokens | Device credentials — device count and timestamps included instead |
| Embedding vectors | Numeric restatement of text already included in full; provenance is included |
| Emergency-card viewer IP hashes | Pseudonymous third-party identifiers |
| This user's accesses to **other** patients | Applies to clinician accounts; those rows are the other patient's data |
| API keys, settings, Django groups/permissions, admin log | System configuration and internal authorization state |

The exclusion list lives in `EXCLUSIONS` and is written into every manifest, so
the archive documents its own omissions.

### Security

Subject is always `request.user`; **no endpoint accepts a user identifier**, and
a supplied one is ignored (regression-tested). Files are reached through the
storage API from querysets already filtered to the user, so no filesystem path
is built from untrusted input. Archive paths are derived from the object UUID
plus a sanitised extension — never from the stored filename — so a record named
`../../etc/passwd` cannot escape the archive. Responses are
`Cache-Control: no-store`. Nothing is written to disk server-side, so there is no
artifact to expire or leak. Throttled at 5/hour.

### Synchronous by choice

The archive is written to a `SpooledTemporaryFile` that rolls to disk past 8 MB,
so memory stays bounded without a job queue, artifact store, signed URL or expiry
mechanism — none of which this project has. The seam for going asynchronous is
`build_export()`, which already returns a file object rather than a response.

### Endpoints

| Surface | Route | Notes |
|---|---|---|
| Web | `GET /accounts/export/` | Explains contents and exclusions |
| Web | `POST /accounts/export/` | Streams the ZIP. POST so a prefetch cannot trigger it |
| API | `GET /api/v1/export/` | `{state: "ready", export_version, data_categories, exclusions}` |
| API | `GET /api/v1/export/download/` | Streams the ZIP; `{state: "failed"}` with 500 on error |

`state` is always `ready` because generation is synchronous; the field exists so
the contract can grow if it ever moves to a queue.

## 9. Application security hardening

Audited 2026-08-12. **No migration.** Regression tests: `apps/accounts/test_appsec.py`.

### SSRF — no sink exists

Every outbound HTTP call in the codebase targets a **string literal**
(`https://hasanai.net/seizure-comparison/predict/`, twice). No endpoint accepts
a URL, fetches a remote resource, or proxies to a caller-chosen host, so there is
nothing to validate and an IP/DNS allowlist would be dead code. `source_url` on
`GeneralKnowledgeChunk` is stored operator-supplied text that is never fetched.

`OutboundRequestTests.test_no_user_controlled_outbound_url_exists` pins this with
an `ast` scan: it fails if any `requests.*` call is ever given a non-literal URL,
which is the point at which a real SSRF validator becomes necessary.

Both calls now pass `allow_redirects=False` — the destination is fixed, but a
hijacked or compromised host could otherwise 302 the EEG upload at an internal
address.

### Uploads

| Control | Where |
|---|---|
| Magic-byte validation | `validate_upload` — now covers `xlsx` (ZIP header) as well |
| **Per-file size ceiling** | `MAX_UPLOAD_BYTES` (default 25 MB), enforced in `validate_upload` |
| Image allowlist by content | `validate_image_upload`, used by both web form and API |
| Filename sanitisation | non-`[\w\-.]` stripped, basename only, 200-char cap |
| Record type constrained | `_coerce_record_type` — Django does not validate `choices` on `save()` |

> `DATA_UPLOAD_MAX_MEMORY_SIZE` is measured against the request body **excluding
> file upload data**, and `FILE_UPLOAD_MAX_MEMORY_SIZE` is only the spool-to-disk
> threshold. Neither caps file size — before `MAX_UPLOAD_BYTES`, uploads were
> unbounded.

### Uploaded files cannot become active web content

`serve_media` now returns a strict content type from an allowlist
(`png/jpg/jpeg/gif/webp/pdf`), `application/octet-stream` for everything else,
plus `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src
'none'; sandbox`, and `Content-Disposition: attachment` for anything not on the
allowlist. SVG and HTML can therefore never be served as themselves. PDFs stay
inline so people can read their own lab reports; browser PDF viewers sandbox
their JavaScript away from this origin, and the CSP applies on top.

The traversal guard was also rewritten from a `startswith(root + '/')` string
prefix to `Path.is_relative_to()` — the old form hardcoded the POSIX separator
(so it rejected every path on Windows) and a prefix match would also treat a
sibling directory such as `/media-evil/` as inside `/media`.

### XSS

Two live stored-XSS paths were found and fixed:

1. **Chart payloads.** Dashboards embed `const X = {{ x_json|safe }};` inside
   inline `<script>`. `json.dumps` does not escape `</script>`, and the payloads
   contain lab parameter names taken from uploaded documents — so a lab named
   `</script><script>…</script>` broke out of the script element.
   Fixed by `apps/accounts/safe_json.py::script_safe_json`, which escapes
   `<`, `>`, `&`, U+2028 and U+2029 at serialisation time. Chosen over Django's
   `json_script` because the templates embed payloads as JavaScript expressions;
   escaping at the source is a far smaller change than rewriting every chart
   initialiser.
2. **Chat rendering.** Assistant answers were passed to `marked.parse()` and
   assigned to `innerHTML`; marked passes raw HTML straight through. Since
   answers are generated by an external model from retrieved document content,
   that is attacker-influenced HTML executing in the app's origin. Fixed with
   `escapeHtml()` before `renderMarkdown()`, so markdown still renders and
   literal tags do not.

### CSRF

Session-authenticated state-changing views are covered by Django's middleware and
verified with `enforce_csrf_checks=True` against consent, export, profile edit,
appointments, login and registration. JWT API endpoints are not
cookie-authenticated, so CSRF does not apply and none was added. Two
`csrf_exempt` views remain (`seizure_realtime_load`,
`seizure_realtime_predict_chunk`) — see PLAN_STATUS for why they are accepted
risk rather than a CSRF flaw.

### API input validation

Malformed UUIDs on `records/<pk>`, `predictions/<pk>` and
`assistant/sessions/<id>` raised `ValidationError` and returned **500**; they now
return 404. Mass assignment of `patient`, parameter pollution on list filters,
unexpected content types and deeply nested JSON are all covered by tests.

## 10. Production reliability

Audited 2026-08-12. **No migration.** Tests: `apps/accounts/test_reliability.py`.

### Health checks — liveness vs. readiness

| Endpoint | Answers | Checks | Used by |
|---|---|---|---|
| `/health/` | *is this process alive?* | nothing external | Railway `healthcheckPath` |
| `/health/ready/` | *can it serve requests?* | DB `SELECT 1`, cache round-trip | uptime monitoring |

Kept separate deliberately. Railway restarts the service when its health check
fails, and restarting does not fix a down database — it just removes the instance
that could still have served cached pages and a clear error. Readiness returns
503 with a per-check breakdown, and never echoes the exception text, which can
carry connection strings.

### External provider timeouts

Every model call now carries an explicit deadline
(`RAG_CONFIG['PROVIDER_TIMEOUT']`, default 45s; `EMBED_TIMEOUT` 60s for
off-request indexing), covering generation ×4 providers, embeddings, the LLM
reranker, the query classifier and prediction interpretation.

Previously there were none. The OpenAI and Anthropic SDKs default to **600s**
while gunicorn kills a worker at **120s**, so a few hung upstream requests could
occupy all 16 threads (2 workers × 8) until the workers were killed mid-request.
The timeout is asserted to stay below the worker timeout by test.

Gemini takes its timeout in **milliseconds** via `http_options`, and older
`google-genai` releases reject the argument — `_gemini_client()` falls back
rather than breaking generation on a version difference.

### Graceful degradation

`generate()` now wraps each provider call in `try/except` and continues to the
next on any exception. The per-provider functions already returned `None` on
their own errors, but anything escaping one — an SDK bug, an import error, a
timeout raised outside the inner handler — used to surface as a 500 rather than
falling through the chain. With all four down, the static fallback still answers.

### Concurrency

`AIModel.run_count` was a read-modify-write (`run_count=model.run_count + 1`)
across four call sites: two concurrent predictions read the same value and one
increment was lost. Now `F('run_count') + 1`, computed in the database. A
`TransactionTestCase` runs 4 threads × 10 increments and asserts the count is
exactly 40; a source scan fails the build if the stale-read pattern reappears.

### Background indexing

Indexing spawned **one thread per saved record**. A Kanta XML import creates one
`MedicalRecord` per document inside a loop, so a 200-document import meant 200
threads, each opening a database connection and calling the embedding API.

Now a module-level `ThreadPoolExecutor` capped at 2 workers, submitted from
`transaction.on_commit` so the row is visible to the worker's own connection.
Each task closes its database connection in a `finally` — pool threads are
long-lived, so their connections would be too, and would accumulate until the
server's connection limit was reached.

### Logging

`understand_node` logged the patient's question at INFO and `router_node` at
DEBUG. A health question is itself health data — asking about a diagnosis reveals
the diagnosis. Both now log `q_len` instead of the text. A source scan test fails
the build if any logging call interpolates question or query text again.

### Resource bounds

| Bound | Setting | Default |
|---|---|---|
| Per-file upload size | `MAX_UPLOAD_BYTES` | 25 MB |
| Tabular import rows | `MAX_PARSED_ROWS` | 200,000 |
| Provider call | `LLM_TIMEOUT_SECONDS` | 45 s |
| Embedding call | `EMBED_TIMEOUT_SECONDS` | 60 s |
| Indexing concurrency | `_INDEX_WORKERS` | 2 |

Parquet row count is read from the file **footer via pyarrow before any data is
materialised**, so a compression bomb is rejected rather than decompressed.

## 11. Security controls in place

| Control | Implementation |
|---|---|
| Field-level encryption | `EncryptedCharField` (Fernet) on `PatientProfile.national_id` — `apps/accounts/fields.py` |
| Upload validation | Magic-byte + extension check — `apps/medical_records/services.py:27-60` |
| Media authorization | Authenticated + per-object ownership + path-traversal guard — `healthcompass/urls.py:17-58` |
| Doctor access audit | `DoctorAccessLog` |
| Emergency-card audit | `EmergencyCardView` with hashed IP; patient can revoke token or disable card |
| PHI retention | `QUERYLOG_RETENTION_DAYS` (default 90) + `purge_old_query_logs` command |
| Account deletion | `purge_user_data()` behind password re-confirmation — `apps/accounts/views.py:290-301` |
| Transport / headers | HSTS, secure cookies, nosniff, XSS filter when `DEBUG=False` — `settings.py:279-286` |
| CORS | Env-driven allowlist; permissive localhost regex only under `DEBUG` |
| JWT | 1 h access / 30 d refresh with rotation |
| Secrets | All via `python-decouple`; `SECRET_KEY` has **no default** (fails closed) |
| API rate limiting | DRF scoped throttles — `apps/api/throttling.py`; rates in `REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']`, all env-overridable |
| Web rate limiting | django-ratelimit on login / register / password reset, keyed by IP |
| Prompt-injection defence | `UNTRUSTED_CONTENT_RULES` on all system prompts + fenced context in `_build_messages` |

### Rate limits

| Scope | Default | Applies to |
|---|---|---|
| `user` / `anon` | 1000/hour, 100/hour | every API endpoint (baseline ceiling) |
| `auth_burst` + `auth_sustained` | 10/min, 60/hour per IP | `login`, `change-password` |
| `register` | 5/hour per IP | `auth/register` |
| `password_reset` | 5/hour per IP | `auth/forgot-password` |
| `ai` + `ai_daily` | 20/min, 300/day per user | `assistant/ask`, `assistant/stream` |
| `upload` | 60/hour per user | all `records/upload/*`, profile picture |
| `ocr` | 30/hour per user | `records/upload/scan` |
| `prediction` | 60/hour per user | model runs, seizure analysis |

Each has an env override (`THROTTLE_AI`, `THROTTLE_UPLOAD`, …). Throttle state
lives in the Django cache, so **Redis (`CACHE_URL`) is required in production** —
with LocMem each gunicorn worker counts separately and the effective limit is
multiplied by the worker count.

### Prompt-injection boundary

Retrieved context comes from PDFs, OCR, Kanta XML and scraped articles — all
untrusted. Defence has three layers:

1. **Policy** — `UNTRUSTED_CONTENT_RULES` is appended to all four system prompts
   (personal / general / hybrid / trajectory), so no routing mode can select an
   unhardened prompt.
2. **Structure** — `_build_messages()` wraps retrieved content in explicit
   `BEGIN/END_RETRIEVED_DATA` fences, separate from the fenced patient question,
   and `_strip_fence_markers()` removes those delimiters from untrusted text so a
   document cannot forge its way out of the region.
3. **Scope** — retrieval is always filtered to the requesting patient, so even a
   fully successful injection cannot reach another user's records.

All eight provider functions (4 sync + 4 streaming) route through
`_build_messages` and a `sys_prompt` from `_resolve_context_and_prompt`, so there
is no generation path that bypasses layers 1 and 2. The LLM reranker, which also
consumes untrusted chunk text, carries its own boundary system message.

## 12. Management commands (operational surface)

| Command | Purpose |
|---|---|
| `reindex_all_embeddings` / `reindex_records` | Rebuild vectors after an embedding-model change |
| `purge_old_query_logs` | PHI retention enforcement (`QUERYLOG_RETENTION_DAYS`) |
| `send_appointment_reminders` | Driven by `railway.cron.toml` |
| `load_knowledge_base` | Ingest curated Finnish clinical sources |
| `ensure_social_app` | Creates the Google `SocialApp` row on startup |
| `seed_demo_models` / `seed_population` / `seed_trajectory_patient` | Demo & test data |

> There is **no** data-export command and **no** command to re-encrypt legacy
> plaintext `national_id` rows.

## 13. Testing & evaluation

- ~1,950 lines of Django tests, concentrated in `rag_assistant` (797),
  `ai_insights` (524) and `medical_records` (446). `api` and `notifications`
  are effectively untested (3 lines each).
- `RAG_AUTO_INDEX_SYNC` forces synchronous indexing under `manage.py test`
  for determinism (`settings.py:356-357`).
- `evaluation/` holds a RAG quality harness with committed results
  (`rag_quality_results.json`, `results.json`) and generated charts.

## 14. Known architectural characteristics worth naming

1. **Server-side processing, not local-first.** Documents, chunking, embeddings
   and retrieval all execute on the server. PHI is transmitted to up to four
   external LLM providers.
2. **Conversation is stored as query/response pairs**, not as individual
   messages — this shapes what a future citation/message model can look like.
3. **`parsed_data` (JSON) is the extensibility seam** for structured clinical
   facts; only lab values and wearable points have been promoted to relational
   tables so far.
4. **Vectors are in Postgres as bytes**, loaded per patient into NumPy at query
   time. This is simple and deletion-safe, but scans linearly per patient.
