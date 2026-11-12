# Deployment (after the 2026-11 production-readiness work)

Processes (see `Procfile`):

| Process | Command | Count |
|---|---|---|
| release | `bash scripts/release.sh` (backup, then migrate) | once per deploy |
| web | `bash startup.sh` (gunicorn) | 1+ |
| worker | `python manage.py rqworker ingest default` | 1+ |
| scheduler | `python manage.py run_scheduler` | **exactly 1** |

Build step: `pip install -r requirements.txt && bash scripts/build.sh`.

## Order of a first deploy of this version

1. **Redis.** Provision it and set `CACHE_URL`. Rate limits, the job queue and
   per-account lockouts all need it; production refuses to start without it
   (`accounts.E002`) unless `ALLOW_LOCAL_CACHE=True`.
2. **PostgreSQL with pgvector.** The migration runs `CREATE EXTENSION IF NOT
   EXISTS vector`, which needs a role allowed to create extensions (managed
   Postgres: enable the `vector` extension in the console first).
3. **Environment.** New variables, all in `.env.example`:
   `CACHE_URL`, `TRUSTED_PROXY_COUNT`, `FIELD_ENCRYPTION_KEYS`,
   `SESSION_IDLE_MINUTES`, `MFA_ENFORCED`, `LABS_ENABLED`, `PHI_LLM_PROVIDERS`,
   `CONSENT_ENFORCED_EGRESS` (remove any old `rag` override), `SENTRY_DSN`
   (optional), `RELEASE_PHASE_HANDLED=true` where the platform runs the release
   command, `RAG_PGVECTOR` (leave unset until step 7).
4. **Deploy** web, worker and scheduler from the same commit. The release step
   takes a backup and migrates.
5. **Field encryption:** `python manage.py rotate_field_encryption` (dry run),
   then `--apply --include-plaintext`. See `docs/FIELD_ENCRYPTION.md`.
6. **Vectors:** `python manage.py backfill_embedding_vectors`, then
   `python manage.py backfill_embedding_vectors --verify 50`. Expect a mean
   top-10 overlap of at least 0.9.
7. Until step 6 has verified, retrieval already falls back to the NumPy path for
   any patient whose chunks are not all backfilled; after it, nothing more to do.
8. **Staff and clinicians** are asked to enrol TOTP at their next page view.
9. **Check** `/health/ready/`: `"status": "ready"` once every scheduled job has
   run successfully; `"degraded"` lists jobs that have not.

## Rollback

* **Code:** redeploy the previous commit. Migrations in this version only add
  columns, tables and indexes, so the previous code runs against the new schema.
* **Vector search only:** set `RAG_PGVECTOR=False` and restart. Retrieval goes
  back to loading the BinaryField and scoring in NumPy; the `embedding_vec`
  column is ignored. Nothing is deleted. To remove the column and its HNSW
  indexes entirely: `python manage.py migrate rag_assistant 0012`.
* **Consent default:** set `CONSENT_ENFORCED_EGRESS=rag` (see
  `docs/EGRESS_ROLLOUT.md`).
* **Database:** the release step's `backup_database` dump from just before the
  migration; restore with `docs/BACKUP_RESTORE.md`.

## Local stack

`docker compose up --build` runs PostgreSQL (with pgvector), Redis, web, worker
and scheduler. Uploads go through the worker, retrieval through pgvector.
