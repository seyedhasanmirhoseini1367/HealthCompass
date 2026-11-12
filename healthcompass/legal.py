"""
What the legal pages say, built from configuration (audit UX-08).

The privacy page lists the processors this deployment actually uses, read from
settings, so it cannot drift from the code: the AI vendors allowed to receive
health data (PHI_LLM_PROVIDERS), hosting, database, email, error tracking when
SENTRY_DSN is set, and the third-party seizure service when Labs are enabled.

The owner must fill in who the controller is and the legal basis. Until they
do, the pages show a TODO banner in development and `manage.py check --deploy`
fails (pages.E001), so a production deploy cannot go out with placeholders.
"""
from django.conf import settings
from django.core.checks import Error, register

LLM_VENDORS = {
    'gemini':    ('Google (Gemini API)', 'AI answers, document reading, embeddings'),
    'anthropic': ('Anthropic (Claude API)', 'AI answers (fallback)'),
    'groq':      ('Groq', 'AI answers'),
    'openai':    ('OpenAI', 'AI answers'),
}

#: Setting name -> what the owner must provide.
OWNER_FIELDS = {
    'LEGAL_CONTROLLER_NAME':    'the legal name of the data controller',
    'LEGAL_CONTROLLER_ADDRESS': 'the controller’s postal address',
    'LEGAL_BASIS':              'the legal basis for processing health data (GDPR Art. 6 and 9)',
    'CONTACT_EMAIL':            'a contact address for privacy requests',
    'LEGAL_HOSTING_PROVIDER':   'the hosting provider and region',
    'LEGAL_DATABASE_PROVIDER':  'the database provider and region',
}


def processors():
    rows = []
    for key in getattr(settings, 'PHI_LLM_PROVIDERS', []):
        vendor = LLM_VENDORS.get(str(key).strip().lower())
        if vendor:
            rows.append({'name': vendor[0], 'purpose': vendor[1] + ', only with your consent'})
    rows.append({'name': getattr(settings, 'LEGAL_HOSTING_PROVIDER', '') or 'Hosting provider (TODO)',
                 'purpose': 'Runs the application'})
    rows.append({'name': getattr(settings, 'LEGAL_DATABASE_PROVIDER', '') or 'Database provider (TODO)',
                 'purpose': 'Stores your account and records'})
    if getattr(settings, 'OBJECT_STORAGE_URL', ''):
        rows.append({'name': getattr(settings, 'LEGAL_STORAGE_PROVIDER', '') or 'File storage provider',
                     'purpose': 'Stores uploaded files'})
    rows.append({'name': getattr(settings, 'LEGAL_EMAIL_PROVIDER', '') or 'Email provider',
                 'purpose': 'Sends account and reminder emails'})
    if getattr(settings, 'SENTRY_DSN', ''):
        rows.append({'name': 'Sentry (Functional Software Inc.)',
                     'purpose': 'Error reports, without health data or request bodies'})
    rows.append({'name': 'Google', 'purpose': 'Sign-in, only if you choose Google sign-in'})
    if getattr(settings, 'LABS_ENABLED', False):
        rows.append({'name': 'hasanai.net', 'purpose': 'EEG seizure analysis research demo (Labs)'})
    return rows


def missing_owner_fields():
    return [desc for name, desc in OWNER_FIELDS.items() if not getattr(settings, name, '')]


def legal_context():
    return {
        'processors':      processors(),
        'retention_days':  getattr(settings, 'QUERYLOG_RETENTION_DAYS', 90),
        'controller_name': getattr(settings, 'LEGAL_CONTROLLER_NAME', ''),
        'controller_address': getattr(settings, 'LEGAL_CONTROLLER_ADDRESS', ''),
        'legal_basis':     getattr(settings, 'LEGAL_BASIS', ''),
        'contact_email':   getattr(settings, 'CONTACT_EMAIL', ''),
        'owner_todos':     missing_owner_fields(),
        'show_todos':      settings.DEBUG,
    }


@register('pages', deploy=True)
def check_legal_pages_are_filled_in(app_configs, **kwargs):
    missing = missing_owner_fields()
    if missing:
        return [Error('The privacy page still has placeholders: ' + '; '.join(missing) + '.',
                      hint='Set ' + ', '.join(n for n in OWNER_FIELDS if not getattr(settings, n, ''))
                           + ' in the environment.',
                      id='pages.E001')]
    return []
