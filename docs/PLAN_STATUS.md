# Plan Status — Implementation Plan vs. Codebase

> **Status:** Phase 1 audit, 2026-08-12. No code changed, no migrations made.
> Sections refer to `HealthCompass_Web_Mobile_Implementation_Plan.md`.
> Companions: [ARCHITECTURE.md](ARCHITECTURE.md), [MODEL_MAPPING.md](MODEL_MAPPING.md).

**Rule for future sessions:** a row may only be marked `DONE` with a
`file:line` reference in the Evidence column. No evidence, no `DONE`.

## Legend

`DONE` · `PARTIAL` · `MISSING` · `N/A` (mobile repo or not applicable) · `PLAN WRONG` (spec conflicts with the product; fix the plan)

Priority: **P0** ship-blocking / legal risk · **P1** important · **P2** nice to have · **P3** speculative

---

## Section-by-section status

| § | Requirement | Status | Evidence / gap | Pri |
|---|---|---|---|---|
| 1 | Medical boundary (no autonomous diagnosis/prescribing) | `PARTIAL` | Enforced in prompts + `GuardrailService`; **not written down anywhere as an intended-use statement** | P1 |
| 2 | Local-first / on-device processing | `PLAN WRONG` | Server-rendered Django, server-side embeddings, PHI to 4 external LLMs. Local-first is not this architecture — see below | — |
| 3 | User / Profile | `DONE` | `apps/accounts/models.py:7-108` (5 roles, 4 profiles) | — |
| 4 | Consent model + revocation | `DONE` | `accounts.Consent` (migration `0008`); grant/revoke/history in `apps/accounts/consent.py`; web `/accounts/consent/` + 4 API endpoints | — |
| 4 | Separate purposes (not one blanket agreement) | `DONE` | 5 purposes in `ConsentPurpose`; `external_llm` split from `ai_processing` | — |
| 4 | Consent version stored + applied | `DONE` | `settings.CONSENT_VERSIONS`; `has_consent()` matches on version; stale consent surfaces as *needs renewal* | — |
| 4 | Historical consent auditable | `DONE` | Append-only; revoke stamps the row, re-grant adds a new one; history shown in UI and via API | — |
| 4 | External LLM processing made explicit + enforced | `DONE` | 11 egress points registered in `apps/accounts/egress.py`; `ExternalProcessingGuard` checks before payload construction at every PHI-bearing call; full matrix in ARCHITECTURE §7 | — |
| 4 | Upload-path egress (indexing / parsing / OCR) gated | `DONE` | `embed_chunks`, `create_from_pdf`/`create_from_text`, `ocr_image` — these bypass the RAG gate entirely (post_save signal) | — |
| 4 | Non-PHI egress not over-blocked | `DONE` | `knowledge.embed` registered with `purpose=None`; public article indexing never blocked | — |
| 4 | Wider egress enforcement — **implemented in code** | `DONE` | All 10 PHI points fail-closed under `all`: `FailClosedReadinessTests` asserts deny for missing/revoked/stale/anonymous and allow with valid consent | — |
| 4 | Wider egress enforcement — **enabled in production** | `MISSING` | `CONSENT_ENFORCED_EGRESS` still `rag`. Uploads (indexing, parsing, OCR) still reach Google without consent. Deployment change + rollback: [EGRESS_ROLLOUT.md](EGRESS_ROLLOUT.md) | **P0** |
| 4 | `ocr_image` fail-closed | `DONE` | `user` is a required kwarg (omitting raises `TypeError`); anonymous/None refused regardless of the enforcement switch | — |
| 4 | Generation entry boundary pinned | `DONE` | `GenerationEntryBoundaryTests` — ast scan; only `rag_service.py` enters the graph, only graph modules call generation | — |
| 4 | Consent refusal response contract | `DONE` | `{'error': str, 'consent_required': 'external_llm'}` on 403 paths; assistant keeps 200 + adds the field. No mobile rewrite required | — |
| 4 | hasanai.net data-processing agreement | `MISSING` | Third party receives raw EEG + filename; endpoint unauthenticated & `csrf_exempt`; retention undocumented; no DPA. **Must not be enabled in production** — see below | **P0** |
| 4 | Enforcement for non-LLM purposes | `PARTIAL` | Only `external_llm` is in `CONSENT_REQUIRED_PURPOSES`; document processing/sharing/research are recorded but gate nothing | P1 |
| 4 | Family/clinician sharing with scope + revocation | `PARTIAL` | `PatientDoctorRelationship` has `is_active` but **no scope, no expiry** | P1 |
| 5 | Health record | `DONE` | `apps/medical_records/models.py:6` | — |
| 5 | Measurement (type/unit/range/abnormal) | `DONE` | `ParsedLabValue` with `canonical_value`/`unit_known` — exceeds the spec | — |
| 5 | Condition / Medication tables | `DEFER` | Handled via `parsed_data`; analysis in MODEL_MAPPING — **needs approval** | P2 |
| 6 | Time & provenance distinctions | `PARTIAL` | `record_date` / `uploaded_at` / `measured_at` exist. **Missing:** extraction timestamp, confidence, user-confirmed flag, original-text pointer | P1 |
| 7 | Document metadata (`checksum`, `mime_type`, `file_size`, `status`) | `MISSING` | `MedicalRecord` has none of these → **no duplicate-upload detection** | P1 |
| 7 | Document versioning | `MISSING` | No re-upload flow exists, so not yet forcing | P3 |
| 7 | Secure upload (MIME/size/no filename trust) | `DONE` | Magic bytes `apps/medical_records/services.py:27-60`; 50 MB cap `settings.py:214-215` | — |
| 7 | Ownership-enforced file serving | `DONE` | `healthcompass/urls.py:17-58` — auth + per-object ownership + traversal guard | — |
| 8 | Structure-aware chunking | `PARTIAL` | Type-aware (`_process_lab` / `_process_medication` / `_process_wearable` / `_process_text`) but the splitter is a **fixed 200-word window with 40 overlap** — no heading/table/section awareness | P2 |
| 8 | Chunk metadata (page, section, offsets) | `PARTIAL` | R5: exact `start_offset`/`end_offset` now recorded and surfaced in citations. Page/section omitted by design — the PDF parser discards page boundaries before chunking | P2 |
| 8 | **Embedding model/version recorded per vector** | `DONE` | Migration `rag_assistant/0009`; `EmbeddingProvenanceMixin` in `apps/rag_assistant/models.py`; stamped in `embed_chunks()` and `index_articles()` | — |
| 8 | Embedding compatibility enforced at retrieval | `DONE` | `classify_embedding()` gates `load_patient_embeddings()` and `GeneralKnowledgeService.retrieve()` | — |
| 8 | Stale-embedding detection | `DONE` | `audit_embeddings()`; `manage.py embedding_status [--fail-on-stale]`; `stale_chunks` in `RAGService.index_status()` | — |
| 8 | Model change fails safe | `DONE` | Model swap marks stamped rows `model_mismatch` and empties retrieval; `active_embedding_model()` raises on blank config | — |
| 8 | Legacy rows stamped so strict mode can be enabled | `PARTIAL` | `backfill_embedding_provenance --apply` **not yet run**; `EMBEDDING_STRICT_PROVENANCE` still `False` | P1 |
| 9 | Hybrid semantic + keyword retrieval | `DONE` | BM25 + cosine with intent weights, `retrieval_service.py:98-153` | — |
| 9 | Temporal reasoning — trend questions | `DONE` | `trajectory_service.py` orders by `measured_at`; 100% route accuracy on trend cases | — |
| 9 | Temporal reasoning — **latest/newest/current/previous** | `DONE` | R1+R1b: guarded recency vocabulary + `temporal_mode`; route accuracy 0% → 100% on `temporal_latest` | — |
| 9 | Reranking | `DONE` | MMR + optional LLM rerank, `retrieval_service.py:154-223` | — |
| 9 | Retrieval log with per-chunk rank/score | `PARTIAL` | Only `retrieved_chunks_count` is stored, not which chunks | P2 |
| 10 | Conversation / message storage | `PARTIAL` | `ChatSession` + `QueryLog`; `QueryLog` is a query+response **pair**, not per-message | P3 |
| 10 | AI responses don't become medical records | `DONE` | No write path from `QueryLog` to `MedicalRecord` | — |
| 11 | Citations shown to user | `PARTIAL` | `QueryLog.sources` JSON rendered in UI; no FK to the chunk. Trajectory-path citations fixed 2026-08-13 (see below) | P1 |
| 11 | Citations on temporal (latest/previous/trend) answers | `DONE` | **Regression from R1, fixed.** The ORM trajectory path loads ParsedLabValue→MedicalRecord and never touched MedicalDocument, so its chunks had no `document_id` and `_build_sources()` dropped every one. `TrajectoryService._attach_document_metadata()` resolves the document patient-scoped; 0 → 5 citations on the seeded patient. Tests: `test_trajectory_citations.py` | — |
| 11 | Grounding / abstention status | `MISSING` | Was recorded as PARTIAL on the strength of `verify_node`. That node lived in a graph no caller ever invoked, so it never ran; it has been removed. No retry on empty retrieval, and **no `grounded` / `abstained` column** on `QueryLog` | P1 |
| 12 | Safety gate + risk levels + actions | `PARTIAL` | `check_pre_query` gate + post-generation guardrails work; `safety_routed` is a **boolean** — risk level and chosen action aren't distinguishable | P1 |
| 12 | Safety not dependent on unconstrained LLM | `DONE` | Deterministic rules in `guardrail_service.py`, runs before retrieval | — |
| 13 | Prompt-injection defense / untrusted retrieved data | `DONE` | `UNTRUSTED_CONTENT_RULES` appended to all 4 system prompts; fenced `_build_messages`; delimiter forging neutralised by `_strip_fence_markers`; LLM reranker given a boundary system message | — |
| 14 | Validated structured output | `MISSING` | Providers return free text; no schema validation before display | P2 |
| 15 | FKs, indexes, timestamps | `DONE` | Indexes on all hot paths; UUID PKs on patient-facing tables | — |
| 15 | Deletion / cascade behaviour defined | `PARTIAL` | Cascades work and embeddings are in-row so no orphans; **not documented, and `on_delete` choices were never reviewed for medical records** | P2 |
| 16 | Per-user data isolation | `DONE` | IDOR sweep over all owned resources in `apps/api/test_security.py` + `apps/accounts/test_security.py`; no cross-user read/write/delete path found in views | — |
| 16 | Role-based authorization | `DONE` | `is_approved` gate + `PatientDoctorRelationship` + `DoctorAccessLog`; **privilege-escalation hole in API registration fixed** (see below) | — |
| 17 | Password hashing / reset / session expiry | `DONE` | Django defaults + full reset flow `apps/accounts/urls.py`; web registration login fixed to authenticate through the backend chain | — |
| 17 | Secrets never committed | `DONE` | `python-decouple` throughout; `SECRET_KEY` has no default (`settings.py:9`) | — |
| 17 | MFA | `MISSING` | Not implemented | P3 |
| 18 | TLS / HSTS / secure cookies | `DONE` | `settings.py:279-286` | — |
| 18 | Encryption at rest (field level) | `PARTIAL` | Only `PatientProfile.national_id`. **Legacy plaintext rows still need a one-time re-encryption command** | P1 |
| 19 | General audit event log | `PARTIAL` | Three narrow trails now (`DoctorAccessLog`, `EmergencyCardView`, `Consent`). **Still no audit of login, upload, deletion, export** | P1 |
| 20 | Notifications, privacy-aware previews | `PARTIAL` | `Notification` + FCM exist; **push payload content not reviewed for PHI leakage on lock screens** | P1 |
| 21 | Deletion propagates to embeddings | `DONE` | Vectors are in-row → `CASCADE` removes them; `rag_vector_store/` is empty | — |
| 21 | Account deletion | `DONE` | `purge_user_data()` behind password confirm, `apps/accounts/views.py:290-301` | — |
| 21 | Data export (GDPR portability) | `DONE` | `apps/accounts/export.py`; ZIP of 10 JSON categories + original files; web `/accounts/export/` + `GET /api/v1/export/download/`. No migration | — |
| 21 | Export includes consent history | `DONE` | `consent.json` — current state + full append-only history including revocations | — |
| 21 | Export excludes credentials/secrets | `DONE` | Password hash, JWT/OAuth tokens, `emergency_token`, FCM device tokens, API keys, settings all excluded and documented in every manifest | — |
| 21 | Export is strictly user-scoped | `DONE` | Subject is always `request.user`; supplied identifiers ignored (regression-tested); archive paths built from UUIDs, never stored filenames | — |
| 22 | Web sections (dashboard/records/documents/chat/settings) | `DONE` | 8 URL namespaces, see ARCHITECTURE §3 | — |
| 22 | Measurement UI (value/unit/date/range/trend) | `DONE` | Chart.js payloads from `trajectory_service.get_chart_data` | — |
| 22 | Accessibility (semantic HTML, focus, contrast, reduced motion) | `MISSING` | Never audited | P2 |
| 23 | Mobile app implementation | `N/A` | Separate repository | — |
| 23 | Mobile-facing API + JWT | `DONE` | `/api/v1/` — 40 endpoints, SimpleJWT with rotation (`settings.py:331-335`) | — |
| 24 | Sync / offline queue / conflict resolution | `MISSING` | No `updated_at`-based delta endpoints, no server-side conflict handling | P2 |
| 25 | Search over records/measurements/documents | `PARTIAL` | RAG search is strong; **no plain filtered list search** by date/type/source | P2 |
| 26 | Unit / date / value validation | `DONE` | `unit_known` flag + `DATE_FORMAT_PREFERENCE` for eu/us ambiguity (`settings.py:350`) | — |
| 26 | Duplicate document / measurement detection | `MISSING` | No `checksum` → same file can be uploaded repeatedly | P1 |
| 26 | extracted / inferred / uncertain / user-confirmed distinction | `MISSING` | No confidence or confirmation fields | P1 |
| 27 | Contradictory records surfaced, not silently resolved | `DONE` | R6: `conflict_service.py` distinguishes progression / conflict / duplicate; conflicts reach the model with both values and sources, unresolved | — |
| 28 | Background jobs | `PARTIAL` | Indexing uses a **background thread**, not a job queue (`rag_assistant/signals.py:27`) — no retry, no visibility, dies with the worker | P1 |
| 28 | Processing status in UI | `PARTIAL` | No `status` field on `MedicalRecord` to show | P1 |
| 28 | No raw health content in general logs | `PARTIAL` | Log levels are sane, but `apps` logger is at `DEBUG` (`settings.py:312`) — needs a PHI-leak review | P1 |
| 29 | Rate limiting | `DONE` | DRF throttles across `/api/v1/` (`apps/api/throttling.py`), scoped per endpoint class; web login/register/password-reset limited with django-ratelimit; all rates env-configurable | — |
| 29 | Cache invalidation on data change | `MISSING` | No AI/RAG response caching to invalidate (acceptable) | P3 |
| 30 | Documented API contract | `MISSING` | No OpenAPI schema; mobile client integrates against undocumented endpoints | P1 |
| 31 | Unit + integration tests | `PARTIAL` | 282 tests passing; `apps/api` now covered for security, but its business logic and `apps/notifications` remain untested | P1 |
| 31 | Security tests (IDOR, injection, upload, prompt injection) | `DONE` | ~185 security tests. XSS, CSRF, SSRF, upload and API-abuse audited 2026-08-12 — `apps/accounts/test_appsec.py` | — |
| 31 | SQL injection | `N/A` | No raw SQL or string-built queries anywhere; all access via the ORM | — |
| 32 | RAG evaluation benchmark | `DONE` | `evaluation/` with committed results + charts | — |
| 32 | Evaluation targets / regression gate | `PARTIAL` | Deterministic baseline committed (`evaluation/rag_deterministic_baseline.json`) and pinned by 31 tests; still no agreed threshold for the LLM-dependent metrics | P2 |
| 32 | Benchmark covers latest/historical/conflicting/ambiguous | `DONE` | `scripts/evaluation/rag_eval_dataset.py` — 15 property-based cases across 8 categories | — |

---

## Security findings (Phase 3, 2026-08-12)

### Fixed — privilege escalation via API registration (critical)

`RegisterSerializer` exposed `role` as a writable field and passed it straight to
`create_user()`, while `CustomUser.is_approved` defaults to `True`. A single
unauthenticated request could therefore mint an **approved** privileged account:

```
POST /api/v1/auth/register/   {"email": ..., "password": ..., "role": "hospital_admin"}
```

The escalation chain to PHI:

1. `hospital_admin` → the dashboard lists **every patient and doctor**
   (`apps/dashboard/views.py:56-57`) — the patient roster leaks immediately.
2. `create_link` lets that admin link **any** doctor to **any** patient
   (`apps/dashboard/views.py:141`).
3. Register a second account as `doctor`, link it, then read the victim's full
   record set through `patient_records`.

`role: "data_scientist"` separately unlocked the MLOps monitoring view.

The server-rendered `RegisterForm` never exposed `role`, so this was API-only.
Fixed by removing `role` from the serializer's fields and hard-coding
`Role.PATIENT`. Regression tests: `PrivilegeEscalationTests`.

### Audited and found correct

Every `/api/v1/` and web view that touches a user-owned object already filtered
by `patient=request.user` / `user=request.user`. No IDOR was found in records,
conversations, appointments, alerts, notifications or predictions. Doctor access
correctly requires both the `doctor` role and an **active**
`PatientDoctorRelationship`, and writes a `DoctorAccessLog` row. Media serving
enforces ownership plus a path-traversal guard. These are now pinned by tests
rather than left to inspection.

### Documented, not changed — FCM token reassignment

`register_fcm_token` does `update_or_create(token=..., defaults={'user': ...})`,
so submitting a token already registered to another user **reassigns the device**.
This is the standard FCM pattern (a physical device legitimately changes hands at
logout/login) and the token is a secret, so it is left as-is. The residual risk:
anyone who obtains another user's FCM token can redirect their push
notifications. Mitigations in place: authentication required, and the endpoint is
now covered by the default user throttle. Revisit if notification payloads ever
carry clinical detail.

### Fixed — web registration 500

`register_view` called `login(request, user)` after `form.save()`. With three
entries in `AUTHENTICATION_BACKENDS` and no `backend` attribute on the new user,
Django raised `ValueError: You have multiple authentication backends
configured...` — **every successful web signup returned a 500**. Likely unnoticed
because Google OAuth is the common path.

Fixed by re-authenticating with the credentials just submitted rather than
asserting a login the auth stack never approved: `authenticate()` walks
`AUTHENTICATION_BACKENDS`, sets `user.backend`, and still enforces every backend
check including `user_can_authenticate`. If it returns `None` the user is sent to
the login page instead of the request failing. Google OAuth is untouched — it
goes through allauth, not this view. Regression tests: `WebRegistrationTests`,
including one asserting the session records
`apps.accounts.backends.EmailOrUsernameBackend`.

### Privileged account audit tooling

`manage.py audit_privileged_accounts` — read-only report of every account holding
`doctor`, `hospital_admin`, `data_scientist` or `admin`, plus any `patient` with
staff/superuser flags. Flags risk indicators:

- **`username equals email`** — the signature the vulnerable API path left, since
  `RegisterSerializer` set `username=email` while the web form collects a
  separate username;
- **missing role profile** — the admin/web paths create `DoctorProfile` /
  `HospitalAdminProfile` / `DataScientistProfile`; the API path did not;
- **approved with no record of who approved it**;
- **staff / superuser**.

Options: `--suspicious-only`, `--no-email` (redaction), `--json`. Output goes to
stdout only — never to the application logger and never through an HTTP endpoint,
because the output is a list of privileged identities. The command **never
modifies an account**; it prints revocation guidance instead, which recommends
demote-and-suspend over deletion so the `DoctorAccessLog` trail survives, and
points at GDPR Art. 33/34 if the log shows patient data was actually read.

Run on the dev database: 1 privileged account (the legitimate superuser), flagged
only for being a superuser. **Production has not been audited** — that requires a
shell on the deployment.

## Application security audit (2026-08-12)

**Found and fixed — 7:**

| # | Issue | Severity |
|---|---|---|
| 1 | **Stored XSS** — lab parameter names from uploaded documents rendered into inline `<script>` via `json.dumps` + `\|safe`; `</script>` is not escaped by `json.dumps` | High |
| 2 | **Stored XSS** — assistant answers passed to `marked.parse()` → `innerHTML`; marked emits raw HTML, and answers derive from retrieved document content | High |
| 3 | **Arbitrary file stored as profile picture** — validated on client-declared `Content-Type`, so an SVG announced as `image/png` was stored and later served from our own origin | High |
| 4 | **Uploads served as active content** — `serve_media` inferred the type from the filename, so a stored `.svg` came back as `image/svg+xml` | High |
| 5 | **Unbounded upload size** — `DATA_UPLOAD_MAX_MEMORY_SIZE` excludes file data and `FILE_UPLOAD_MAX_MEMORY_SIZE` is only a spool threshold, so no ceiling existed | Medium |
| 6 | **500 on malformed UUID** — `records/<pk>`, `predictions/<pk>`, `assistant/sessions/<id>` raised `ValidationError` | Medium |
| 7 | **Unvalidated enum** — `record_type` from the request body stored verbatim; Django does not validate `choices` on `save()` | Low |

Also fixed while in the area: the media traversal guard used
`startswith(root + '/')`, which hardcodes the POSIX separator (broken on Windows)
and would treat a sibling directory like `/media-evil/` as inside `/media`. Now
`Path.is_relative_to()`. And both outbound proxy calls now set
`allow_redirects=False`.

**Audited, no vulnerability found:**

- **SSRF** — every outbound URL in the codebase is a string literal. No endpoint
  accepts a URL, downloads a remote resource, or proxies to a caller-chosen host.
  An IP/DNS allowlist would be dead code; instead an `ast` test fails the build
  if a non-literal URL is ever passed to `requests.*`, which is exactly when such
  a validator becomes necessary.
- **SQL injection** — no raw SQL, no string-built queries.
- **CSRF** — Django middleware covers the session-authenticated web surface,
  verified with `enforce_csrf_checks=True`. JWT endpoints are not
  cookie-authenticated, so no CSRF requirement was added to them.

**Left open, deliberately:**

- ~~`seizure_realtime_*` unauthenticated compute DoS~~ — **fixed 2026-08-12** in
  the reliability phase: both endpoints are rate limited by IP (20/h load,
  120/h predict) and the loader now rejects uploads over `MAX_UPLOAD_BYTES`.
  They remain `csrf_exempt` and unauthenticated by design so the public demo
  keeps working; neither changes state.
- ~~Decompression amplification~~ — **fixed 2026-08-12**: parquet row count is
  read from the file footer via pyarrow *before* any data is materialised and
  rejected above `MAX_PARSED_ROWS` (200k).

## Production reliability audit (2026-08-12)

**Found and fixed — 6:**

| # | Issue | Impact |
|---|---|---|
| 1 | **No timeout on any external model call** — OpenAI/Anthropic SDKs default to 600s while gunicorn kills a worker at 120s | Resource exhaustion: a few hung upstream calls occupy all 16 threads |
| 2 | **A raising provider ended the request** — `generate()` had no guard around each call, defeating the point of a fallback chain | 500 instead of degradation |
| 3 | **Lost updates on `run_count`** — read-modify-write across 4 call sites | Silent undercount under concurrency |
| 4 | **One background thread per saved record** — a 200-document Kanta import spawned 200 threads, each with a DB connection | Connection/FD exhaustion on bulk import |
| 5 | **Patient questions logged** at INFO and DEBUG | Clinical content in ordinary application logs |
| 6 | **Health check ignored dependencies** — reported healthy with the database down | Broken instance kept taking traffic |

Also: background pool tasks now close their database connection in a `finally`,
since pool threads are long-lived and their connections would otherwise
accumulate.

**Audited, no change needed:**

- **Transaction boundaries** — record creation and lab-value writes are already
  inside `transaction.atomic()`; the indexing signal fires on `on_commit`.
- **Reminder idempotency** — `Appointment.reminded_*` flags make the 5-minute
  cron safe to re-run; a duplicate dispatch is not possible.
- **Cache correctness** — only used for throttling and a cached population-stats
  payload; no correctness-critical state.
- **Django production config** — HSTS, secure cookies, nosniff and
  `SECURE_PROXY_SSL_HEADER` are all set behind `if not DEBUG`.

**Deferred:**

- **No error tracking service.** Exceptions reach stderr and Railway logs only;
  there is no Sentry-equivalent, so no alerting, grouping or release tracking.
  This is the largest remaining observability gap. **P1.**
- **No metrics.** Latency, error rate and provider-fallback frequency are not
  measured; `QueryLog.llm_provider` is the closest proxy and requires a manual
  query. **P1.**
- **Backup/recovery is entirely Railway's managed Postgres default.** No
  documented RPO/RTO, no tested restore, and uploaded files on local disk are
  ephemeral between deploys unless `OBJECT_STORAGE_URL` is set. **P0 for any
  real deployment** — untested backups are not backups.
- **Indexing is still in-process.** The bounded pool fixes exhaustion, but work
  is lost if the worker dies mid-task and there is no retry or dead-letter path.
  A real queue is the correct answer if ingestion volume grows. **P2.**

## RAG quality — baseline, then optimization (2026-08-12)

Two passes. The audit pass tuned nothing and produced a dataset, a reproducible
baseline and the failure list below. The optimization pass then addressed
R1→R1b→R3→R6→R5→R4 in that order, measuring after each.

**Still untouched throughout:** embedding model, chunk size, chunk overlap,
top-k, reranker, similarity threshold, BM25/semantic weights, time-decay
parameters. No migration was created.

### Result

| Metric | Before | After |
|---|---|---|
| Route accuracy (8 asserted) | 25.0% | **100.0%** |
| Temporal recognition (9 asserted) | 22.2% | **100.0%** |
| `temporal_latest` / `temporal_previous` routing | 0% | **100%** |
| `temporal_trend` routing | 100% | 100% (no regression) |

All seven findings are now closed in code and pinned by acceptance tests.

### Findings

| # | Finding | Evidence | Severity |
|---|---|---|---|
| **R1** | **`latest` / `most recent` / `current` / `previous` / `newest` are not in the temporal keyword list**, so recency questions route to plain hybrid retrieval with no ordering step | Route accuracy 0% on `temporal_latest` and `temporal_previous`; 25% overall | **High** |
| **R2** | Time decay is the only recency signal on that path, and it is weak: no penalty under 365 days, ≤15% after | A 2-year-old chunk scoring 0.60 still beats a current one scoring 0.50 | **High** |
| **R3** | The record date appears only in the first chunk's header line; wider panels leave later chunks holding lab values with no date in the text the model sees | `test_baseline_wide_panel_splits_and_later_chunks_lose_the_date` | **High** |
| **R4** | Chunking splits on whitespace with no line awareness, so a window edge can fall between an analyte and its value | `test_baseline_word_window_can_split_an_analyte_from_its_value` | Medium |
| **R5** | Chunk metadata has no page, section or offset, so a citation can name a document but never point inside it | `test_baseline_chunk_metadata_has_no_page_or_section` | Medium |
| **R6** | No conflict detection anywhere — contradictory records are passed to the model with no signal, and handling is entirely prompt-dependent | `test_baseline_no_conflict_detection_exists` | Medium |
| **R7** | Retrieval weights are configurable but classifier vocabulary is hardcoded, so R1 needs a code change not a settings change | `test_baseline_keyword_lists_are_hardcoded_not_configurable` | Low |

R1 is the headline: the implementation plan names these exact words as requiring
temporal logic, and the trajectory machinery that does the ordering **already
works correctly** — `TrajectoryOrderingTests` proves it returns all points
oldest→newest, patient-scoped, with the newest present. The data supports the
right answer; the classifier withholds it.

### What was implemented

| # | Change | Where |
|---|---|---|
| R1 | Recency vocabulary (`latest`, `most recent`, `current`, `newest`, `previous`, `prior`) added as a **guarded** family: only temporal when the query names the patient or a biomarker, so "the latest clinical guidelines" stays general. Routing further requires that trajectory can actually order the thing asked about, so medication/diagnosis recency keeps its own route. | `query_understanding.py` |
| R1b | `temporal_mode` ∈ {`latest`, `previous`, `trend`, `None`} on `QueryIntent`, threaded through graph state to `TrajectoryService`, which prepends an explicit line naming the requested value. Trend wins when a query mixes both, because a trend answer contains the latest value and not vice versa. | `query_understanding.py`, `graph/state.py`, `graph/nodes.py`, `trajectory_service.py` |
| R3 | Continuation chunks carry a short `[Title — date] (continued)` header, so a chunk holding lab values always states when they were taken. Chunk 0 is untouched (it already opens with that line). | `document_processor.py` |
| R6 | `conflict_service.py` — deterministic grouping by analyte into `progression` / `conflict` / `duplicate`. Only same-analyte-same-date disagreements are called conflicts; the notice names both values and their sources and does not resolve them. | `conflict_service.py`, `trajectory_service.py` |
| R5 | Exact `start_offset`/`end_offset` per chunk, surfaced in citations. Page and section are **omitted, not guessed** — the PDF parser joins pages before chunking, so page boundaries no longer exist there. | `document_processor.py`, `generation_service.py` |
| R4 | Window boundaries are nudged backwards (never forwards, max 6 tokens) so a chunk never ends on a label, orphaning its value. | `document_processor.py` |

### Re-indexing required by R3 + R5 + R4

These change **chunk text and metadata**, so affected chunks must be rebuilt.
Chunk text feeds the embedding, so their vectors are stale too.

**Not executed.** The exact command:

```bash
# scope first — how many chunks would change
python manage.py shell -c "
from django.db.models import Count
from apps.rag_assistant.models import MedicalChunk, MedicalDocument
print('continuation chunks:', MedicalChunk.objects.filter(chunk_index__gt=0).count())
print('multi-chunk docs   :', MedicalDocument.objects.annotate(n=Count('chunks')).filter(n__gt=1).count())
"

# then rebuild documents + chunks and re-embed
python manage.py reindex_records --clear            # all patients
python manage.py reindex_records --patient <id>     # one patient
```

**What changes:** only documents large enough to split. Chunk 0 of every
document is byte-identical, so single-chunk documents need nothing. On the dev
database that is **0 of 10 chunks**. Production scope must be measured with the
query above before deciding.

`reindex_all_embeddings --stale-only` is **not** the right command here: it
re-embeds by provenance mismatch, but the chunk *text* itself must be
regenerated, which only `reindex_records` does.

### Metrics not reported, and why

- **Recall@k** — no per-chunk relevance labels; only document-level recall is meaningful.
- **Reranker effectiveness** — needs paired on/off runs against live Groq; not reproducible in the suite.
- **Latency / provider-fallback frequency** — `QueryLog.llm_provider` makes fallback
  frequency derivable from production data, but there is no timing column and no
  metrics backend, so no number is invented here. Ties to the P1 observability gap.

## Evaluation status (2026-08-13) — read before quoting any RAG number

### What is valid

| Source | Status |
|---|---|
| `evaluation/rag_deterministic_baseline.json` | **valid** — no LLM, no quota, reproducible |
| `apps/rag_assistant/test_*` (581 tests) | **valid** — deterministic, no network |
| `evaluation/rag_quality_results.json` (golden dataset, 30 items) | **valid** — all 30 retrieved context |
| `evaluation/rag_llm_eval_results.json` (new dataset, 15 items) | ⚠ **PARTIALLY INVALID** — 10 of 15 usable |

### Why the new-dataset run is partially invalid

The **Gemini free-tier embedding quota (1,000 requests/day) was exhausted
mid-run**. Five cases retrieved zero chunks and were scored against an empty
context. The file now carries a `validity` block naming them; each case also has
`valid` and `invalid_reason` fields.

**These must not be treated as quality measurements:**

- **`injection-ignore-instructions`, `injection-exfiltrate`** — PASS means there
  was no injected content to resist, not that injection was resisted. Genuine
  prompt-injection coverage lives in
  `apps/rag_assistant/test_prompt_injection.py`, which needs no quota.
- **`unanswerable-no-such-test`** — refused because nothing was retrieved, not
  because it recognised an unanswerable question.
- **`conflict-medication-status`** — failed with an all-providers-unavailable
  fallback; an infrastructure artefact, not a conflict-handling result.
- **`factual-glucose-any`** — zero context.

The 7 temporal cases **are** valid: the trajectory path is ORM-based and needs no
query embedding, so it was unaffected by the quota.

### Known scoring flaw — correct before re-running

The three `temporal_latest` cases were scored FAIL, but the answers were correct
(*"your most recent glucose measurement is 7.8 mmol/L"*). The dataset declares
`must_not_contain: ['5.1']`, while R1b deliberately includes the full series
alongside the latest value — so the expectation contradicts the implemented
design. Correction is specified in ARCHITECTURE §5c. Hand-scored under the
corrected rule, temporal correctness is **7/7**; that is a manual check and must
be reproduced by a real run before being quoted.

### Next run — scope

1. Apply the corrected temporal scoring first.
2. Add **trajectory citation assertions** (`n_sources > 0` on every temporal
   case). The previous run recorded `sources=0` throughout — that was the
   attribution regression fixed 2026-08-13.
3. Budget ~75 embedding calls per full pass; run both harnesses the same day.
4. **Tune nothing until the new results are reported.**

## Controlled evaluation corpus + baseline (2026-08-13)

The `sara.m` demo data was audited and found **insufficient** as a test fixture:
one patient (isolation untestable), longest document 167 words against a
200-word chunk size (chunking never splits, so R3/R4 unreachable), and zero
same-analyte-same-date pairs (R6's conflict rule can never fire).

Built `scripts/evaluation/eval_corpus.py` — 3 patients, 17 records, 107 lab
values, 18 chunks, with fixtures for every previously-untestable path. Cases and
criteria in `corpus_cases.py`; runner `run_corpus_eval.py --offline|full`.

### Baseline — offline half (no embedding quota needed)

14 of 22 cases; 8 need vector retrieval and are pending quota.

| Dimension | Result |
|---|---|
| Temporal (latest/previous/trend) | 6/6 — **but see C2** |
| Citations | 1/1, citation rate **100%** |
| Isolation | 2/2 |
| Unanswerable / refusal | 2/2, refusal correctness **100%** |
| Conflict | 1/1 |
| Hallucination | 1/2 — **see C3** |
| **Overall** | **13/14 (92.9%)**, 0 errors, median 11.9 s |

Structural checks: 1 document splits into 3 chunks, both continuation chunks
carry the record date (R3 ✓), no chunk ends on a label (R4 ✓), all 15 chunks
have offsets (R5 ✓), glucose correctly classified `conflict` and HbA1c
`progression` (R6 ✓), 4/4 trajectory citations carry `document_id`.

### New defect found — C1: contested newest date

**Severity: high. Not fixed — no optimization without review.**

When two readings share the newest date, `_point_in_time_line()` and
`_compute_trend()` both silently pick one arbitrarily. With glucose 7.8 and 5.2
both dated 2026-05-20, the assistant answered:

> *"your most recent glucose measurement was **5.2** mmol/L … total change is
> **+4.0%**, slope +0.014 mmol/L/month"*

Two consequences: "most recent" is an arbitrary choice presented as fact, and the
**trend maths is computed from the arbitrary pick** (+4.0% instead of ≈+56% from
5.0 to 7.8). The conflict notice *is* emitted alongside, which is the designed
behaviour, but the assertion and the arithmetic are still made on one value.

Candidate remedy (not implemented): when the newest date is contested, suppress
the point-in-time line and the trend figure and defer to the conflict notice.

### Evaluation-criteria defects found (must be fixed before the numbers are quoted)

- **C2** — `must_contain: ['7.8']` passed on an *incidental* mention inside the
  trend narrative while the headline claim was 5.2. The criterion must be
  "names X **as the most recent**", not "mentions X anywhere". **Temporal 6/6 is
  therefore not trustworthy** until this is tightened.
- **C3** — `halluc-unknown-unit` was scored FAIL for containing "140", but the
  answer listed it inside a sourced conflict disclosure. That is transparency,
  not hallucination; the criterion is wrong.
- **C4** — the duplicate branch of R6 is still untested: creatinine spans several
  dates so its group classifies as `progression`, which dominates. Testing
  `duplicate` needs an analyte whose only readings are the same-date pair.

### Still pending embedding quota

8 cases: retrieval, answer quality, injection (2), unanswerable-no-such-test,
citation-absence, medication conflict, and the deep multi-chunk analyte lookup.
Free-tier embedding quota (1000/day) was exhausted at the time of the run;
`retryDelay` in the error is misleading — it is a per-day quota.

## Decision: MIMIC-III/IV not used for RAG evaluation (2026-08-13)

A local copy of MIMIC-IV 3.1 (+ a MIMIC-III sibling) was audited and **not
adopted**. Recorded so the question is not reopened without new information.

**Audit result — the data is genuinely good.** 364,627 patients, 546,028
admissions, dense longitudinal labs (12,496 subject/analyte pairs with >=5 dated
readings in a 300k-row sample), medications, ICD diagnoses, and — in MIMIC-III
only — discharge summaries averaging 1,478 words, roughly 7 chunks each.

**Why it was declined anyway:**

1. **Licence.** PhysioNet Credentialed Health Data License v1.5.0, clause 3:
   *"The LICENSEE will not share access to PhysioNet restricted data."* Our RAG
   pipeline transmits chunk text to Groq, Google, Anthropic and OpenAI, and
   embeds every query through Gemini. Running an end-to-end evaluation on this
   data would send credentialed patient records to third parties. PhysioNet
   guidance treats online LLM APIs as a DUA violation absent an approved
   pathway. This is real de-identified patient data, not synthetic.
2. **Identifiers do not link.** MIMIC-III and MIMIC-IV `subject_id`s are not
   joinable, so a patient with both dense labs and long notes would have to come
   from MIMIC-III alone — a second dataset to govern for one property.
3. **Marginal benefit.** The only thing it uniquely offered was realistic long
   clinical prose. Everything else — contested same-date values, planted
   injection payloads, decoy patients, deliberate absences — has to be
   constructed regardless, and the synthetic corpus already does it.

**What we lose, and the cheaper substitute.** Real notes are messy in ways
generated text is not: abbreviations, templated headers, irregular whitespace,
de-identification placeholders such as `[**2151-8-5**]`. If chunk-boundary
behaviour against messy text becomes worth measuring, the answer is a synthetic
long-document fixture with those properties, which carries no licensing
exposure. Not built yet — it is not currently the bottleneck.

**If this is ever revisited**, the blocker to resolve first is (1): either an
approved LLM pathway with a signed agreement and retention opt-out, or restrict
MIMIC to the pure-ORM half of the evaluation (chunking, conflict detection,
trajectory ordering, citation plumbing) which makes no external calls. Note that
even retrieval is out of scope under that restriction, since queries embed via
Gemini.

## M1 fixed — singular/plural keyword matching (2026-08-13)

**Root cause.** Keyword matching was exact: `platelet` does not match
"platelets". Since `platelets` is the *canonical* biomarker name, it failed to
match its own alias list, so `detect_biomarker('my platelets')` returned None and
platelet questions never reached the trajectory path — they fell through to the
generic timeline with no point-in-time answer. Confirmed on 15/15 MIMIC patients.

**Fix.** New `apps/rag_assistant/services/text_match.py`. Each alias expands into
a small, explicit set of inflected forms (`+s`, `+es`, `-is → -es`,
`consonant+y → -ies`, plus the singular when the alias is already plural) matched
with boundary lookarounds. Deliberately *not* a substring match, which would have
fixed the symptom while matching `bp` inside "bpm" and `a1c` inside "ha1c".

Lookarounds `(?<![a-z0-9]) … (?![a-z0-9])` replace `` because several aliases
end in non-word characters — `na\+` can never match, `na+` now does.

Applied at three call sites: `trajectory_service._biomarker_alias_match`,
`query_understanding._biomarker_alias_match`, `query_understanding._kw_match`.

### Before / after — MIMIC offline trajectory audit (120 series, 15 patients)

| Metric | Before | After |
|---|---|---|
| Chronological order correct | 105/120 | **120/120** |
| Latest value correct | 105/120 | **120/120** |
| Previous value correct | 105/120 | **120/120** |

All 15 failures were `Platelet Count`; all 15 resolved. No other metric moved.
Outbound API attempts during the re-run: **0**.

### Routing improvements that came with it

Two long-standing misroutes were the same defect and are now fixed:

- *"Were any of my December **results** flagged as critical?"* — was `general`
  (`result` missed the plural), now `lab_results`.
- *"What are my current **medications**?"* — was mis-routed, now `medications`.

Non-regression asserted for temporal routing and general-knowledge routing.

**Tests:** `apps/rag_assistant/test_plural_matching.py`, 24 tests — inflection
rules, the plurals that must match, and the near-misses that must *not*
(`bpm`, `ha1c`, `plateletpheresis`, `recreate`), which is what separates this
from a substring fix.

## New finding — M2: the two biomarker tables have diverged (NOT fixed)

Surfaced while fixing M1. There are two independent biomarker alias tables:

| Table | Keys |
|---|---|
| `trajectory_service._BIOMARKERS` | 16 |
| `query_understanding._BIOMARKER_ALIASES` | 10 |

Missing from the classifier: **blood_pressure, heart_rate, platelets, urea, wbc,
weight**.

Consequence: `QueryIntent.biomarker` is None for those six. Since `_route_kw()`
uses `biomarker` to decide whether a recency question takes the trajectory path,
a question like *"what is my latest platelet count?"* can still miss trajectory
even though `TrajectoryService` itself now resolves the biomarker correctly.

Not fixed — outside the M1 scope. The fix is to make one table the single source
of truth rather than to re-sync two copies, which would only diverge again.

## M2 fixed — one biomarker definition (2026-08-13)

**Root cause.** The vocabulary existed twice: `trajectory_service._BIOMARKERS`
(16 entries) and `query_understanding._BIOMARKER_ALIASES` (10). They drifted.
Diffing them showed the trajectory copy was a strict superset — the classifier's
copy was simply stale, missing **blood_pressure, heart_rate, platelets, urea,
wbc, weight** plus ~12 aliases on shared entries (`k+`, `na+`, `bun`, `fbs`,
`kidney function`, …). Because `_route_kw()` consults `QueryIntent.biomarker`
when deciding whether a recency question takes the trajectory path, a biomarker
the classifier could not name was a biomarker whose recency questions were
misrouted.

**Fix.** New `apps/rag_assistant/services/biomarkers.py` holds the table plus
`detect()` / `aliases_for()`. **The second table is deleted, not topped up** —
both modules import the shared one, so they cannot diverge again.
`trajectory_service._BIOMARKERS` is now an alias of the shared dict, keeping the
module's internal callers untouched. M1's inflection matching is preserved
unchanged.

### Before / after — all 16 biomarkers through the full routing path

| | Before | After |
|---|---|---|
| Biomarkers the classifier can name | 10/16 | **16/16** |
| `QueryIntent.biomarker` populated | 10/16 | **16/16** |
| *latest* questions reaching trajectory | 10/16 | **16/16** |
| *previous* questions reaching trajectory | 10/16 | **16/16** |
| *trend* questions reaching trajectory | 10/16 | **16/16** |

Every one of the ~90 aliases resolves to its own canonical name — no shadowing.

### MIMIC offline trajectory audit — unchanged, as expected

120/120 on ordering, latest and previous, exactly as after M1. M2 fixes the
*classifier*, and the MIMIC audit calls `TrajectoryService.detect_biomarker`
directly, so it could not have observed the defect. Outbound API attempts: **0**.

**Tests:** `apps/rag_assistant/test_biomarker_source.py`, 17 tests — including a
source scan that fails if a second biomarker table ever reappears in
`services/`, an alias-shadowing check across the whole table, and a guard that
`bpm` still resolves to heart_rate rather than being captured by blood_pressure's
`bp`.

## Open P0 — hasanai.net seizure proxy

Separate from the egress rollout, and **must not be enabled in production as
part of it**. The audit found, in one endpoint:

- the raw uploaded EEG file leaves HealthCompass;
- the user-supplied filename leaves with it, and may itself identify a person;
- the outbound request is **unauthenticated** — no key, no service identity;
- the inbound web view is `csrf_exempt` and requires **no login**;
- retention and deletion at the receiving service are **undocumented**;
- there is **no data-processing agreement**.

EEG recordings are Art. 9 special-category data. Consent alone does not make a
transfer lawful when the processor relationship is undefined.

**Recommended remediation, in order:**

1. Keep `insights.seizure_proxy` **out of** `CONSENT_ENFORCED_EGRESS` — enabling
   it would imply the transfer is otherwise sanctioned. Instead, disable the
   feature at the route level for production until step 2 or 3 lands.
2. Obtain a DPA with the operator of `hasanai.net` covering purpose, retention,
   deletion, sub-processors and location; authenticate the call; strip the
   filename and send an opaque id.
3. If no DPA is achievable, bring the model in-house. Local ONNX inference
   already exists for the realtime variant (`seizure_realtime_predict_chunk`),
   so the ensemble is the only part that needs replacing.

Nothing has been removed — the feature still works exactly as before, gated by
consent in code but not enforced in production.

## Where the plan is wrong and should be edited

1. **§2 local-first is not this product.** Rewrite as *server-side processing
   with data minimization, explicit consent, and guaranteed deletion*. Keep
   local-first only as a mobile-cache concern in the mobile repo.
2. **§3 assumes `USER 1:1 PROFILE`.** Reality is a role-discriminated user with
   four profile tables. Correct the plan.
3. **§5 assumes one `MEASUREMENT` table.** Reality is two, split by source, and
   the split is correct. Correct the plan.
4. **§9 contradicts itself** — defines `RETRIEVAL_LOG.query_text` then says not
   to store query content. Resolve: store it, and govern it with
   `QUERYLOG_RETENTION_DAYS` (already implemented).
5. **§33 references an `EMBEDDING` table that no section defines.** Embeddings
   are a column here. Remove or define it.
6. **The plan has no section for third-party LLM processing of Article 9 data**,
   despite four providers receiving PHI. This is the largest omission.
7. **The plan omits the emergency card entirely** — an unauthenticated public
   endpoint keyed by a UUID token. It needs its own threat model.
8. **The plan's arrows are mojibake** (`â` instead of `→`) throughout. Re-save
   as UTF-8; an agent reads those literally.

---

## Recommended order of work (Phase 2 candidates)

Ranked by risk × effort. **None of these are started.**

| # | Work item | Why now | Migration? |
|---|---|---|---|
| ~~1~~ | ~~**Record embedding model + dimension per chunk**~~ | **DONE 2026-08-12** — migration `0009`. Remaining follow-up: run `backfill_embedding_provenance --apply` in production, then set `EMBEDDING_STRICT_PROVENANCE=True` | Applied |
| ~~2~~ | ~~**Rate-limit auth, password reset, uploads, and all `/api/v1/`**~~ | **DONE 2026-08-12** | No |
| ~~3~~ | ~~**Prompt-injection clause in all four system prompts**~~ | **DONE 2026-08-12** | No |
| ~~4~~ | ~~**Consent model + AI-processing consent gate**~~ | **DONE 2026-08-12** — migration `accounts/0008_consent` | Applied |
| 5 | **GDPR data export** | Legal requirement; deletion already exists, export does not | No |
| 6 | **`checksum` / `mime_type` / `file_size` / `status` on `MedicalRecord`** | Unblocks duplicate detection *and* processing-status UI in one migration | Yes (additive) |
| 7 | **Generalized `AuditEvent`** | Login, upload, delete, export, consent change are unaudited | Yes |
| ~~8~~ | ~~**IDOR + security test suite over `/api/v1/`**~~ | **DONE 2026-08-12** — found and fixed a privilege-escalation chain | No |
| 9 | **Intended-use statement in the repo** | Cheap, and it constrains every later product decision | No |

Items 2, 3, 5, 8 and 9 require **no migration** and can start immediately.
