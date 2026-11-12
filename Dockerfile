# Production-like image for local runs (docker compose) and container hosts.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Build phase: hashed, compressed static files (audit ARCH-01/07). The key is
# only for this step; the real one comes from the environment at run time.
RUN SECRET_KEY=build-only-not-secret ALLOW_EPHEMERAL_MEDIA=True ALLOW_LOCAL_CACHE=True \
    python manage.py collectstatic --noinput --verbosity 0

RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
CMD ["gunicorn", "healthcompass.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--worker-class", "gthread", "--threads", "8", "--timeout", "120"]
