#!/bin/bash
set -e

echo "========================================"
echo "  HealthCompass startup — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"
echo "Python: $(python --version 2>&1)"
echo "Working directory: $(pwd)"
echo "BASE_DIR contents:"
ls -la

# ── collectstatic (normally done in the build phase) ──────────────────────────
# scripts/build.sh runs collectstatic once per deploy. Only when the image was
# built without it (no manifest) is it run here as a fallback.
echo ""
if [ -f "./staticfiles/staticfiles.json" ]; then
    echo ">>> [1/6] Static files already collected at build time."
else
    echo ">>> [1/6] No static manifest found; running scripts/build.sh as a fallback..."
    bash scripts/build.sh
fi

# ── migrations (normally done in the release phase) ───────────────────────────
# scripts/release.sh backs up the database and migrates once per deploy. Set
# RELEASE_PHASE_HANDLED=true where the platform runs it (Procfile `release:`,
# Render pre-deploy command); otherwise it runs here, before gunicorn.
echo ""
if [ "${RELEASE_PHASE_HANDLED:-false}" = "true" ]; then
    echo ">>> [2/6] Release phase handled by the platform; not migrating here."
else
    echo ">>> [2/6] Running release steps (backup, migrate)..."
    bash scripts/release.sh
fi

# ── superuser (one-time bootstrap) ───────────────────────────────────────────
if [ "$CREATE_ADMIN" = "true" ]; then
    echo ""
    echo ">>> [3/6] Creating superuser (CREATE_ADMIN=true)..."
    python manage.py shell -c "
import os
from django.contrib.auth import get_user_model
User = get_user_model()
email    = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@healthcompass.dev')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', '')
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', email.split('@')[0])
if not password:
    print('DJANGO_SUPERUSER_PASSWORD not set — skipping superuser creation.')
elif User.objects.filter(is_superuser=True).exists():
    print('Superuser already exists — skipping.')
else:
    u = User.objects.create_superuser(username=username, email=email, password=password)
    u.is_approved = True
    u.save()
    print(f'Superuser created: {email}')
" || echo "Superuser creation failed — check logs above."
fi

# ── remove seeded demo AI models (one-time, opt-in) ──────────────────────────
#
# Demo models are NOT seeded here any more. `seed_demo_models` used to run on
# every container start, writing twelve placeholder models into the production
# database on each restart; the command and that hook are gone.
#
# This block clears what those earlier deploys already wrote, for deployments
# with no shell access where a one-off command cannot be run by hand. It is
# gated on an environment variable for the same reason the superuser block
# above is: a deploy must not change what a user sees unless someone
# deliberately asked it to. Unset REMOVE_DEMO_MODELS once it has run — leaving
# it set is harmless (there is nothing left to delete, and the command says so)
# but the variable is a record of intent, not a setting.
#
# The dry run goes first so the deploy log keeps a permanent record of exactly
# what was there before anything was deleted. That is the review step you would
# otherwise get from reading the output before typing --confirm.
if [ "$REMOVE_DEMO_MODELS" = "true" ]; then
    echo ""
    echo ">>> [4/6] Removing seeded demo AI models (REMOVE_DEMO_MODELS=true)..."
    python manage.py remove_demo_models || echo "WARNING: demo model report failed — check logs above"
    python manage.py remove_demo_models --confirm || echo "WARNING: demo model removal failed — check logs above"
fi

# ── Google OAuth SocialApp ────────────────────────────────────────────────────
echo ""
echo ">>> [5/6] Ensuring Google SocialApp credentials in DB..."
python manage.py ensure_social_app || echo "WARNING: ensure_social_app failed — check logs above"

# ── gunicorn ──────────────────────────────────────────────────────────────────
echo ""
echo ">>> [6/6] Starting gunicorn on 0.0.0.0:${PORT}..."
exec gunicorn healthcompass.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --worker-class gthread \
    --threads 8 \
    --timeout 120 \
    --log-file -
