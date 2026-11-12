release: bash scripts/release.sh
web: bash startup.sh
# Background jobs (audit ARCH-02/03): one worker, and exactly one scheduler.
worker: python manage.py rqworker ingest default
scheduler: python manage.py run_scheduler
