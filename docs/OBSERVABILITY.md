# Observability

**Status: structured operational events implemented. External error-tracking
destination is a DEPLOYMENT DECISION and is not wired — see §4.**

---

## 1. Why this exists

Three defects found during the audit were invisible in production and were
caught only by reading source code:

| Defect | What it logged |
|---|---|
| XML hardening fell back to the unsafe stdlib parser | nothing at all |
| Criticality check ran on 1 of 3 ingestion paths | nothing at all |
| Embedding failures made records permanently unretrievable | one ERROR line, then forgotten |

Ordinary log lines were not enough. They are unstructured prose, so no alert
rule can key on them reliably, and the one ERROR line was buried under
DEBUG-level retrieval logging that was enabled in production.

## 2. Operational events

`healthcompass/observability.py` emits **stable, machine-readable codes**:

```
event=EMBEDDING_FAILED chunks=50 error_type=RuntimeError patient_id=… patient_impacting=True
```

| Code | Meaning | Patient-impacting |
|---|---|---|
| `EMBEDDING_FAILED` | Chunks unretrievable until retried | ✅ |
| `EMBEDDING_NO_VECTOR` | Provider returned no usable vector | ✅ |
| `INDEXING_FAILED` | Background indexing raised; record may never be searchable | ✅ |
| `INGESTION_PARSE_FAILED` | A document failed to parse | ✅ |
| `ALERT_CREATION_FAILED` | **The patient was NOT notified of abnormal values** | ✅ |
| `RETRIEVAL_MISSING_EMBEDDINGS` | Retrieval excluded unembedded chunks | ✅ |
| `UNSAFE_DOCUMENT_REJECTED` | Hardened parser rejected an upload | — |
| `LLM_ALL_PROVIDERS_FAILED` | Every provider failed; user saw the fallback | — |

`patient_impacting=True` means a patient may be seeing incomplete or missing
medical information. **That is the field to alert on.**

Codes are stable strings. Renaming one is a breaking change for whoever is on
call.

## 3. PHI safety is enforced, not intended

`emit()` accepts **scalars only** — ints, floats, bools, None, UUIDs, and short
identifier-like strings (≤ 64 chars, no newlines).

Anything else:

- **raises `ValueError` when `DEBUG=True`** — a developer who writes
  `content=chunk.text` finds out immediately;
- **is replaced with `<redacted:non-scalar>` in production** — a logging bug can
  never become a PHI incident.

Audited separately: no existing log statement in the codebase embeds patient
content. Queries are logged as `q_len=37`, never as text. The `apps` logger now
runs at `INFO` in production (`DEBUG` only when `DEBUG=True`), because
per-request retrieval detail was burying the lines that matter.

## 4. PENDING — routing events to an alerting destination

Events currently go to stdout via the `healthcompass.ops` logger, which is
configured separately from `apps` precisely so it can be subscribed to on its
own.

**No external error-tracking service has been introduced.** Choosing a vendor is
a deployment decision, and the architecture does not require one to make these
events observable. Options, in increasing order of effort:

1. **Railway log drain** (no code change) — forward stdout to a log service and
   alert on `patient_impacting=True`.
2. **A logging handler** — add a handler to the `healthcompass.ops` logger in
   `settings.LOGGING`. No application code changes; `emit()` already attaches the
   full payload as `record.ops_event` for structured handlers.
3. **Sentry** — add `sentry-sdk`, initialise with a `SENTRY_DSN` env var, and
   attach its logging integration to `healthcompass.ops`. This adds a dependency
   and an external processor, so it needs the same consent/DPA review as any
   other third party receiving system telemetry.

Until one of these is configured, **failures are recorded but nobody is
notified.** That is an improvement on silence, but it is not alerting.

## 5. Health checks

`railway.toml` now points at `/health/ready/` rather than `/health/`.

| Endpoint | Checks | Use |
|---|---|---|
| `/health/` | process is up | liveness |
| `/health/ready/` | database + cache reachable | readiness — returns 503 when a dependency is down |

Previously the platform health check used the liveness endpoint, so a container
with a dead database reported healthy and kept receiving traffic.

## 6. Still missing

- **No metrics** (request rate, latency, error rate, queue depth).
- **No alerting** until §4 is configured.
- **No dashboard** for the `index_status` counters (`pending/failed/blocked`),
  which are currently only reachable via the management command.
