# Model Mapping — Implementation Plan → HealthCompass Codebase

> **Purpose:** the implementation plan names ~20 generic tables. This document
> binds every one of them to what already exists, and records a **decision** so
> no agent creates a duplicate model.
>
> **Status:** Phase 1 audit, 2026-08-12. No migrations have been made.
> Decisions marked **NEEDS APPROVAL** must not be implemented without sign-off.

## Legend

| Decision | Meaning |
|---|---|
| `KEEP` | Existing model satisfies the intent. Do not create the plan's table. |
| `EXTEND` | Existing model is the right home; add fields to it. |
| `NEW` | No equivalent exists; a new model is justified. |
| `OUT OF SCOPE` | Belongs to the mobile repo or is not part of this product. |
| `DEFER` | Plausible but not currently justified; revisit with evidence. |

---

## 1. Identity

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `USER` | `accounts.CustomUser` | `KEEP` | Plan assumes a single `PROFILE`; reality is a **role-discriminated user with four profile tables**. The plan's 1:1 model is a simplification and should be corrected in the plan, not in the code. |
| `PROFILE` | `PatientProfile`, `DoctorProfile`, `DataScientistProfile`, `HospitalAdminProfile` | `KEEP` | Plan fields `timezone`, `locale`, `preferred_units` do not exist. Only add if a real feature needs them — `TIME_ZONE` is currently fixed to `Europe/Helsinki` globally. |

## 2. Consent & sharing

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `CONSENT` | `accounts.Consent` + `accounts.ConsentPurpose` | `DONE` | Implemented 2026-08-12, migration `accounts/0008_consent`. Covers every field the plan asked for (`purpose`, `version`, `status`, `granted_at`, `revoked_at`, `created_at`, `updated_at`) plus a partial unique constraint for the active row. Five separate purposes, no blanket agreement. Service layer: `apps/accounts/consent.py`. |
| family sharing | `PatientDoctorRelationship` | `KEEP` + `EXTEND` later | A scoped, revocable clinician link already exists (`is_active`, `unique_together`). It has **no scope field and no expiry** — those are the real gaps, not a new table. |

## 3. Clinical data

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `HEALTH_RECORD` | `medical_records.MedicalRecord` | `KEEP` | Direct equivalent. Plan's `description` ≈ `notes`; `clinical_date` ≈ `record_date`. |
| `MEASUREMENT` | `ParsedLabValue` **and** `WearableDataPoint` | `KEEP` (two tables) | Deliberate split by source. `ParsedLabValue` already exceeds the plan: `canonical_value` / `original_unit` / `unit_known` give safe cross-record comparison the plan only gestures at. **Do not merge these into one table.** |
| `CONDITION` | `MedicalRecord(record_type='diagnosis')` + `parsed_data` | `DEFER` — **NEEDS APPROVAL** | See §"Condition / Medication analysis" below. |
| `MEDICATION` | `MedicalRecord(record_type='prescription')` + `parsed_data` | `DEFER` — **NEEDS APPROVAL** | Same. |

## 4. Documents

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `DOCUMENT` | `MedicalRecord` (`file`, `raw_text`) + `rag_assistant.MedicalDocument` | `KEEP` | The plan's single `DOCUMENT` is split across two models: the uploaded artifact (`MedicalRecord.file`) and its RAG projection (`MedicalDocument`). This split is sound. Missing plan fields on `MedicalRecord`: `checksum`, `mime_type`, `file_size`, `status`. |
| `DOCUMENT_VERSION` | **nothing** | `DEFER` | Uploads are immutable in practice (no in-place replace flow exists). Versioning is only justified once re-upload/correction exists. |
| `DOCUMENT_CHUNK` | `rag_assistant.MedicalChunk` | `EXTEND` | Exists with `chunk_index` + `unique_together`. **Missing:** `page_number`, `section_title`, `start_offset`, `end_offset` — i.e. no way to point a citation at a location inside the source. |
| `EMBEDDING` (referenced in plan §33, never defined) | `MedicalChunk.embedding` `BinaryField` | `KEEP` | Embeddings are a column, not a table. This is deletion-safe and correct for the current scale. **But the embedding model name/version is not recorded per row** — see PLAN_STATUS §8. |

## 5. Conversations & AI

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `CONVERSATION` | `rag_assistant.ChatSession` | `KEEP` | Direct equivalent (plan's `status` field absent). |
| `MESSAGE` | `rag_assistant.QueryLog` | `KEEP` (with caveat) | **Shape mismatch:** `QueryLog` stores query **and** response in one row. The plan wants one row per message with `role` + `sequence_number`. Splitting is a large, risky migration touching the chat UI, SSE endpoint, mobile API and eval harness. Not worth it unless multi-turn tool calls or per-message editing are required. |
| `AI_REQUEST` | flattened into `QueryLog.llm_provider` | `KEEP` | Model *name* is recorded, model *version* is not. |
| `AI_RESPONSE` | flattened into `QueryLog.response` | `KEEP` | Plan's `grounded` / `abstained` / `safety_level` have no column. `safety_routed` (bool) is the closest. |
| `CITATION` | `QueryLog.sources` `JSONField` | `EXTEND` → `DEFER` table | Citations exist but are denormalized JSON with no FK to the chunk. Normalizing depends on `MedicalChunk` gaining location fields first. |
| `SAFETY_ASSESSMENT` | `QueryLog.safety_routed` + `triggered_rules` JSON | `KEEP` | Signals are captured; risk level and action are not distinguishable. Adding a `risk_level` / `action` field to `QueryLog` is cheaper than a new table. |

## 6. Retrieval

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `RETRIEVAL_LOG` | `QueryLog` (`retrieved_chunks_count`, `query_mode`) | `KEEP` | Merged into `QueryLog`; acceptable. |
| `RETRIEVAL_RESULT` | **nothing** | `NEW` (low priority) | Only the *count* of retrieved chunks is stored, not *which* chunks at what rank/score. This blocks offline retrieval-quality analysis on production traffic — but the `evaluation/` harness already covers this on a fixed dataset. |

## 7. Audit, notifications, ops

| Plan table | Actual | Decision | Notes |
|---|---|---|---|
| `AUDIT_EVENT` | `DoctorAccessLog` + `EmergencyCardView` | `NEW` (generalize) | Two narrow, well-built audit tables exist. There is **no** audit for login/logout, uploads, deletions, exports or consent changes. |
| `NOTIFICATION` | `notifications.Notification` | `KEEP` | Direct equivalent + `FCMDevice` for push. |

## 7b. External egress registry (no table — deliberately)

`apps/accounts/egress.py` declares `EgressPoint` as a frozen dataclass and
`EGRESS_POINTS` as a module-level dict, **not** a Django model. The registry
describes code paths, not user data: it changes only when a developer adds or
removes an outbound call, so it belongs in version control where that change is
reviewable, not in a table an operator could edit at runtime. `Consent` remains
the database record; the registry is the map of what consent governs.

`ExternalProcessingGuard` is the only thing that reads it, and
`egress_matrix()` renders it for documentation.

## 7c. Data export (no model — deliberately)

`apps/accounts/export.py` reads existing models and streams a ZIP. **No new
table, no migration.**

An `ExportJob` model would only be justified by asynchronous generation, which
would in turn need a queue, an artifact store, signed URLs and an expiry sweep —
none of which exist here, and each of which is a new place for a health archive
to sit at rest. Generating on request and storing nothing is both simpler and
the more private design. `build_export()` returns a file object rather than a
response, so it is already the seam to make this asynchronous if export volume
ever demands it.

The export reads: `CustomUser`, all four profile models, `EmailAddress`,
`SocialAccount`, `MedicalRecord` (+ `ParsedLabValue`, `WearableDataPoint`),
`ChatSession` (+ `QueryLog`), `MedicalDocument`, `MedicalChunk`, `Appointment`,
`HealthAlert`, `AIModel` (own submissions), `ModelPrediction`, `Notification`,
`FCMDevice`, `Consent`, `DoctorAccessLog` (subject side only),
`PatientDoctorRelationship` (subject side only), `EmergencyCardView`.

Not read: `GeneralKnowledgeChunk` (not user-owned), `SocialToken`, `LogEntry`,
`Group`, `Permission`.

## 7d. Security hardening (no model changes)

The 2026-08-12 application-security audit produced **no schema change and no
migration**. Every fix was in validation, serialisation or response headers:

| Concern | Where it lives | Why not a model |
|---|---|---|
| Script-safe JSON | `apps/accounts/safe_json.py` | An encoding rule, not data |
| Upload size / type / image allowlist | `medical_records/services.py` | Validation belongs at the boundary |
| Media response hardening | `healthcompass/urls.py::_safe_file_response` | A response concern |
| `record_type` coercion | `medical_records/services.py::_coerce_record_type` | Constrains writes to the **existing** `choices`; no new column |

`MedicalRecord.record_type` already declared its `choices`; Django simply does
not enforce them on `save()`. Adding a database constraint would need a
migration *and* would fail on any historical row already holding a stray value,
so the constraint is applied at the write path instead.

## 7e. Reliability hardening (no model changes)

The 2026-08-12 reliability audit also produced **no schema change and no
migration**. Two findings were close to the data layer and were still fixed
without touching it:

| Finding | Fix | Why no migration |
|---|---|---|
| Lost updates on `AIModel.run_count` | `F('run_count') + 1` — the increment happens in the database | A concurrency bug in *how* the column was written, not in the column |
| Unbounded indexing threads on bulk import | Bounded `ThreadPoolExecutor` in `rag_assistant/signals.py` | Scheduling concern; the models are unchanged |

An `IndexingJob` table was considered for the second and rejected. It would only
earn its place alongside a real queue with retries and a dead-letter path; adding
the table without the queue would record failures nobody processes. Recorded as
P2 in PLAN_STATUS instead.

## 7f. RAG evaluation (no model changes)

The 2026-08-12 RAG quality phase was an **audit**: no schema change, no
migration, and no retrieval parameter touched. The dataset and baseline live in
version control, not the database:

| Artifact | Location | Why not a model |
|---|---|---|
| Evaluation dataset | `scripts/evaluation/rag_eval_dataset.py` | Test fixtures; they must be reviewable in a diff |
| Deterministic baseline | `evaluation/rag_deterministic_baseline.json` | A build artifact, regenerated by a script |
| Regression tests | `apps/rag_assistant/test_rag_eval.py` | — |

### Model changes the findings needed — implemented, still no migration

| Finding | Change | Migration? |
|---|---|---|
| R1 (recency not temporal) | keyword tables in `query_understanding.py` | **No** — module constants |
| R1b (latest vs trend) | `temporal_mode` on the `QueryIntent` **dataclass** and `HealthState` **TypedDict** | **No** — neither is a Django model |
| R3 (date lost in later chunks) | context header prepended to continuation chunk text | **No** — content only; **requires re-indexing** |
| R5 (no location data) | `start_offset` / `end_offset` keys in `MedicalChunk.metadata` | **No** — `metadata` is a JSONField |
| R6 (no conflict detection) | new `conflict_service.py` reading existing `ParsedLabValue` | **No** — read-only analysis |
| R4 (labels split from values) | boundary logic in the splitter | **No** — **requires re-indexing** |

**All six landed without a migration.** Two design decisions made earlier are
what paid off here:

- `MedicalChunk.metadata` being a **JSONField** made R5 free. Had chunk
  provenance been modelled as columns, adding offsets would have needed a schema
  change plus a backfill.
- `ParsedLabValue` being a **normalized table** with `canonical_value` and
  `unit_known` made R6 possible at all. Conflict detection compares values across
  documents, which cannot be done against `parsed_data` JSON — this is a concrete
  instance of the argument in §3 for when normalization earns its place, and is
  worth remembering if Condition/Medication normalization is revisited.

R3 and R4 change chunk **text**, so their benefit only reaches existing data
after `reindex_records --clear`. See PLAN_STATUS for the command and scope.

## 8. Not in the plan, but in the codebase

These have no counterpart in the implementation plan and must be preserved:

| Model | Why it matters |
|---|---|
| `ai_insights.AIModel` / `ModelPrediction` / `HealthAlert` | Data-scientist ONNX model marketplace with an approval workflow — a whole product surface the plan omits. |
| `accounts.EmergencyCardView` + `PatientProfile.emergency_token` | **Unauthenticated public endpoint** keyed by UUID. A deliberate security exception the plan never models. |
| `appointments.Appointment` | Reminder scheduling via Railway cron. |
| `rag_assistant.GeneralKnowledgeChunk` | Curated public Finnish clinical corpus — enables the "general" and "hybrid" answer modes. |

---

## Condition / Medication analysis — **NEEDS APPROVAL**

Per instruction, these were assessed rather than migrated.

### Current handling

`MedicalRecord` already carries `record_type='diagnosis'` and
`record_type='prescription'`, with structured output in `parsed_data` (JSON)
populated by `apps/medical_records/parsers.py`. The RAG layer projects
prescriptions through `DocumentProcessor._process_medication()` into
`document_type='medication'` chunks, and the LangGraph pipeline has dedicated
`medications_node` and `diagnosis_node` routes. **The feature works today.**

### When `parsed_data` becomes insufficient

Normalization is justified when a feature needs to query *across* records by
clinical attribute. Concretely:

1. **"Am I still taking X?"** — requires `status` (active/stopped) and
   `start_date`/`end_date` per medication, queried across all records.
   JSON cannot index this; today the LLM must infer it from retrieved text.
2. **Drug-interaction or duplicate-therapy checks** — require a canonical
   medication list per patient, not one list per prescription document.
3. **Condition timeline** (`onset_date` → `resolution_date`) — the dashboard
   cannot render an active-conditions panel without per-condition rows.
4. **Structured export** (GDPR / clinician handover) — a normalized medication
   and problem list is the expected format.

None of these features currently exist in the UI or API.

### Recommendation

**Keep `parsed_data` for now.** The precedent to follow is `ParsedLabValue`:
it was normalized because trajectory comparison genuinely required indexed,
unit-normalized numeric rows. No equivalent forcing feature exists yet for
conditions or medications.

### If approved later — proposed shape

Mirror `ParsedLabValue` exactly: child tables of `MedicalRecord`, populated by
the parser, with the JSON retained as the source of truth for provenance.

```
ParsedMedication(record FK, name, normalized_name, dosage, unit,
                 frequency, route, status, start_date, end_date,
                 confidence, user_confirmed)
ParsedCondition (record FK, name, icd10_code, status,
                 onset_date, resolution_date,
                 confidence, user_confirmed)
```

- **Migration complexity: moderate.** Schema is additive (no data loss risk),
  but requires a backfill command re-parsing existing `parsed_data` across all
  historic diagnosis/prescription records, plus reindexing affected RAG chunks.
- **Backward compatibility: preserved.** `parsed_data` stays populated and
  authoritative; the new tables are a derived, rebuildable index. Any parser
  bug can be fixed by re-running the backfill. Existing API responses and RAG
  nodes continue to work untouched during rollout.
- **Rollout:** add models → backfill command → dual-read (prefer table, fall
  back to JSON) → switch RAG nodes → only then build the new features.

**Do not implement without explicit approval.**
