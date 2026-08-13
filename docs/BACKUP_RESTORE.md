# Backup & Restore

**Status: repository mechanism implemented and verified on SQLite.
Production (Railway PostgreSQL) verification is PENDING — see §5.**

Before this existed the project had no backup mechanism of any kind. The only
occurrences of the word "backup" anywhere in the repository were in the audit
documents describing its absence.

---

## 1. Principle

**A backup that has never been restored is not a backup.**

Every backup writes two files:

```
healthcompass-<engine>-<timestamp>.(sqlite3|dump)
healthcompass-<engine>-<timestamp>.manifest.json
```

The manifest is what makes the backup *verifiable*. It records:

- row counts for **every** concrete model (not a curated list, so a new model
  cannot silently fall outside verification)
- the applied migration set, so schema drift is detectable
- a SHA-256 of the dump, so corruption in transit is detectable

`verify_backup` restores into a **scratch** database and checks the restore
against that manifest. It exits non-zero when the restore is unfaithful, so it
can gate a release.

---

## 2. Taking a backup

```bash
python manage.py backup_database --output /var/backups/healthcompass
```

- **SQLite** — consistent snapshot via sqlite3's backup API. A plain file copy of
  a live database can capture a torn write; the backup API cannot.
- **PostgreSQL** — `pg_dump --format=custom --no-owner --no-privileges`.
  If `pg_dump` is not on PATH the command **fails loudly** rather than silently
  substituting a weaker mechanism.

---

## 3. Verifying a restore

```bash
python manage.py verify_backup \
    --backup   backups/healthcompass-sqlite-20260813T121452Z.sqlite3 \
    --manifest backups/healthcompass-sqlite-20260813T121452Z.manifest.json
```

Checks performed:

| Check | Failure means |
|---|---|
| SHA-256 vs manifest | the dump is not the artefact the manifest describes |
| Row counts per model | rows lost or added during restore |
| Applied migrations | schema drift between capture and restore |
| `chunk_patient_matches_document` | a chunk restored onto the **wrong patient** |
| `document_patient_matches_record` | a document restored onto the wrong patient |
| `lab_values_have_a_record` | orphaned lab values — ownership is derived through the record |
| `wearable_points_have_a_record` | orphaned wearable points |

The ownership invariants matter more than the counts. A restore with correct
totals that attaches a chunk to the wrong patient is **worse** than a failed
restore, because it looks like success.

### Safety rails

- Restoring into the `default` alias is **refused outright**. A verification run
  must never become a destructive production operation through a mistyped flag.
- The source database is never written to.
- The scratch database is a temporary copy, deleted afterwards unless `--keep`.

---

## 4. Verified run (development, SQLite)

Executed 2026-08-13 against the development database:

```
Backup written: healthcompass-sqlite-20260813T121452Z.sqlite3
  engine   : sqlite
  rows     : 4987 across 34 models
  sha256   : 4ee7261bc9ddef1c…

verify_backup:
  checksum : OK
  models   : 34
  OK   chunk_patient_matches_document   (0 violations)
  OK   document_patient_matches_record  (0 violations)
  OK   lab_values_have_a_record         (0 violations)
  OK   wearable_points_have_a_record    (0 violations)
Restore verified: row counts, migrations and ownership invariants all match.
```

This is a **real** backup and a **real** restore into a scratch database — not a
dry run.

---

## 5. PENDING — production (Railway PostgreSQL)

**Production backups are NOT yet verified. Do not treat them as such.**

Automated restore verification is implemented for SQLite dumps. A PostgreSQL
manifest deliberately produces an explicit error from `verify_backup` rather
than passing quietly, so a Postgres backup cannot be recorded as verified by
accident.

What remains, and requires infrastructure access this repository does not have:

1. **Confirm Railway's managed PostgreSQL backup policy** — retention, frequency,
   and whether point-in-time recovery is enabled.
2. **Schedule `backup_database`** in an environment where `pg_dump` is installed
   (the application container does not necessarily have the client tools).
3. **Provision a scratch PostgreSQL database** for restore rehearsal.
4. **Rehearse a restore** with `pg_restore`, then run the same integrity
   invariants against the scratch database:

   ```bash
   createdb healthcompass_restore_test
   pg_restore --no-owner --no-privileges \
              --dbname healthcompass_restore_test backups/<dump>
   DATABASE_URL=postgres://…/healthcompass_restore_test \
       python manage.py shell -c \
       "from healthcompass.backup import integrity_checks; \
        [print(c) for c in integrity_checks()]"
   ```

5. **Record the rehearsal date here.** Until a real production restore has
   succeeded, the durability risk identified in the audit (P0-2) remains open.

### Migration interaction

`startup.sh` currently runs `migrate` on **every** container start, with
`restartPolicyMaxRetries = 3`. A backup must be taken **before** a release that
carries migrations, and migrations should move behind an explicit release step
rather than running on every boot. Tracked separately in the roadmap.

---

## 6. What is deliberately not automated

- **No scheduled job is registered.** Scheduling belongs to the deployment
  platform, and adding a scheduler here would be a new external dependency for a
  problem the platform already solves.
- **No automatic restore into production, ever.** Restore is a human-initiated,
  reviewed operation.
