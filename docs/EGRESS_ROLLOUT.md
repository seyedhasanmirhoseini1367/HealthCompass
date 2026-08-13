# Rollout — `CONSENT_ENFORCED_EGRESS=all`

> **Status: prepared, NOT enabled.** Production is still `rag`.
> This document is the deployment change and its rollback.

## What is implemented vs. what is enabled

| | In code | Enabled in production |
|---|---|---|
| Consent model, versioning, revocation | ✅ | ✅ |
| Guard + registry for all 11 egress points | ✅ | — |
| Gate on ask/stream (`rag.*`) | ✅ | ✅ **enabled** |
| Gate on upload indexing (`rag.embed_documents`) | ✅ | ❌ **not enabled** |
| Gate on document parsing (`records.parse`) | ✅ | ❌ **not enabled** |
| Gate on OCR (`records.ocr`) | ✅ | ❌ **not enabled** |
| Gate on prediction interpretation | ✅ | ❌ **not enabled** |
| Gate on seizure proxy + its interpretation | ✅ | ❌ **not enabled** |

Everything in the "not enabled" rows is written, wired and covered by tests that
assert the provider is never called. They are inert only because
`CONSENT_ENFORCED_EGRESS` still reads `rag`.

## No staging environment exists

`railway.toml` defines a single production service and `railway.cron.toml` a
cron service against the same repo. There is no staging service and no
per-environment settings module, so **this change cannot be rehearsed against a
deployed environment today**, and the production `.env` has deliberately not
been touched.

Two options, in order of preference:

1. **Create a staging Railway service** from the same repo with its own
   `DATABASE_URL`, set `CONSENT_ENFORCED_EGRESS=all` there, exercise upload →
   OCR → assistant with and without consent, then promote.
2. **Canary in production** using the per-point list, lowest blast radius first:

   ```
   CONSENT_ENFORCED_EGRESS=rag,insights.seizure_proxy
   CONSENT_ENFORCED_EGRESS=rag,insights.seizure_proxy,records.ocr
   CONSENT_ENFORCED_EGRESS=rag,insights.seizure_proxy,records.ocr,records.parse
   CONSENT_ENFORCED_EGRESS=all
   ```

   Ordered by user impact: the seizure proxy is a demo surface, OCR is opt-in
   per scan, parsing degrades silently to regex, and record indexing is last
   because it is the one that quietly changes assistant answer quality.

## Prerequisite — ship consent UI first

Enforcement is default-deny and consent **must not be back-filled**. Before
enabling, confirm:

- [ ] `/accounts/consent/` is reachable and linked from the profile/settings nav
- [ ] Mobile has shipped a build that can call `POST /api/v1/consent/grant/`
- [ ] Users have been told, and given a window to consent
- [ ] `SELECT count(*) FROM accounts_consent WHERE purpose='external_llm' AND status='granted'`
      is a meaningful fraction of active users

Enabling before this leaves every existing user unable to use the assistant or
have uploads processed until they act.

## The change

Railway → service → Variables:

```
CONSENT_ENFORCED_EGRESS=all
```

Redeploy. No migration, no data change, no restart ordering requirement.

## Verification after enabling

```bash
# Expect enforced=True for all ten PHI points, False for knowledge.embed
python manage.py shell -c "
from apps.accounts.egress import egress_matrix
for r in egress_matrix():
    print(f\"{r['id']:<34}{r['phi']!s:<7}{r['enforced']}\")
"
```

Then, as a user **without** consent: upload a document (expect it saved,
lab values still extracted by regex, no embedding), run OCR (expect 403 with
`consent_required`), ask the assistant (expect 200 with the consent message).
Grant consent and repeat — all should now work.

## Rollback

```
CONSENT_ENFORCED_EGRESS=rag
```

Redeploy. Instant and stateless — the setting is read per request, nothing is
persisted, and no consent record is created or destroyed by either direction.

**Backlog after a rollback:** records uploaded while enforcement was on were
saved and chunked but not embedded. They are not lost and not corrupt; they are
simply missing from the assistant's index. Re-embed them with:

```bash
python manage.py reindex_all_embeddings --stale-only
```

The same command is what you run after users grant consent, for the records that
were skipped while they had not.
