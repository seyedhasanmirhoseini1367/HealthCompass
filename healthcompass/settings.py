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
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=Csv())

AUTHENTICATION_BACKENDS = [
    'accounts.backends.EmailOrUsernameBackend',
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
    'accounts',
    'medical_records',
    'ai_insights',
    'rag_assistant',
    'dashboard',
    'stories',
    'notifications',
    'integrations',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # WhiteNoise must be directly after SecurityMiddleware
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
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
                'notifications.context_processors.unread_notifications',
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

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# ── Email ──────────────────────────────────────────────────────────────────────
# Console backend for local dev; SMTP in production (set EMAIL_HOST_USER etc.)
EMAIL_BACKEND = (
    'django.core.mail.backends.console.EmailBackend'
    if DEBUG else
    'django.core.mail.backends.smtp.EmailBackend'
)
EMAIL_HOST         = config('EMAIL_HOST',         default='smtp.gmail.com')
EMAIL_PORT         = config('EMAIL_PORT',         default=587, cast=int)
EMAIL_USE_TLS      = True
EMAIL_HOST_USER    = config('EMAIL_HOST_USER',    default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL  = config('DEFAULT_FROM_EMAIL',  default='noreply@healthcompass.dev')

# ── AI APIs ────────────────────────────────────────────────────────────────────
GROQ_API_KEY      = config('GROQ_API_KEY',      default='')
GEMINI_API_KEY    = config('GEMINI_API_KEY',    default='')
ANTHROPIC_API_KEY = config('ANTHROPIC_API_KEY', default='')
OPENAI_API_KEY    = config('OPENAI_API_KEY',    default='')

# ── RAG Configuration ──────────────────────────────────────────────────────────
RAG_CONFIG = {
    'EMBEDDING_MODEL':   'sentence-transformers/all-MiniLM-L6-v2',
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
    'CONTEXT_TYPE_BOOST': 0.08,
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
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'https'
LOGIN_REDIRECT_URL  = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

SOCIALACCOUNT_ADAPTER       = 'accounts.adapters.HealthCompassSocialAdapter'
SOCIALACCOUNT_SIGNUP_FORM_CLASS = 'accounts.forms.SocialSignupRoleForm'
SOCIALACCOUNT_AUTO_SIGNUP   = False   # always show role-selection form for new social users
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True  # link Google to existing email account
ACCOUNT_EMAIL_VERIFICATION  = 'none'
ACCOUNT_EMAIL_REQUIRED      = True
ACCOUNT_USERNAME_REQUIRED   = False

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APP': {
            'client_id': config('GOOGLE_CLIENT_ID',     default=''),
            'secret':    config('GOOGLE_CLIENT_SECRET', default=''),
            'key': '',
        },
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
    }
}

# ── Production security headers ────────────────────────────────────────────────
# Railway terminates SSL at the edge, so no SECURE_SSL_REDIRECT needed.
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE    = True
    SECURE_BROWSER_XSS_FILTER   = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
