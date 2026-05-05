#!/bin/bash
set -e

echo "========================================"
echo "  HealthCompass startup — $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
echo "========================================"
echo "Python: $(python --version 2>&1)"
echo "Working directory: $(pwd)"
echo "BASE_DIR contents:"
ls -la

# ── collectstatic ─────────────────────────────────────────────────────────────
echo ""
echo ">>> [1/3] Running collectstatic (verbose)..."
python manage.py collectstatic --noinput --verbosity 2

echo ""
echo ">>> Verifying staticfiles directory was created..."
if [ ! -d "./staticfiles" ]; then
    echo "ERROR: collectstatic exited 0 but ./staticfiles/ was NOT created."
    echo ""
    echo "--- Disk usage ---"
    df -h .
    echo ""
    echo "--- Directory permissions ---"
    ls -la .
    echo ""
    echo "--- Django check output ---"
    python manage.py check --deploy 2>&1 || true
    exit 1
fi

STATIC_COUNT=$(find ./staticfiles -type f | wc -l)
echo "OK: ./staticfiles/ exists with ${STATIC_COUNT} file(s)."

# ── migrations ────────────────────────────────────────────────────────────────
echo ""
echo ">>> [2/3] Running migrations..."
python manage.py migrate --noinput

# ── gunicorn ──────────────────────────────────────────────────────────────────
echo ""
echo ">>> [3/3] Starting gunicorn on 0.0.0.0:${PORT}..."
exec gunicorn healthcompass.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --timeout 120 \
    --log-file -
