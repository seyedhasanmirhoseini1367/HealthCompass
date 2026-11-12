#!/bin/bash
# Release phase: runs once per deploy, before the new web process starts
# (audit ARCH-07). Back up first, then migrate, so a bad migration can be
# rolled back from the dump taken seconds before it ran.
#   Procfile `release:` / Render "Pre-Deploy Command" = bash scripts/release.sh
set -euo pipefail
if command -v pg_dump >/dev/null 2>&1 && [ -n "${DATABASE_URL:-}" ]; then
    echo "release: backing up the database before migrating"
    python manage.py backup_database
else
    echo "release: pg_dump or DATABASE_URL unavailable, skipping pre-migration backup"
fi
python manage.py migrate --noinput
