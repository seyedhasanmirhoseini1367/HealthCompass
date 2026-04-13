from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'healthcompass-dev-secret-key')
DEBUG = os.getenv('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
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
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
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

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
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

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/'

# Email
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'  # Console for dev
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = True
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@healthcompass.dev')

# AI APIs
GEMINI_API_KEY    = os.getenv('GEMINI_API_KEY', '')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY', '')
OPENAI_API_KEY    = os.getenv('OPENAI_API_KEY', '')

# ── RAG Configuration ──────────────────────────────────────────────────────────
RAG_CONFIG = {
    # Embedding model — same family as PersonalPortfolio, medical-optimised variant
    'EMBEDDING_MODEL':   'sentence-transformers/all-MiniLM-L6-v2',
    # Chunking
    'CHUNK_SIZE':        200,   # words per chunk
    'CHUNK_OVERLAP':     40,    # overlapping words between consecutive chunks
    # Retrieval
    'TOP_K':             6,     # final chunks sent to LLM
    'BM25_WEIGHT':       0.35,  # weight for BM25 keyword score in hybrid retrieval
    'SEMANTIC_WEIGHT':   0.65,  # weight for cosine similarity score
    'TIME_DECAY_DAYS':   365,   # records older than this are down-weighted
    'TIME_DECAY_FACTOR': 0.15,  # max score reduction from time decay (0–1)
    'MMR_LAMBDA':        0.6,   # MMR diversity param (1=pure relevance, 0=pure diversity)
    'SIM_THRESHOLD':     0.15,  # minimum similarity to include a chunk
    # Generation
    'GEMINI_MODEL':      'gemini-1.5-flash',
    'ANTHROPIC_MODEL':   'claude-haiku-4-5-20251001',
    'OPENAI_MODEL':      'gpt-4o-mini',
    'MAX_TOKENS':        800,
    # Vector store (file cache — speeds up cold retrieval)
    'VECTOR_STORE_PATH': str(BASE_DIR / 'rag_vector_store'),
}

# File upload limits (50MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 52428800
FILE_UPLOAD_MAX_MEMORY_SIZE = 52428800
