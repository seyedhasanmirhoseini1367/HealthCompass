#!/bin/bash
set -e

echo ">>> Running collectstatic..."
python manage.py collectstatic --noinput

echo ">>> Running migrations..."
python manage.py migrate --noinput

# Optional: create the 'admin' superuser on first deploy.
# Set the Railway environment variable CREATE_ADMIN=true to trigger this once.
# The command is idempotent — it will not overwrite an existing admin unless
# you also set RESET_ADMIN=true.
if [ "${CREATE_ADMIN}" = "true" ]; then
    echo ">>> CREATE_ADMIN=true — running create_admin..."
    if [ "${RESET_ADMIN}" = "true" ]; then
        python manage.py create_admin --reset
    else
        python manage.py create_admin
    fi
fi

echo ">>> Starting gunicorn..."
exec gunicorn healthcompass.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --workers 2 \
    --timeout 120 \
    --log-file -
