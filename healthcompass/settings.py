from pathlib import Path
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Core security ──────────────────────────────────────────────────────────────
# No default — Railway (and any production environment) MUST set this explicitly.
SECRET_KEY    = config('SECRET_KEY')
DEBUG         = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='127.0.0.1,localhost',
    cast=Csv()
) + [
    'healthcompass-ibco.onrender.com',
    'healthcompass.hasanai.net',
]

# Trust Railway's HTTPS proxy and the subdomain
CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='https://healthcompass.hasanai.net',
    cast=Csv(),
)

AUTHENTICATION_BACKENDS = [
    'apps.accounts.backends.EmailOrUsernameBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# Email address to notify when a new professional role registers
ADMIN_NOTIFICATION_EMAIL = config('ADMIN_NOTIFICATION_EMAIL', default='')
# Shown on About and Privacy when set. Empty hides the contact lines.
CONTACT_EMAIL = config('CONTACT_EMAIL', default='')
# Owner details for the privacy page (healthcompass/legal.py). Empty values show
# a TODO banner in development and fail `check --deploy` (pages.E001).
LEGAL_CONTROLLER_NAME    = config('LEGAL_CONTROLLER_NAME', default='')
LEGAL_CONTROLLER_ADDRESS = config('LEGAL_CONTROLLER_ADDRESS', default='')
LEGAL_BASIS              = config('LEGAL_BASIS', default='')
LEGAL_HOSTING_PROVIDER   = config('LEGAL_HOSTING_PROVIDER', default='')
LEGAL_DATABASE_PROVIDER  = config('LEGAL_DATABASE_PROVIDER', default='')
LEGAL_EMAIL_PROVIDER     = config('LEGAL_EMAIL_PROVIDER', default='')
LEGAL_STORAGE_PROVIDER   = config('LEGAL_STORAGE_PROVIDER', default='')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.sitemaps',
    # allauth
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    # TOTP + recovery codes; required for staff and professional roles by
    # apps.accounts.mfa.RequireMFAMiddleware (audit SEC-17).
    'allauth.mfa',
    # Project apps
    'apps.accounts',
    'apps.medical_records',
    'apps.ai_insights',
    'apps.rag_assistant',
    'apps.dashboard',
    'apps.notifications',
    'apps.appointments',
    'apps.care',
    'apps.ops',     # scheduled jobs and their heartbeats
    'apps.scribe',  # experimental AI scribe demo — see apps/scribe/README.md
    # Mobile API
    'apps.api',
    'rest_framework',
    'rest_framework_simplejwt',
    'drf_spectacular',
    # Revocable refresh tokens: logout, logout-all and password change blacklist
    # them (audit SEC-04). Run `manage.py flushexpiredtokens` daily.
    'rest_framework_simplejwt.token_blacklist',
    'django_rq',
    'corsheaders',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise must be directly after SecurityMiddleware
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    # Language from the session or Accept-Language (en, fi, sv; audit UX-07).
    'django.middleware.locale.LocaleMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # CSP (enforcing), Permissions-Policy, no-store/noindex on signed-in pages.
    'healthcompass.middleware.SecurityHeadersMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    # After AuthenticationMiddleware: needs request.user.
    'apps.accounts.idle_session.IdleSessionMiddleware',
    # After AuthenticationMiddleware: staff/professional accounts without an
    # authenticator are sent to enrolment.
    'apps.accounts.mfa.RequireMFAMiddleware',
]

# Signed out after this long without a request (see apps/accounts/idle_session.py).
# 30 minutes idle for a site holding health records (audit SEC-17).
SESSION_COOKIE_AGE = config('SESSION_IDLE_MINUTES', default=30, cast=int) * 60
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

ROOT_URLCONF = 'healthcompass.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            # {% icon %} everywhere without {% load %} (apps/dashboard/templatetags/icons.py).
            'builtins': ['apps.dashboard.templatetags.icons'],
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.notifications.context_processors.unread_notifications',
                'apps.dashboard.navigation.nav_menu',
                'apps.ai_insights.labs.labs_context',
                'apps.accounts.verification.verification_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'healthcompass.wsgi.application'

# ── Database ───────────────────────────────────────────────────────────────────
# Railway injects DATABASE_URL automatically when a PostgreSQL service is added.
# Falls back to SQLite for local development when DATABASE_URL is not set.
DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,
        # A pooled connection that died (database restart, idle timeout) is
        # replaced instead of failing the next request.
        conn_health_checks=True,
    )
}

AUTH_USER_MODEL = 'accounts.CustomUser'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en'
# Finnish and Swedish catalogues exist (locale/) but are not translated yet.
LANGUAGES = [('en', 'English'), ('fi', 'Suomi'), ('sv', 'Svenska')]
LOCALE_PATHS = [BASE_DIR / 'locale']
TIME_ZONE = 'Europe/Helsinki'
USE_I18N = True
USE_TZ = True
# One date format everywhere, in every language (healthcompass/formats/<lang>/).
DATE_FORMAT = 'j M Y'
DATETIME_FORMAT = 'j M Y, H:i'
FORMAT_MODULE_PATH = ['healthcompass.formats']

# ── Static files ───────────────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# STORAGES is defined below, unconditionally. STATICFILES_STORAGE was removed in
# Django 5.1 and was silently ignored here, so static files were served
# unhashed and uncompressed (audit ARCH-01).

MEDIA_URL = '/media/'
MEDIA_ROOT = Path(config('MEDIA_ROOT', default=str(BASE_DIR / 'media')))

# ── Object storage (S3-compatible — Cloudflare R2, MinIO, AWS S3) ──────────────
# Set OBJECT_STORAGE_URL to opt in.  Without it, uploads fall back to MEDIA_ROOT
# (local disk — fine for dev, but ephemeral on Railway between deploys).
#
# Required env vars when OBJECT_STORAGE_URL is set:
#   OBJECT_STORAGE_URL    — e.g. https://<account>.r2.cloudflarestorage.com
#   STORAGE_BUCKET_NAME   — e.g. healthcompass-media
#   STORAGE_ACCESS_KEY    — S3-compatible access key
#   STORAGE_SECRET_KEY    — S3-compatible secret key
# Deliberately acknowledging that uploads are lost on every deploy. Without
# this, a production start with no object storage is a hard system-check error
# — see apps/medical_records/checks.py for why that is not a warning.
#
# Always true under the test runner. Django sets DEBUG=False for tests, so the
# check would otherwise read the suite as a production deployment and abort
# every run — and it would be wrong on the merits too: test media goes to a
# throwaway temp directory on purpose, so "this storage is ephemeral" is a
# statement of intent there rather than a defect.
import sys as _sys_storage
ALLOW_EPHEMERAL_MEDIA = ('test' in _sys_storage.argv
                         or config('ALLOW_EPHEMERAL_MEDIA', default=False, cast=bool))

# Hashed, compressed static files (WhiteNoise). The test runner uses the plain
# compressed storage: the manifest one needs collectstatic to have run.
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': (
        'whitenoise.storage.CompressedStaticFilesStorage' if 'test' in _sys_storage.argv
        else 'whitenoise.storage.CompressedManifestStaticFilesStorage')},
}

_OBJECT_STORAGE_URL = config('OBJECT_STORAGE_URL', default='')
if _OBJECT_STORAGE_URL:
    INSTALLED_APPS += ['storages']
    STORAGES['default'] = {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage'}
    # Private bucket, short-lived signed URLs only.
    AWS_QUERYSTRING_AUTH     = True
    AWS_QUERYSTRING_EXPIRE   = 60
    AWS_S3_ENDPOINT_URL      = _OBJECT_STORAGE_URL
    AWS_STORAGE_BUCKET_NAME  = config('STORAGE_BUCKET_NAME', default='healthcompass-media')
    AWS_ACCESS_KEY_ID        = config('STORAGE_ACCESS_KEY',  default='')
    AWS_SECRET_ACCESS_KEY    = config('STORAGE_SECRET_KEY',  default='')
    AWS_S3_FILE_OVERWRITE    = False
    AWS_DEFAULT_ACL          = None
    MEDIA_URL                = f'{_OBJECT_STORAGE_URL}/{AWS_STORAGE_BUCKET_NAME}/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Checks muted on purpose, by id, per environment (e.g. CI acknowledging
# ephemeral media). Empty by default.
# Enforcing Content-Security-Policy (healthcompass/middleware.py). Set False to
# switch to report-only while investigating a violation.
CSP_ENFORCE = config('CSP_ENFORCE', default=True, cast=bool)

SILENCED_SYSTEM_CHECKS = config('SILENCED_SYSTEM_CHECKS', default='', cast=Csv())

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# ── Email ──────────────────────────────────────────────────────────────────────
EMAIL_HOST         = config('EMAIL_HOST',         default='smtp.gmail.com')
EMAIL_PORT         = config('EMAIL_PORT',         default=587, cast=int)
EMAIL_USE_TLS      = True
EMAIL_HOST_USER    = config('EMAIL_HOST_USER',    default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
# No made-up default domain: mail from a domain you do not control fails SPF/DKIM.
# accounts.W003 warns when this is unset or not on SITE_DOMAIN's domain.
DEFAULT_FROM_EMAIL  = config('DEFAULT_FROM_EMAIL',  default='webmaster@localhost')

# Use SMTP only when credentials are actually configured; fall back to console
if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_TIMEOUT = 10  # fail fast so gunicorn worker doesn't get killed waiting
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# ── AI APIs ────────────────────────────────────────────────────────────────────
# ── Which LLM vendors may receive patient data ────────────────────────────────
#
# Consent is granted per purpose — "sending my health data to external AI
# providers" — not per company, while the generation fallback chain spans four
# organisations. Whichever key was configured and answered first got the
# records, which is a vendor decision made at runtime rather than by policy.
#
# This is the server-side allowlist, enforced inside the fallback loop (see
# apps/accounts/egress.py). The default is every provider the code already
# used, so it changes nothing on its own; narrowing it is now a configuration
# change instead of a code change.
PHI_LLM_PROVIDERS = config(
    # Two vendors by default, one primary and one fallback, each of which needs
    # a data-processing agreement (audit SEC-09). Widen only with a DPA.
    'PHI_LLM_PROVIDERS', default='gemini,anthropic', cast=Csv())

GROQ_API_KEY      = config('GROQ_API_KEY',      default='')
GEMINI_API_KEY    = config('GEMINI_API_KEY',    default='')
ANTHROPIC_API_KEY = config('ANTHROPIC_API_KEY', default='')
OPENAI_API_KEY    = config('OPENAI_API_KEY',    default='')

# ── Firebase Cloud Messaging (push notifications) ─────────────────────────────
# Paste the full service-account JSON (Firebase Console → Project Settings →
# Service Accounts → Generate new private key) as a single-line string.
# Without this, apps.notifications.firebase.send_push() silently no-ops.
FIREBASE_CREDENTIALS_JSON = config('FIREBASE_CREDENTIALS_JSON', default='')

# ── RAG Configuration ──────────────────────────────────────────────────────────
RAG_CONFIG = {
    'EMBEDDING_MODEL':   'models/gemini-embedding-001',
    # Output dimension of EMBEDDING_MODEL. Stored per chunk alongside the vector
    # so a model swap is detectable instead of silently degrading retrieval.
    # Changing EMBEDDING_MODEL without changing this (or vice versa) will cause
    # existing chunks to be reported stale rather than mixed into the index.
    'EMBEDDING_DIM':     3072,
    'CHUNK_SIZE':        200,
    'CHUNK_OVERLAP':     40,
    'TOP_K':             6,
    'BM25_WEIGHT':       0.35,
    'SEMANTIC_WEIGHT':   0.65,
    'TIME_DECAY_DAYS':   365,
    'TIME_DECAY_FACTOR': 0.15,
    'MMR_LAMBDA':        0.6,
    'SIM_THRESHOLD':     0.15,
    'INTENT_WEIGHTS': {
        'temporal':   (0.20, 0.80),
        'medication': (0.50, 0.50),
        'diagnostic': (0.30, 0.70),
        'general':    (0.35, 0.65),
    },
    'CONTEXT_TYPE_BOOST':     0.08,
    'RERANK_RECALL_FACTOR':   5,    # Stage-1 retrieves top_k×5 before LLM reranker
    'TRAJECTORY_ENABLED': True,
    'COLD_START_ENABLED': True,
    'GROQ_MODEL':        'llama-3.1-8b-instant',
    'GEMINI_MODEL':      'gemini-2.5-flash',
    'ANTHROPIC_MODEL':   'claude-haiku-4-5-20251001',
    'OPENAI_MODEL':      'gpt-4o-mini',
    'MAX_TOKENS':        1200,
    # Wall-clock ceiling for a single provider call. Must stay well below the
    # gunicorn worker timeout (120s) so a slow provider surfaces as a handled
    # fallback rather than a killed worker. Without this the SDK defaults apply —
    # OpenAI and Anthropic default to 600s, which is long enough for a handful of
    # hung requests to occupy every thread in the pool.
    'PROVIDER_TIMEOUT':  config('LLM_TIMEOUT_SECONDS', default=45, cast=int),
    # Indexing runs off-request; a tighter bound keeps a stuck batch from
    # pinning a background thread indefinitely.
    'EMBED_TIMEOUT':     config('EMBED_TIMEOUT_SECONDS', default=60, cast=int),
    'VECTOR_STORE_PATH': str(BASE_DIR / 'rag_vector_store'),
}

# File upload limits.
# NOTE: neither of these caps how large an uploaded FILE may be.
#   DATA_UPLOAD_MAX_MEMORY_SIZE is measured against the request body excluding
#   file upload data; FILE_UPLOAD_MAX_MEMORY_SIZE is only the point at which an
#   upload spools from memory to a temp file.
# MAX_UPLOAD_BYTES is the actual per-file ceiling, enforced in
# apps/medical_records/services.py::validate_upload.
# 5 MB for non-file request bodies (JSON, form fields); was 50 MB (SEC-16).
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800
# Longest pasted or OCR'd text accepted as a record.
MAX_PASTED_TEXT_CHARS = 200_000
MAX_UPLOAD_BYTES = config('MAX_UPLOAD_BYTES', default=25 * 1024 * 1024, cast=int)

# Research demos (seizure analysis, real-time EEG, ICU demo, model submission).
# Off by default: every Labs route returns 404 unless this is True (audit SEC-03).
LABS_ENABLED = config('LABS_ENABLED', default=False, cast=bool)

# Model files are legitimately larger than clinical documents, so they get their
# own ceiling rather than being forced under the record limit. Enforced in
# apps/ai_insights/forms.py; there was previously no limit on model uploads at
# all, and no format or content check either.
MAX_MODEL_UPLOAD_BYTES = config('MAX_MODEL_UPLOAD_BYTES',
                                default=200 * 1024 * 1024, cast=int)
# Row ceiling for tabular imports (parquet/CSV). Compressed formats can
# expand far beyond MAX_UPLOAD_BYTES, so the expansion is bounded too.
MAX_PARSED_ROWS = config('MAX_PARSED_ROWS', default=200_000, cast=int)

# ── Proxy / HTTPS (Railway terminates SSL at edge) ────────────────────────────
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST    = True

# ── Google OAuth (allauth) ─────────────────────────────────────────────────────
SITE_ID = 1
# The default follows DEBUG. Hardcoding the production host meant a local
# database ended up with Site.domain = healthcompass.hasanai.net, which is the
# host Django uses to build absolute URLs when no request is in hand — password
# reset links and OAuth callbacks generated on a dev machine pointed at
# production. An explicit SITE_DOMAIN still wins in both environments.
SITE_DOMAIN = config('SITE_DOMAIN',
                     default='127.0.0.1:8000' if DEBUG else 'healthcompass.hasanai.net')
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'http' if DEBUG else 'https'
LOGIN_REDIRECT_URL  = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

SOCIALACCOUNT_ADAPTER       = 'apps.accounts.adapters.HealthCompassSocialAdapter'
ACCOUNT_ADAPTER             = 'apps.accounts.adapters.HealthCompassAccountAdapter'
SOCIALACCOUNT_AUTO_SIGNUP   = True    # auto-create user; role selection handled post-signup
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True  # link Google to existing email account
# False: a GET must not start an OAuth flow, or any page could sign a visitor
# in through an <img> tag (audit SEC-17). allauth shows a one-click confirm page.
SOCIALACCOUNT_LOGIN_ON_GET  = False
# Mandatory once mail can actually be delivered; 'none' otherwise, which
# accounts.W002 reports. Unverified users can sign in but cannot share or use
# the assistant until they confirm (audit SEC-05).
# The test runner always starts from 'none' so results do not depend on the
# machine's .env; verification tests switch it on with override_settings.
import sys as _sys
ACCOUNT_EMAIL_VERIFICATION  = config(
    'ACCOUNT_EMAIL_VERIFICATION',
    default='mandatory' if (EMAIL_HOST_USER and EMAIL_HOST_PASSWORD
                            and 'test' not in _sys.argv) else 'none')
ACCOUNT_CONFIRM_EMAIL_ON_GET = False
ACCOUNT_EMAIL_CONFIRMATION_EXPIRE_DAYS = 3
MFA_SUPPORTED_TYPES = ['totp', 'recovery_codes']
MFA_TOTP_ISSUER = 'HealthCompass'
ACCOUNT_SIGNUP_FIELDS       = ['email*', 'password1*', 'password2*']
ACCOUNT_LOGIN_METHODS       = {'email'}
SOCIALACCOUNT_EMAIL_REQUIRED = False  # don't block signup if Google omits email

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        # No APP key here — credentials live only in the DB (created by
        # ensure_social_app on startup). Having both settings APP and a DB
        # record causes allauth list_apps() to return 2 → MultipleObjectsReturned.
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
    }
}

# ── ICU Demo (MIMIC-IV local data) ────────────────────────────────────────────
ICU_DEMO_DATA_PATH = config('ICU_DEMO_DATA_PATH', default='')

# ── Cache ──────────────────────────────────────────────────────────────────────
# Default: process-local memory cache (dev / single-worker).
# Set CACHE_URL (e.g. redis://localhost:6379/0) to switch to Redis in production.
# Redis is required for django-ratelimit to work correctly across gunicorn workers.
_CACHE_URL = config('CACHE_URL', default='')
# Rate limits and throttles count in the cache. LocMem counts per worker
# process, so production needs Redis; accounts.E002 refuses to start without it
# unless this is set deliberately. Always true under the test runner.
ALLOW_LOCAL_CACHE = ('test' in _sys_storage.argv
                     or config('ALLOW_LOCAL_CACHE', default=False, cast=bool))
# Reverse proxies in front of the app (Render/Railway edge = 1). client_ip()
# trusts this many hops of X-Forwarded-For, read from the right (SEC-06).
TRUSTED_PROXY_COUNT = config('TRUSTED_PROXY_COUNT', default=1, cast=int)
if _CACHE_URL:
    CACHES = {
        'default': {
            'BACKEND':  'django.core.cache.backends.redis.RedisCache',
            'LOCATION': _CACHE_URL,
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }

# ── PHI retention policy ────────────────────────────────────────────────────────
# QueryLog stores full patient question + response text (PHI).
# Run `python manage.py purge_old_query_logs` periodically (e.g. weekly cron)
# to delete records older than this many days.
# Set to 0 to disable automatic purging (retain forever — not recommended for
# any deployment targeting regulated health data contexts such as Kanta/THL).
# Field encryption keys (apps/accounts/fields.py, audit SEC-15): comma-separated
# Fernet keys, newest first. The SECRET_KEY-derived key is always tried last, so
# existing data stays readable. See docs/FIELD_ENCRYPTION.md for rotation.
FIELD_ENCRYPTION_KEYS = config('FIELD_ENCRYPTION_KEYS', default='', cast=Csv())

QUERYLOG_RETENTION_DAYS = config('QUERYLOG_RETENTION_DAYS', default=90, cast=int)

# Population statistics: every figure must rest on at least this many distinct
# patients, for every viewer (audit SEC-11). POPULATION_MIN_PATIENTS, if set, is
# the stricter threshold applied to viewers without research/admin access.
POPULATION_MIN_COHORT = config('POPULATION_MIN_COHORT', default=5, cast=int)

# ── Consent ────────────────────────────────────────────────────────────────────
# Version of the consent text for each purpose. Bump a value when the wording
# materially changes: consent recorded against an older version stops counting,
# and the user is asked again. Never reuse a version number for different text.
CONSENT_VERSIONS = {
    'ai_processing':       'v1',
    'external_llm':        'v1',
    'document_processing': 'v1',
    'data_sharing':        'v1',
    'research':            'v1',
}

# Purposes that BLOCK the corresponding feature when not granted.
#
# Only external_llm is enforced today, because it is the only purpose whose
# absence has a clean, implementable meaning in the current product: refuse to
# transmit health data to third-party AI providers. The other purposes are
# recorded and shown to the user but do not gate anything yet — enforcing
# document_processing, for example, would require an ingestion path that works
# without the external parser, which does not exist.
#
# Deployment note: enforcement is default-deny. Existing users have no consent
# rows and will be blocked from the AI assistant until they grant it. That is
# the legally correct behaviour — consent must be affirmative and cannot be
# back-filled — but it is a visible change, so stage the rollout by shipping the
# consent UI first and setting this afterwards.
CONSENT_REQUIRED_PURPOSES = config(
    'CONSENT_REQUIRED_PURPOSES', default='external_llm', cast=Csv(),
)

# Which registered egress points block when consent is absent.
# See apps/accounts/egress.py for the full matrix.
#
#   'rag'  — the points already covered by the RAGService gate, i.e. everything
#            reached by ASKING a question. It deliberately excludes
#            rag.embed_documents, which is reached by UPLOADING a record and was
#            never behind that gate — see RAG_PIPELINE_POINTS in egress.py.
#            This is the current default, so widening the boundary is a
#            deliberate act rather than a side effect of deploying.
#   'all'  — every PHI-bearing point: document parsing, OCR, prediction
#            interpretation and the seizure proxy included.
#   Or an explicit comma-separated list of point ids for a staged rollout, e.g.
#            CONSENT_ENFORCED_EGRESS=rag,records.ocr,insights.seizure_proxy
#
# Impact of moving to 'all': users who have not granted external_llm consent
# stop having uploads OCR'd and LLM-parsed (regex extraction still runs), get
# the static prediction interpretation instead of the generated one, cannot use
# the seizure analysis, and have new records left unindexed until they consent.
# Nothing is deleted and nothing already processed is affected.
# Default 'all' (audit SEC-09): no health data leaves the system for parsing,
# OCR or embedding before external_llm consent. Set 'rag' only to roll back.
CONSENT_ENFORCED_EGRESS = config('CONSENT_ENFORCED_EGRESS', default='all', cast=Csv())

# ── Embedding provenance enforcement ───────────────────────────────────────────
# When False (default), chunks embedded before provenance tracking existed
# (embedding_model == '') are still usable provided their vector dimension
# matches RAG_CONFIG['EMBEDDING_DIM'].  This keeps pre-existing production
# embeddings working without a re-embed.
#
# Set to True once `python manage.py backfill_embedding_provenance` has stamped
# every legacy row.  In strict mode, unknown-provenance chunks are excluded from
# retrieval, so a model swap can never silently mix vector spaces.
EMBEDDING_STRICT_PROVENANCE = config('EMBEDDING_STRICT_PROVENANCE', default=False, cast=bool)

# ── Production security headers ────────────────────────────────────────────────
# Railway terminates SSL at the edge, so no SECURE_SSL_REDIRECT needed.
if not DEBUG:
    SESSION_COOKIE_SECURE        = True
    CSRF_COOKIE_SECURE           = True
    # The proxy terminates TLS and sets X-Forwarded-Proto (SECURE_PROXY_SSL_HEADER),
    # so this only redirects requests that really arrived over plain HTTP.
    # Health probes from the platform come in over HTTP and must not be redirected.
    SECURE_SSL_REDIRECT          = config('SECURE_SSL_REDIRECT',
                                      default='test' not in _sys_storage.argv, cast=bool)
    SECURE_REDIRECT_EXEMPT       = [r'^health/']
    SECURE_CONTENT_TYPE_NOSNIFF  = True
    SECURE_HSTS_SECONDS          = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD          = True

# ── Logging — always print full tracebacks to stderr (visible in Railway logs) ─
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {module} {process}: {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'allauth': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        # DEBUG only when DEBUG is on. In production this logger carries per-query
        # retrieval detail for every request; the volume buries the ERROR lines
        # that actually need attention. No patient content is logged at any level
        # (queries are logged as q_len, never as text), so this is about signal,
        # not disclosure.
        'apps': {'handlers': ['console'],
                 'level': 'DEBUG' if DEBUG else 'INFO',
                 'propagate': False},
        # Structured operational events — see healthcompass/observability.py.
        # Separated from 'apps' so a log drain or alert rule can subscribe to
        # failures that make medical data unretrievable or unnotified without
        # also ingesting routine application logging.
        'healthcompass.ops': {'handlers': ['console'], 'level': 'INFO',
                              'propagate': False},
    },
}

# ── REST Framework (DRF) ───────────────────────────────────────────────────────
from datetime import timedelta

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    # Baseline ceiling for every API endpoint. Deliberately generous — it exists
    # to stop runaway clients and scripted abuse, not to shape normal traffic.
    # Endpoints that are expensive (AI, uploads, inference) or security-sensitive
    # (auth) declare a tighter scope via apps/api/throttling.py.
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.AnonRateThrottle',
    ),
    # Client IP for anonymous throttles: the right-most XFF hop our proxy added,
    # never the client-controlled left-most one (SEC-06).
    'NUM_PROXIES': TRUSTED_PROXY_COUNT,
    'DEFAULT_PAGINATION_CLASS': 'apps.api.pagination.StandardPagination',
    'PAGE_SIZE': 20,
    # One error body for unhandled exceptions: {error, code, reference}.
    'EXCEPTION_HANDLER': 'apps.api.errors.exception_handler',
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_RATES': {
        'user':           config('THROTTLE_USER',           default='1000/hour'),
        'anon':           config('THROTTLE_ANON',           default='100/hour'),
        # Authentication — keyed by IP. Burst stops credential stuffing;
        # sustained stops slow distributed guessing from one address.
        'auth_burst':     config('THROTTLE_AUTH_BURST',     default='10/min'),
        'auth_sustained': config('THROTTLE_AUTH_SUSTAINED', default='60/hour'),
        'register':       config('THROTTLE_REGISTER',       default='5/hour'),
        'password_reset': config('THROTTLE_PASSWORD_RESET', default='5/hour'),
        # Expensive operations — keyed by user.
        'ai':             config('THROTTLE_AI',             default='20/min'),
        'ai_daily':       config('THROTTLE_AI_DAILY',       default='300/day'),
        'upload':         config('THROTTLE_UPLOAD',         default='60/hour'),
        'ocr':            config('THROTTLE_OCR',            default='30/hour'),
        'prediction':     config('THROTTLE_PREDICTION',     default='60/hour'),
        # Export reads every record and file the user owns.
        'export':         config('THROTTLE_EXPORT',         default='5/hour'),
    },
}

# Rate limits for the server-rendered (non-DRF) auth views, applied with
# django-ratelimit. Kept separate from DRF scopes because those views are not
# DRF and are keyed by IP rather than by authenticated user.
RATELIMIT_LOGIN          = config('RATELIMIT_LOGIN',          default='10/m')
RATELIMIT_REGISTER       = config('RATELIMIT_REGISTER',       default='5/h')
RATELIMIT_PASSWORD_RESET = config('RATELIMIT_PASSWORD_RESET', default='5/h')

# Set True in tests that are not specifically exercising throttling.
RATELIMIT_ENABLE = config('RATELIMIT_ENABLE', default=True, cast=bool)
# A blocked request is a 429 with Retry-After, not a 403.
RATELIMIT_VIEW = 'healthcompass.error_views.ratelimited'
CSRF_FAILURE_VIEW = 'healthcompass.error_views.csrf_failure'

# Short access tokens, refresh tokens blacklisted on rotation, logout and
# password change: a stolen token is useful for minutes, not a month (SEC-04).
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=14),
    'ROTATE_REFRESH_TOKENS':  True,
    'BLACKLIST_AFTER_ROTATION': True,
    'UPDATE_LAST_LOGIN':      True,
}

# ── OpenAPI schema (drf-spectacular) ─────────────────────────────────────────
# Regenerate after changing the API:
#   python manage.py spectacular --file openapi.yaml
# CI fails when the committed openapi.yaml is stale.
SPECTACULAR_SETTINGS = {
    'TITLE': 'HealthCompass API',
    'DESCRIPTION': 'Mobile API. JWT bearer authentication (auth/login/, auth/refresh/).',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'SERVE_PERMISSIONS': ['rest_framework.permissions.IsAdminUser'],
    'COMPONENT_SPLIT_REQUEST': True,
    # PatientReport.input_method and TaskOccurrence.response_input share choices.
    'ENUM_NAME_OVERRIDES': {'InputMethodEnum': 'apps.care.models.PatientReport.InputMethod'},
}

# ── CORS (allow Flutter app to call this API) ──────────────────────────────────
CORS_ALLOWED_ORIGINS = config('CORS_ALLOWED_ORIGINS', default='http://localhost:3000', cast=Csv())
CORS_ALLOW_CREDENTIALS = True
# Allow any localhost port only in dev — in production this regex is absent so
# only CORS_ALLOWED_ORIGINS (set via env var) controls access.
if DEBUG:
    CORS_ALLOWED_ORIGIN_REGEXES = [r'^http://localhost(:\d+)?$']

# ── Date format disambiguation ─────────────────────────────────────────────────
# Controls how parsers resolve ambiguous numeric dates like 03/04/2024 where both
# d/m/Y and m/d/Y are structurally valid.  'eu' = day/month/year (Finnish Kanta
# convention).  'us' = month/day/year.  Dates with an unambiguous component
# (one value > 12, ISO order, or alphabetic month) are never affected by this.
DATE_FORMAT_PREFERENCE = config('DATE_FORMAT_PREFERENCE', default='eu')

# ── RAG auto-index mode ────────────────────────────────────────────────────────
# When True, rag_assistant.signals indexes records synchronously (no background
# thread). Set automatically during `manage.py test` so test cases are
# deterministic and free from SQLite "table is locked" race conditions.
import sys as _sys
RAG_AUTO_INDEX_SYNC = 'test' in _sys.argv

# ── Background jobs (django-rq; healthcompass/tasks.py) ────────────────────────
# Async only with a Redis CACHE_URL and outside the test runner; otherwise jobs
# run inline. Worker: `python manage.py rqworker ingest default`.
RQ_ASYNC = config('RQ_ASYNC', default=bool(_CACHE_URL) and 'test' not in _sys.argv, cast=bool)
RQ_JOB_TIMEOUT = config('RQ_JOB_TIMEOUT', default=600, cast=int)
# In-database vector search on PostgreSQL (pgvector). Set False to fall back
# to the NumPy path, e.g. while backfill_embedding_vectors --verify is pending.
RAG_PGVECTOR = config('RAG_PGVECTOR', default=True, cast=bool)
RQ_QUEUES = {
    'ingest':  {'URL': _CACHE_URL or 'redis://localhost:6379/0', 'DEFAULT_TIMEOUT': RQ_JOB_TIMEOUT},
    'default': {'URL': _CACHE_URL or 'redis://localhost:6379/0', 'DEFAULT_TIMEOUT': RQ_JOB_TIMEOUT},
}

# ── MFA enforcement for privileged roles ───────────────────────────────────────
# apps.accounts.mfa.RequireMFAMiddleware. Off under the test runner so the many
# tests that sign in a clinician with force_login are not redirected to
# enrolment; the MFA tests switch it on explicitly.
MFA_ENFORCED = config('MFA_ENFORCED', default='test' not in _sys.argv, cast=bool)

# ── The test suite must not call the embedding provider ───────────────────────
#
# RAG_AUTO_INDEX_SYNC above makes the post_save auto-indexer run inline, so
# every test that creates a MedicalRecord — hundreds of them, most having
# nothing to do with retrieval — sent that record's text to Google using the
# developer's real API key.
#
# Two consequences, both real, both observed. It spent the shared free-tier
# embedding quota until production requests were refused with 429; and it sent
# test-fixture text to a third party from a suite nobody thinks of as making
# network calls.
#
# Every test that genuinely exercises embedding already patches `_call_api` or
# `embed_batch`, so nothing depends on the outbound call. It is refused at that
# one choke point instead; a test that truly needs the provider can override
# this setting deliberately, which is the point — reaching an external service
# should be something a test asks for, not something it does by accident.
RAG_ALLOW_EXTERNAL_EMBEDDING = config(
    'RAG_ALLOW_EXTERNAL_EMBEDDING', default='test' not in _sys.argv, cast=bool)

# ── Test uploads go to a throwaway directory ──────────────────────────────────
#
# The suite writes real files: every test that saves a MedicalRecord, a
# prediction input or a profile picture lands one in MEDIA_ROOT and nothing
# removes it. A full run left well over a thousand unreferenced files in the
# development media directory, discovered when `reconcile_orphaned_files`
# reported 1231 orphans out of 3399 files.
#
# Two consequences, both bad. Developer machines accumulate patient-shaped test
# data indefinitely, and — worse — no file test ever ran against a clean
# baseline, so anything reasoning about "which files exist" was judging
# accumulated debris. The orphan reconciliation tests had to isolate themselves
# to mean anything.
#
# A per-run temporary directory fixes both. It is NOT cleaned up on exit: the
# OS reclaims it, and keeping it lets a failed run be inspected afterwards.
if 'test' in _sys.argv:
    import tempfile as _tempfile
    MEDIA_ROOT = Path(_tempfile.mkdtemp(prefix='healthcompass-test-media-'))

# ── Error tracking (Sentry, audit ARCH-09) ───────────────────────────────────
# Off unless SENTRY_DSN is set. Use an EU-region project. PII and request
# bodies are never sent; see healthcompass/sentry.py for the scrubbing.
SENTRY_DSN = config('SENTRY_DSN', default='')
if SENTRY_DSN and 'test' not in _sys.argv:
    from healthcompass import sentry as _sentry
    _sentry.init(SENTRY_DSN,
                 environment=config('SENTRY_ENVIRONMENT', default='production'),
                 traces_sample_rate=config('SENTRY_TRACES_SAMPLE_RATE', default=0.05, cast=float))
    LOGGING['handlers']['sentry_ops'] = {'class': 'healthcompass.sentry.OpsToSentryHandler',
                                         'level': 'ERROR'}
    LOGGING['loggers']['healthcompass.ops']['handlers'].append('sentry_ops')

# Load testing only: the assistant answers with a stub after this many seconds
# instead of calling a provider. Honoured only with DEBUG=True (loadtest/README.md).
LOADTEST_STUB_LLM_DELAY = config('LOADTEST_STUB_LLM_DELAY', default=0, cast=float)
