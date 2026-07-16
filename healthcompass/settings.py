from pathlib import Path
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Core security ──────────────────────────────────────────────────────────────
# No default — Railway (and any production environment) MUST set this explicitly.
SECRET_KEY    = config('SECRET_KEY')
DEBUG         = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='127.0.0.1,localhost', cast=Csv())

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

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    # allauth
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    # Project apps
    'apps.accounts',
    'apps.medical_records',
    'apps.ai_insights',
    'apps.rag_assistant',
    'apps.dashboard',
    'apps.notifications',
    'apps.appointments',
    # Mobile API
    'apps.api',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise must be directly after SecurityMiddleware
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

ROOT_URLCONF = 'healthcompass.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.notifications.context_processors.unread_notifications',
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
    )
}

AUTH_USER_MODEL = 'accounts.CustomUser'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Europe/Helsinki'
USE_I18N = True
USE_TZ = True

# ── Static files ───────────────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
# Compress and cache-bust static files in production
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

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
_OBJECT_STORAGE_URL = config('OBJECT_STORAGE_URL', default='')
if _OBJECT_STORAGE_URL:
    INSTALLED_APPS += ['storages']
    STORAGES = {
        'default': {'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage'},
        'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage'},
    }
    AWS_S3_ENDPOINT_URL      = _OBJECT_STORAGE_URL
    AWS_STORAGE_BUCKET_NAME  = config('STORAGE_BUCKET_NAME', default='healthcompass-media')
    AWS_ACCESS_KEY_ID        = config('STORAGE_ACCESS_KEY',  default='')
    AWS_SECRET_ACCESS_KEY    = config('STORAGE_SECRET_KEY',  default='')
    AWS_S3_FILE_OVERWRITE    = False
    AWS_DEFAULT_ACL          = None
    MEDIA_URL                = f'{_OBJECT_STORAGE_URL}/{AWS_STORAGE_BUCKET_NAME}/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# ── Email ──────────────────────────────────────────────────────────────────────
EMAIL_HOST         = config('EMAIL_HOST',         default='smtp.gmail.com')
EMAIL_PORT         = config('EMAIL_PORT',         default=587, cast=int)
EMAIL_USE_TLS      = True
EMAIL_HOST_USER    = config('EMAIL_HOST_USER',    default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL  = config('DEFAULT_FROM_EMAIL',  default='noreply@healthcompass.dev')

# Use SMTP only when credentials are actually configured; fall back to console
if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_TIMEOUT = 10  # fail fast so gunicorn worker doesn't get killed waiting
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# ── AI APIs ────────────────────────────────────────────────────────────────────
GROQ_API_KEY      = config('GROQ_API_KEY',      default='')
GEMINI_API_KEY    = config('GEMINI_API_KEY',    default='')
ANTHROPIC_API_KEY = config('ANTHROPIC_API_KEY', default='')
OPENAI_API_KEY    = config('OPENAI_API_KEY',    default='')

# ── RAG Configuration ──────────────────────────────────────────────────────────
RAG_CONFIG = {
    'EMBEDDING_MODEL':   'models/gemini-embedding-001',
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
    'VECTOR_STORE_PATH': str(BASE_DIR / 'rag_vector_store'),
}

# File upload limits (50 MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 52428800
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800

# ── Proxy / HTTPS (Railway terminates SSL at edge) ────────────────────────────
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST    = True

# ── Google OAuth (allauth) ─────────────────────────────────────────────────────
SITE_ID = 1
SITE_DOMAIN = config('SITE_DOMAIN', default='healthcompass.hasanai.net')
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'http' if DEBUG else 'https'
LOGIN_REDIRECT_URL  = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

SOCIALACCOUNT_ADAPTER       = 'apps.accounts.adapters.HealthCompassSocialAdapter'
SOCIALACCOUNT_AUTO_SIGNUP   = True    # auto-create user; role selection handled post-signup
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True  # link Google to existing email account
SOCIALACCOUNT_LOGIN_ON_GET  = True    # skip confirmation page; GET directly redirects to Google
ACCOUNT_EMAIL_VERIFICATION  = 'none'
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
QUERYLOG_RETENTION_DAYS = config('QUERYLOG_RETENTION_DAYS', default=90, cast=int)

# ── Production security headers ────────────────────────────────────────────────
# Railway terminates SSL at the edge, so no SECURE_SSL_REDIRECT needed.
if not DEBUG:
    SESSION_COOKIE_SECURE        = True
    CSRF_COOKIE_SECURE           = True
    SECURE_BROWSER_XSS_FILTER    = True
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
        'allauth': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
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
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME':  timedelta(hours=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=30),
    'ROTATE_REFRESH_TOKENS':  True,
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
