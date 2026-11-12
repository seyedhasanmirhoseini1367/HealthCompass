#!/bin/bash
# Build phase: everything that depends only on the code, run once per deploy
# rather than on every container start (audit ARCH-07).
#   Render:  Build Command = pip install -r requirements.txt && bash scripts/build.sh
set -euo pipefail
python manage.py collectstatic --noinput
test -f staticfiles/staticfiles.json || { echo "collectstatic produced no manifest"; exit 1; }
echo "build: $(find staticfiles -type f | wc -l) static files collected"
