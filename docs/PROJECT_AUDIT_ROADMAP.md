# HealthCompass — Project Audit & Roadmap

**Status:** Audit complete. Remediation in progress — see the resolution log below.
**Audit date:** 2026-08-13
**Scope:** Whole system (not RAG-specific). 31,381 LOC, 8 Django apps, 630 tests.
**Method:** Read-only inspection of code, models, migrations, templates, configuration,
tests and evaluation artefacts. No application code, parameters, schema, evaluation
criteria, MIMIC data or production configuration was modified during this audit.

> **Rule applied throughout:** every finding below cites a file, a test, a
> configuration value, or a reproducible observation. Where something could not be
> verified it is marked **UNCERTAIN** rather than guessed.

---

## 0. Resolution log

Findings closed since the audit. The finding text below is left exactly as written
so the original reasoning stays readable; this table is the only place that says
what has since changed. Each row names the test that would fail if the fix were
reverted.

| ID | Resolved by | Regression test |
|---|---|---|
| SEC-1 | Staff media access still allowed, now written to `DoctorAccessLog`. Rule moved to `apps/accounts/authz.py` so web and API cannot drift | `accounts/test_authz.py::MediaAccessTests::test_staff_access_is_recorded` |
| SEC-2 | `population_view` and the API's `population_insights` require a research/admin role. Checked before the cache read | `accounts/test_authz.py::PopulationAnalyticsTests` |
| SEC-3 | `DoctorAccessLog.actor_label` captures the actor at access time; FK stays SET_NULL so rows survive. Patient side deliberately still anonymised | `accounts/test_authz.py::AccessLogDurabilityTests` |
| SEC-6 | Doctors with an ACTIVE link can download their patient's files; the access is logged | `accounts/test_authz.py::MediaAccessTests::test_a_linked_doctor_can_download_the_file` |
| DM-2 | `patient` FK denormalised onto `ParsedLabValue` and `WearableDataPoint`, derived from the parent record in `save()`, backfilled by migration `0007` | `medical_records/test_data_model.py::LabValueOwnerTests` |
| DM-3 | `Meta.ordering` on both models, with NULL placement stated explicitly rather than inherited from the engine | `medical_records/test_data_model.py::OrderingTests` |
| API-1 | 27 functional tests: anonymous sweep driven from the URLconf, patient-scoping on every object endpoint, read and write paths | `api/test_endpoints.py` |
| A-1 | Dead `health_graph` (generate_node, verify_node, retry loop) removed. README/ARCHITECTURE/PLAN_STATUS corrected — the retry never ran, and grounding is now recorded as MISSING rather than PARTIAL | `rag_assistant/test_graph_liveness.py` |
| NEW-13 | Guardrail softening covers the whole stream instead of the first 500 characters; disclaimers appended once, at the end | `rag_assistant/test_guardrail_streaming.py` |
| NEW-18 | Magic-byte checks accept multi-anchor signatures (WebP needs RIFF *and* WEBP), text formats rejected on NUL bytes, size measured when `.size` is absent | `medical_records/test_upload_validation.py` |
| NEW-19 | Erasure collects every owned file including prediction inputs and model artifacts, deletes rows in a transaction and files after it commits, emits `ERASURE_INCOMPLETE` on failure | `accounts/test_erasure_and_indexing.py::ErasureCompletenessTests` |
| NEW-20 | `MedicalRecord.indexed_at` marks what was actually indexed; `reindex_unindexed_records` sweeps the rest | `accounts/test_erasure_and_indexing.py::UnindexedRecordSweepTests` |
| NEW-21 | Not a defect — `export.py` already uses a context manager for the file handle | — |
| NEW-11 | Not a defect — Django 5 resolves ambiguous DST times through `fold`; verified against 2026-10-25 and 2027-03-28 | — |
| NEW-22 | `appointments/0003` refuses to drop a table holding rows, and reversal — which used to delete every appointment — now raises. A sweep test flags any other migration that drops a table | `appointments/test_migration_safety.py` |
| CI-1 | `makemigrations --check` enabled as a gate, once the one outstanding difference (a help_text, no SQL) was recorded in `ai_insights/0004`. Lint gate added with a narrow rule set that passes today — see `ruff.toml` | CI workflow |
| AIM-1 | Model provenance: `AIModel.version` + `model_file_sha256`, stamped onto each `ModelPrediction` at creation and never rewritten, so a result stays attributable to the weights that produced it. `intended_use` added for review | `ai_insights/test_provenance.py` |
| AIM-2 | Inference bounded by input size before the call. A wall-clock timeout is **not** implemented and would be misleading if it were: onnxruntime's `run()` cannot be interrupted, so a thread-join "timeout" returns to the request while the computation keeps a core. Slow runs emit `INFERENCE_SLOW` | `ai_insights/test_provenance.py::InputSizeBoundTests` |

Still open and unchanged: SEC-4 (Fernet salt), SEC-5 (process), DM-4 (no Medication /
Condition model), DM-5, DM-6, DM-7, a hard inference timeout (needs process
isolation), and everything under P4 and beyond.

---

## 1. Executive summary

HealthCompass is a **platform with strong foundations carrying a clinical core that
cannot yet be trusted**.

The platform layer — authentication, role model, consent, egress control, field
encryption, audit logging, media access control, API skeleton — is well built and in
several places better than typical for a system at this stage. The consent model is
genuinely well designed, and the egress guard covers the *ingestion* path, which is a
boundary most systems miss.

The clinical layer is where the risk concentrates. Three findings are blocking:

1. **Critical-value detection compares SI thresholds against values that may be in
   conventional units, and runs on only one of three ingestion paths.** A record
   reporting `Glucose 140 mg/dL` is flagged CRITICAL and fires a patient alert.
2. **Embedding failure silently and permanently removes a record from retrieval.**
   No retry, no queue, no alert. The patient sees the record; the assistant denies it
   exists.
3. **Dependencies are unpinned and there is no CI.** Two deploys of the same commit can
   install different packages, and nothing runs the test suite before production.

Beneath those sit a structural gap: **medications and diagnoses are extracted by the
ingestion LLM and then discarded**, so clinical *state* has no home in the data model.
Most of the RAG findings accumulated over previous phases (N5, C1, and part of N3) are
symptoms of that gap rather than independent defects.

**Answer to the guiding question — "what must be correct before we can call this a
solid, trustworthy product?"** The platform does not need rework. The clinical data
path does: facts must be trustworthy (provenance + idempotency), correct (units), and
durable (indexing) before any further feature or RAG work is worth doing.

---

## 2. Current system architecture

```
                              ┌────────────────────────────────┐
  Browser (55 Django templates)│  Mobile client (separate repo) │
        │  session auth        └────────────┬───────────────────┘
        │                          JWT (SimpleJWT) + CORS
        ▼                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│  URL layer   healthcompass/urls.py                               │
│    /health/  /health/ready/   /media/<path>  (auth + ownership)   │
└───────┬──────────────────────────────┬───────────────────────────┘
        │ web views                    │ apps/api  (40+ endpoints)
        ▼                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  AUTHZ:  @login_required · role properties · DRF permissions      │
│          PatientDoctorRelationship(is_active) · DoctorAccessLog   │
│          ExternalProcessingGuard (consent) · scoped throttles     │
└───────┬──────────────────────────────────────────────────────────┘
        ▼
┌───────────────────────┐   ┌──────────────────────────────────────┐
│ INGESTION             │   │ SERVICES                             │
│ parsers.py (1099 LOC) │   │ dashboard · ai_insights · appointments│
│  Kanta XML / PDF /    │──►│ notifications · accounts/export      │
│  Wearable CSV / Text  │   └──────────────────────────────────────┘
│  + Gemini extraction  │
└───────┬───────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  DATA MODEL                                                       │
│   CustomUser ──1:1── PatientProfile / DoctorProfile / ...         │
│        │                                                          │
│        ├── MedicalRecord ──┬── ParsedLabValue     (no patient FK) │
│        │                   └── WearableDataPoint  (no patient FK) │
│        ├── Consent (append-only, partial unique)                  │
│        ├── MedicalDocument ── MedicalChunk (patient FK + vector)  │
│        └── HealthAlert · Notification · Appointment · Prediction  │
│   ✗ NO Medication model   ✗ NO Condition/Diagnosis model          │
└───────┬──────────────────────────────────────────────────────────┘
        │  post_save signal → ThreadPoolExecutor(2) → index+embed
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  RAG   safety_gate → understand → router → ONE OF                 │
│        {trajectory | lab_results | medications | wearable |        │
│         diagnosis | records | general | cold_start} → generate     │
│        → verify (retry only when 0 chunks)                        │
└───────┬──────────────────────────────────────────────────────────┘
        ▼
┌──────────────────────────────────────────────────────────────────┐
│  EXTERNAL   Gemini (embed + ingest extraction) · Groq (gen,       │
│  rerank, classify) · Anthropic · OpenAI · Firebase FCM · S3       │
└──────────────────────────────────────────────────────────────────┘
```

### Component responsibility table

| Component | Responsibility | Depends on | Clean boundary? | SSoT? | Can corrupt/expose? | Tested? |
|---|---|---|---|---|---|---|
| `accounts` | identity, roles, consent, encryption, export | Django auth | ✅ | ✅ | exposure — mitigated | ✅ strong (7 test files) |
| `medical_records` | ingestion, parsing, lab extraction | Gemini, pdfplumber, defusedxml | ⚠️ parsing + persistence + clinical judgement mixed | ⚠️ | **yes — corruption** | ⚠️ 53 tests, unit-level |
| `rag_assistant` | index, retrieve, generate | 4 LLM providers | ⚠️ trajectory duplicates lab logic | ❌ two lab-value definitions | exposure — guarded | ⚠️ heavily mocked |
| `api` | mobile contract, 40+ endpoints | all services | ✅ | ✅ | yes — IDOR surface | ❌ **0 functional tests** |
| `dashboard` | web UI for 4 roles | services | ✅ | ✅ | exposure | ❌ 3 tests |
| `ai_insights` | predictions, analytics, alerts | ParsedLabValue | ⚠️ third lab-value reader | ❌ | aggregate exposure | ⚠️ |
| `notifications` | FCM + in-app | Firebase | ✅ | ✅ | low | ⚠️ |
| `appointments` | scheduling + cron reminders | Firebase | ✅ | ✅ | low | ⚠️ |

---

## 3. Core product capabilities

| Capability | Works? | Data model correct? | Permissions | Tested |
|---|---|---|---|---|
| Registration / login / OAuth | ✅ | ✅ | ✅ | ✅ |
| Roles (patient/doctor/scientist/hosp-admin) | ✅ | ✅ | ✅ | ✅ |
| Doctor↔patient linkage + access log | ✅ | ✅ | ✅ gated on `is_active` | ✅ |
| Record upload (PDF/XML/CSV/text/OCR) | ✅ | ⚠️ no idempotency | ✅ | ⚠️ |
| Lab value extraction | ⚠️ LLM-derived, no provenance | ⚠️ | ✅ | ⚠️ |
| Abnormal / critical flagging | ❌ **unit-unsafe, path-dependent** | ⚠️ | ✅ | ❌ |
| **Medications as state** | ❌ **does not exist** | ❌ | — | ❌ |
| **Diagnoses as state** | ❌ **does not exist** | ❌ | — | ❌ |
| Trajectory / trend | ⚠️ discards contested data | ⚠️ | ✅ | ✅ |
| RAG chat + citations | ⚠️ see §9 | ⚠️ | ✅ isolation verified | ⚠️ |
| Consent grant/revoke/history | ✅ | ✅ | ✅ | ✅ |
| GDPR export | ✅ | ✅ | ✅ | ✅ |
| Emergency card (public token) | ✅ | ✅ | ✅ correctly scoped | ✅ |
| Dashboards (4 roles) | ⚠️ one broken image | ✅ | ✅ | ❌ 3 tests |
| Notifications / FCM | ✅ | ✅ | ✅ | ⚠️ |
| Appointments + reminders | ✅ | ✅ | ✅ | ⚠️ |

---

## 4. Documentation vs reality

Documentation was read first, then checked against code. **It is partly stale.**

| Claim | Reality | Evidence |
|---|---|---|
| README lists an `integrations` module | **Does not exist** | `ls apps/` → accounts, ai_insights, api, appointments, dashboard, medical_records, notifications, rag_assistant |
| README omits `api` and `appointments` | Both exist and are substantial | `apps/api/urls.py` — 40+ routes |
| `requirements.txt`: "Before go-live, run pip-compile" | Never done; no lock file | `requirements.txt:1-3` |
| `docs/ARCHITECTURE.md` (941 lines) | Broadly accurate for RAG; predates the reranker guard and N5 change | — |

**UNCERTAIN:** `docs/ARCHITECTURE.md` and `PLAN_STATUS.md` are large (941 / 717 lines);
this audit spot-checked them rather than verifying every claim line by line.

---

## 5. Security & privacy findings

**Overall: the strongest area of the project.** No cross-patient data leakage was found
in any inspected query path. Nine `ParsedLabValue` call sites were individually checked
and all correctly scope by `record__patient`.

**Verified correct:**
- Media serving requires auth, blocks path traversal via `Path.is_relative_to`, checks
  ownership, and refuses to serve SVG/HTML inline — `healthcompass/urls.py:84-130`
- Public emergency card renders name, DOB, blood type, allergies, emergency contact —
  **not** `national_id` (authenticated view only). UUID4 token, rate-limited,
  patient-disableable, access-logged — `apps/accounts/views.py:364-388`
- Consent is append-only with a partial unique constraint — `apps/accounts/models.py:189-200`
- Egress guard covers the **ingestion** embedding path, not just the assistant —
  `apps/rag_assistant/services/embedding_service.py:227-233`
- `national_id` encrypted with Fernet, PBKDF2 100k iterations — `apps/accounts/fields.py`
- Record ownership set server-side from `request.user`, never client input —
  `apps/api/serializers.py:135`
- Doctor access gated on `PatientDoctorRelationship(is_active=True)` and written to
  `DoctorAccessLog` *before* records render — `apps/dashboard/views.py:88-99`
- Production cookie/HSTS/nosniff config behind `if not DEBUG` — `settings.py:365-372`

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| SEC-1 | `_user_can_access_media` returns True for **any** `is_staff` user, with **no audit-log entry** — unlike dashboard record views which are logged | Medium | `healthcompass/urls.py:67-68` |
| SEC-2 | `population_view` is `@login_required` with no role check; any patient can view population aggregates. Aggregate-only, but small-N cohorts are re-identifying | Medium | `apps/ai_insights/views/analytics.py:109-110` |
| SEC-3 | `DoctorAccessLog` uses `on_delete=SET_NULL` for both actor and patient despite documenting "Never delete rows" — deleting a user anonymises the audit trail | Medium | `apps/accounts/models.py:120-127` |
| SEC-4 | Fernet key derived from `SECRET_KEY` with a **static salt**; rotating `SECRET_KEY` renders all encrypted `national_id` values unreadable | Medium (operational) | `apps/accounts/fields.py:26-35` |
| SEC-5 | `docs/TEST_ACCOUNTS.txt` holds a plaintext superuser password and states the account contains **real FIMLAB lab results**. Correctly git-ignored, but real PHI is present in a development database | Medium (process) | `docs/TEST_ACCOUNTS.txt`; `.gitignore` last line |
| SEC-6 | Doctors cannot download patient files (`_user_can_access_media` ignores `PatientDoctorRelationship`) while the UI presents patient media | Low (functional, see UX-1) | `healthcompass/urls.py:65-81` |

**No critical security vulnerability was found.** No SQL injection risk (ORM
throughout), XML parsed with `defusedxml`, uploads validated by magic bytes with size
limits, prompt-injection fences present, CORS driven by env.

---

## 6. Data model & integrity findings

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| DM-1 | **No idempotency on ingestion.** No unique constraint or content hash on `MedicalRecord` or `ParsedLabValue`; `_save_lab_value` creates unconditionally. Re-uploading a document duplicates every value | High | `apps/medical_records/services.py:187`; `models.py:47-68` |
| DM-2 | `ParsedLabValue` and `WearableDataPoint` have **no `patient` FK** — isolation depends on every caller joining `record__patient`. `MedicalDocument`/`MedicalChunk` *do* denormalise it, so the pattern is inconsistent. All current callers verified correct; no DB-level guarantee exists | High (latent) | `apps/medical_records/models.py:49,83` |
| DM-3 | `ParsedLabValue` has **no `Meta.ordering`** — row order is engine-dependent. Direct cause of C1's unspecified tiebreak between same-date readings | Medium | `apps/medical_records/models.py:47-68` |
| DM-4 | **No `Medication` or `Condition` model.** The ingestion LLM extracts `medications` and `diagnoses` into `parsed_data`; a repo-wide grep finds **zero consumers** | High (architectural) | `apps/medical_records/parsers.py:252-253` |
| DM-5 | `parsed_data` is a schemaless `JSONField` used as a dumping ground with no validation or versioning | Medium | `apps/medical_records/models.py:30` |
| DM-6 | `ParsedLabValue.value` is `CharField` while `canonical_value` is `FloatField`; consumers re-parse with `float()` inside `try/except` and silently skip failures | Medium | `apps/ai_insights/services/insights_service.py:36-39` |
| DM-7 | Migrations run on **every** container start with no pre-migration backup and up to 3 automatic restarts | High (operational) | `startup.sh` "[2/3]"; `railway.toml` |

**Cascade behaviour reviewed:** `MedicalRecord`→`ParsedLabValue`/`WearableDataPoint`,
`MedicalDocument`→`MedicalChunk`, and user→everything are all `CASCADE` — correct for
deletion/GDPR erasure. `DoctorAccessLog` and `HealthAlert.source_record` use
`SET_NULL`; correct for alerts, questionable for the audit log (SEC-3).

**Could two users access or modify each other's data?** **No path was found.** Every
record/lab/chunk query inspected filters by patient or by a verified relationship.
DM-2 is a latent risk, not a present vulnerability.

---

## 7. Backend / API findings

**Better than expected.** Status-code discipline is real: 32×400, 9×404, 5×500, 5×201,
3×204, 2×403, 2×401, plus 502/504 for provider failures.

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| API-1 | **`apps/api/tests.py` contains 0 tests.** 40+ endpoints — the entire mobile contract — have no functional coverage. Only `test_security.py` (31 tests) exercises security properties | High | `grep -c "def test" apps/api/tests.py` → 0 |
| API-2 | No idempotency keys on upload endpoints; a retried mobile upload creates a duplicate record (compounds DM-1) | Medium | `apps/api/urls.py:20-25` |
| API-3 | `seed_demo_models` runs on **every** startup, mutating production data on each restart | Medium | `startup.sh` "[2.5/3]" |
| API-4 | Background indexing uses a per-process `ThreadPoolExecutor(2)`; gunicorn runs 2 workers × 8 threads → up to 4 concurrent indexers each holding a DB connection | Medium | `apps/rag_assistant/signals.py:34`; `startup.sh` gunicorn line |

---

## 8. Frontend / UX findings

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| UX-1 | Doctor's patient-records page renders `{{ patient.profile_picture.url }}`, which routes through `serve_media` and returns 403 for every doctor — a permanently broken image | Low | `templates/dashboard/patient_records.html:11` vs `urls.py:77-80` |
| UX-2 | README describes a module (`integrations`) that does not exist and omits two that do | Low | §4 |
| UX-3 | **UNCERTAIN** — loading/empty/error states across 55 templates were not systematically audited; only permission-relevant templates were inspected | — | — |

---

## 9. RAG / AI findings

Carried forward as **evidence**, not as priorities. Full detail in prior review.

| ID | Finding | Status |
|---|---|---|
| N3 | Stage-2 reranker placed the only relevant chunk last of 13 (Stage-1 score 0.913); reproduced 3/3 at temperature 0 | **Fixed** — parameter-free guard, `test_reranker_regression.py` (8 tests) |
| N1 | Injection payload ranked #1 by Stage 1, discarded by Stage 2 → both injection tests were vacuous | **Fixed** by the same guard; payload now reaches the prompt (2/2) |
| N5 | `document_type='medication'` filter excluded the discontinuation note before ranking (pool = 1 chunk) | **Fixed** — widened to `('medication','note')`, `test_medication_status.py` (14 tests). *Symptom of DM-4* |
| C1 | Same-date readings deduped and discarded; one asserted as "MOST RECENT" while the conflict block says "do not choose one as correct" | **Open** — deferred |
| N2 | `index=False` fixture defeated by the `post_save` signal | **Open** — evaluation-only |
| N4 | Refusal detector produces false negatives **and** false positives | **Open** — evaluation-only |
| M1/M2 | Plural matching; duplicate biomarker tables | **Fixed** |
| RAG-1 | Routing is single-path; trajectory context *replaces* retrieval, so "why" questions cannot combine numbers with narrative | **Open** — architectural |
| RAG-2 | Two independent definitions of "a valid lab value" (`TrajectoryService` vs `conflict_service`) differing in filter, date source, comparability and same-date handling | **Open** — SSoT violation |

---

## 10. Testing & evaluation findings

**630 tests pass. That number is a misleading health signal.**

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| T-1 | **Evaluation metric proven insensitive.** `pass_rate` was identical (0.7727) across three retrieval configurations that differed in whether the answer-bearing evidence reached the model at all | High | `evaluation/ab_rerank.json` |
| T-2 | Pass rate drifts ±2 cases run-to-run on unchanged code (0.7727 → 0.8182 → 0.7273), driven by LLM variance in `unans-*` / `halluc-*` | High | three consecutive corpus runs |
| T-3 | Zero functional API tests (API-1) | High | — |
| T-4 | `apps/dashboard/tests.py` — 3 tests for the entire multi-role web UI | Medium | — |
| T-5 | RAG tests are heavily mocked (40 `patch`/`Mock` in `tests.py`) — they assert wiring, not behaviour | Medium | — |
| T-6 | Evaluation instrumentation itself has been wrong repeatedly: C2, C3, C4 criteria corrections, plus open N2 and N4 | High | prior phases |
| T-7 | No CI — nothing runs tests before deploy | High | `.github/workflows` absent |

**False-confidence areas, explicitly:** the API surface; the dashboard; critical-value
flagging (untested, and wrong — CB-1); ingestion idempotency; and any claim resting on
corpus `pass_rate`. The one metric that discriminated (`evidence_survival`) exists only
in a side harness and is not part of the standard run.

---

## 11. Performance & scalability

No optimisation recommended now. Predicted first failures, in order:

| ID | Bottleneck | When it bites | Evidence |
|---|---|---|---|
| PERF-1 | `_build_pop_biomarker_data` scans **all** `ParsedLabValue` rows across all patients | Population analytics at ~10⁵ rows | `apps/ai_insights/services/insights_service.py:82` |
| PERF-2 | Retrieval loads **every** chunk for a patient and does cosine in NumPy in-process — no ANN index | Patients with thousands of chunks | `embedding_service.load_patient_embeddings` |
| PERF-3 | Indexer threads × gunicorn workers → DB connection pressure (API-4) | Bulk Kanta import | `signals.py:34` |
| PERF-4 | Per-query embedding call on every question; free-tier quota is 1,000/day and we exhausted it twice during evaluation | Already observed | quota 429s, 2026-08-12/13 |
| PERF-5 | **UNCERTAIN** — N+1 patterns not systematically profiled; `select_related` is used in the hot paths inspected | — | — |

---

## 12. Deployment & operations

| ID | Finding | Severity | Evidence |
|---|---|---|---|
| OPS-1 | **`requirements.txt` is unpinned** (`django>=5.2`, `pillow`, `numpy`, … with no versions) and there is no lock file. Two deploys of the same commit can install different packages | **Critical** | `requirements.txt` |
| OPS-2 | **No CI/CD** — no automated test run gates deployment | **Critical** | `.github/workflows` absent |
| OPS-3 | Migrations run on every start, no pre-migration backup, auto-restart ×3 (DM-7) | High | `startup.sh`, `railway.toml` |
| OPS-4 | No error tracking (no Sentry/equivalent) and no metrics; failures are visible only in stdout logs | High | `settings.py:397-398` |
| OPS-5 | Backups untested — carried from the earlier reliability phase | High | prior audit |
| OPS-6 | `/health/` is liveness-only; `/health/ready/` checks DB + cache but is not the Railway healthcheck path | Low | `railway.toml`; `urls.py:164-165` |
| OPS-7 | No `runtime.txt` — Python version unpinned | Medium | — |

---

## 13. Medical-data-specific risks

| ID | Risk | Severity | Evidence |
|---|---|---|---|
| MED-1 | **Critical-value detection is unit-unsafe.** `_check_critical` applies SI thresholds (glucose 2.5–25, creatinine > 1000) to the **raw pre-normalisation** value, while the canonical unit is mg/dL. `Glucose 140 mg/dL` → `140 > 25` → **CRITICAL**, firing a `HealthAlert` + push notification | **Critical** | `apps/medical_records/services.py:135-150` |
| MED-2 | Criticality is computed on **one** ingestion path only. `_save_lab_value` is called without `is_critical` at lines 250 and 302 — so whether a critical value is detected depends on the file format uploaded | **Critical** | `services.py:250,302,362` |
| MED-3 | **Clinical facts are LLM-derived without provenance.** `is_abnormal` comes straight from Gemini; `ParsedLabValue` records nothing about which path (table / AI / regex) produced a row, nor any confidence | **Critical** | `parsers.py:206-255`; `services.py:186` |
| MED-4 | **Silent permanent loss of retrievability.** On embedding failure `embed_chunks` logs and returns; chunks persist with `embedding = NULL`; retrieval filters them out; nothing retries or alerts. The patient sees the record, the assistant denies it exists | **Critical** | `embedding_service.py:238-240` vs `:268-269` |
| MED-5 | Uncertainty is computed then destroyed (C1): same-date readings discarded, one asserted as most-recent, trend derived from the survivor (+4.0% vs ≈+56%) | High | `trajectory_service.py:164-168` |
| MED-6 | Duplicate ingestion presents as clinical conflict — `conflict_service` reports `duplicate` status for what is actually an ingestion defect (DM-1) | High | corpus fixture `Metabolic Panel 2026 (copy)` |
| MED-7 | Source attribution exists and works (offsets, document ids), but via two mechanisms (RAG-2) | Medium | — |
| MED-8 | Consent, export and erasure are implemented and tested | ✅ | `test_consent.py`, `test_export.py` |

*This is an engineering and data-integrity assessment. It contains no medical advice.*

---

## 14. Critical findings (P0)

Three findings block "production-ready". None is an active security breach.

**CB-1 — Wrong medical alerts from unit-unsafe critical detection** (MED-1 + MED-2).
Produces false CRITICAL alerts on conventional-unit records and misses criticals on
paths that never call the check. Directly patient-facing.

**CB-2 — Silent permanent loss of retrievability** (MED-4). Any embedding outage makes
affected records invisible to the assistant forever, with no operator signal. Observed
in our own quota outages.

**CB-3 — Unreproducible deploys with no test gate** (OPS-1 + OPS-2). Unpinned
dependencies plus no CI means the deployed artefact is not reproducible and untested
code can reach production.

---

## 15. Prioritized roadmap

Complexity: **S** ≤ half day · **M** 1–3 days · **L** > 3 days.

### P0 — Critical blockers

| ID | Problem | Evidence | Why it matters | Affected | Risk | Depends on | Proposed fix | Verify | Changes | Cx | Before next phase? |
|---|---|---|---|---|---|---|---|---|---|---|---|
| P0-1 | Unit-unsafe critical detection | `services.py:135-150` | False/missed critical alerts | medical_records, ai_insights, notifications | Patient harm | — | Compute from `canonical_value`; single code path for all ingestion routes | Unit tests per analyte × unit, incl. `140 mg/dL` and `5.0 mmol/L` | code | S | **Yes** |
| P0-2 | Criticality only on one ingestion path | `services.py:250,302,362` | Detection depends on file format | medical_records | Patient harm | P0-1 | Move check into `_save_lab_value` | Parametrised test across all 4 ingestion paths | code | S | **Yes** |
| P0-3 | Silent permanent retrieval loss | `embedding_service.py:238-240` | Assistant denies existing records | rag_assistant | Wrong answers | — | Persist failure state + retry/backfill + operator alert | Test: provider raises → chunk queued → backfill embeds it | code (+ maybe 1 column) | M | **Yes** |
| P0-4 | Unpinned dependencies | `requirements.txt` | Deploys not reproducible | build | Supply chain | — | `pip-compile` → lock file with hashes | Same commit → identical package set twice | config | S | **Yes** |
| P0-5 | No CI | `.github/workflows` absent | Untested code can deploy | repo | Regressions | P0-4 | Workflow running the 630 tests on PR/push | Red build blocks merge | config | S | **Yes** |

### P1 — Core correctness / architecture

| ID | Problem | Evidence | Proposed fix | Cx |
|---|---|---|---|---|
| P1-1 | Facts carry no provenance/confidence | `services.py:186`; `parsers.py` | Record extraction source + confidence per `ParsedLabValue` | M |
| P1-2 | No ingestion idempotency | `services.py:187` | Content hash on record; upsert semantics for lab values | M |
| P1-3 | No `Medication`/`Condition` state model | `parsers.py:252-253`, zero consumers | Introduce state models with start/stop events; consume existing extraction | L |
| P1-4 | Uncertainty destroyed (C1) | `trajectory_service.py:164-168` | Preserve all same-date values; mark contested; suppress or bound trend | M |
| P1-5 | Two lab-value definitions | `trajectory_service.py:150` vs `conflict_service.py:91` | One shared selector | M |
| P1-6 | Single-path routing | `graph.py:88-89`, `generation_service.py:254` | Fan-out composition of facts + narrative | L |

### P2 — Security / privacy
SEC-1 staff media bypass + audit gap (S) · SEC-2 `population_view` role check (S) ·
SEC-3 audit-log `SET_NULL` (S) · SEC-4 document `SECRET_KEY` rotation procedure (S) ·
SEC-5 purge real PHI from dev DB; rotate the documented password (S)

### P3 — Data integrity
DM-2 denormalise `patient` FK onto `ParsedLabValue`/`WearableDataPoint` (M, schema) ·
DM-3 add `Meta.ordering` (S, schema) · DM-5 validate `parsed_data` (M) ·
DM-6 type discipline on values (M) · DM-7 backup before migrate (S, config)

### P4 — Reliability
OPS-3 migration safety (S) · OPS-4 error tracking (S) · OPS-5 test backups (M) ·
API-3 stop seeding on every boot (S) · API-4 bound indexer concurrency (S) ·
indexer/deletion race (S) · OPS-6 healthcheck → `/health/ready/` (S) · OPS-7 pin Python (S)

### P5 — Testing / evaluation
API-1 functional API tests (L) · T-1 promote `evidence_survival` into the standard
runner (S) · T-2 quantify run-to-run variance before quoting any rate (S) ·
N4 two-part refusal criterion, documented as a criteria change (S) · N2 fixture (S) ·
T-4 dashboard tests (M) · patient-isolation tests as an explicit suite (M)

### P6 — Performance / scalability
PERF-1 aggregate pre-computation (M) · PERF-2 ANN index when chunk counts justify it
(L) · PERF-4 embedding cache / paid quota (S)

### P7 — RAG / AI quality
RAG-1 composition (folded into P1-6) · RAG-2 (P1-5) · reranker long-term role ·
re-run injection cases now that payloads actually reach the model

### P8 — UX / polish
UX-1 doctor profile-picture 403 (S) · UX-2 README correction (S) ·
UX-3 systematic loading/empty/error state audit (M)

### P9 — Nice-to-have
Structured logging · admin dashboards · i18n · richer charts

---

## 16. Dependency-aware execution order

Derived from actual dependencies, not from a generic template.

```
STAGE 1 — Make deploys trustworthy        P0-4 → P0-5
          (nothing else is verifiable until the build is reproducible
           and tests gate deployment)
              ↓
STAGE 2 — Stop producing wrong medical    P0-1 → P0-2
          signals                          (P0-2 depends on P0-1's single path)
              ↓
STAGE 3 — Stop losing data                P0-3 → P1-2
          (durable indexing, then idempotent ingestion)
              ↓
STAGE 4 — Make facts trustworthy          P1-1 → P3(DM-2, DM-3)
          (provenance, then the schema guarantees under it)
              ↓
STAGE 5 — Model clinical state            P1-3
          (unblocks medication/diagnosis questions properly)
              ↓
STAGE 6 — Represent uncertainty           P1-4 → P1-5
          (C1 dissolves; the two lab definitions merge)
              ↓
STAGE 7 — Security & reliability tidy-up  P2 → P4
              ↓
STAGE 8 — Rebuild trustworthy evaluation  P5
          (T-1/T-2 first: no RAG claim is meaningful until the metric
           discriminates and its variance is known)
              ↓
STAGE 9 — RAG composition & quality       P1-6 → P7
              ↓
STAGE 10 — Performance                    P6
              ↓
STAGE 11 — UX polish                      P8 → P9
```

**Why RAG comes this late:** N5 is a symptom of P1-3, C1 of P1-4, RAG-2 of P1-5, and
every RAG quality claim depends on P5 producing a metric that can tell improvement from
noise. Optimising RAG before Stage 8 means optimising against a number already proven
insensitive.

---

## 17. Definition of Done — per stage

| Stage | Done when |
|---|---|
| 1 | Lock file with hashes committed; CI runs all tests on push; a deliberately failing test blocks the build |
| 2 | Criticality computed from `canonical_value` on every ingestion path; parametrised tests cover analyte × unit incl. the `140 mg/dL` case; no behaviour change for SI inputs |
| 3 | Embedding failure leaves a durable, queryable state; backfill re-embeds; operator signal exists; test proves a failed embed is later recovered |
| 4 | Every `ParsedLabValue` carries extraction provenance; `patient` FK present and enforced; deterministic ordering; migrations reversible |
| 5 | Medication state queryable without RAG; "what am I taking" answered from the model; the 6 N5 scenarios pass against the state model, not the vector store |
| 6 | No same-date reading discarded; no single "MOST RECENT" asserted when contested; trend suppressed or bounded; conflict and trajectory cannot contradict |
| 7 | Every P2 item closed with a test; staff media access audit-logged |
| 8 | Evaluation reports evidence-coverage as a first-class metric; variance quantified over ≥3 runs; refusal criterion documented and re-baselined; no criteria change unannounced |
| 9 | Facts and narrative composable in one answer; "why" questions retrieve both; injection cases re-run against a context provably containing the payload |
| 10 | A measured bottleneck, not a guessed one, is improved with before/after numbers |
| 11 | UX issues closed; documentation matches reality |

---

## 18. Current known limitations

- Evaluation corpus is 22 self-authored cases over 3 synthetic patients; `pass_rate`
  is insensitive (T-1) and drifts ±2 cases (T-2)
- MIMIC subset is offline-only and must never reach an external API; not re-indexed
- `CONSENT_ENFORCED_EGRESS` remains `rag` in production (staged, `docs/EGRESS_ROLLOUT.md`)
- hasanai.net DPA outstanding
- `backfill_embedding_provenance --apply` not yet run in production;
  `EMBEDDING_STRICT_PROVENANCE` still False
- Free-tier Gemini embedding quota (1,000/day) has been exhausted twice
- Mobile client lives in a separate repository and was not audited
- **UNCERTAIN:** `ARCHITECTURE.md`/`PLAN_STATUS.md` spot-checked, not line-verified;
  frontend states (UX-3) and N+1 profiling (PERF-5) not systematically covered

---

## 19. Evidence index

| Area | Primary files |
|---|---|
| Data model | `apps/*/models.py`; migrations (31 total) |
| Ingestion | `apps/medical_records/parsers.py`, `services.py`, `unit_normalizer.py` |
| Security | `apps/accounts/{consent,egress,export,fields}.py`; `healthcompass/urls.py:65-160` |
| RAG | `apps/rag_assistant/graph/{graph,nodes,state}.py`; `services/*.py` |
| API | `apps/api/{urls,serializers}.py`, `views/*.py`, `throttling.py` |
| Frontend | `templates/` (55 files) |
| Config/deploy | `healthcompass/settings.py`, `startup.sh`, `railway.toml`, `requirements.txt` |
| Tests | 630 tests; `apps/*/test_*.py` |
| Evaluation | `scripts/evaluation/*.py`; `evaluation/ab_rerank.json`, `rag_corpus_full.json` |

---

## 20. Change log

| Date | Version | Change | Author |
|---|---|---|---|
| 2026-08-13 | 1.0 | Initial project-wide audit. 13 phases, read-only. No remediation started. Supersedes the RAG-only prioritisation of N2/N4/C1 | Claude (audit) |

### Per-item status template

Update after each completed roadmap item:

```
### <ID> — <title>
Status:            not started | in progress | done | deferred
Completed:         <date>
Change:            <what changed, files>
Tests added:       <names, count>
Verification:      <measured result, before → after>
Regressions:       <none | detail>
Remaining risk:    <what is still open>
```

---

*End of audit. No code, schema, configuration, evaluation criteria, or data was modified
in producing this document.*
