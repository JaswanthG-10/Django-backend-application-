"""Django settings for Data Extraction Service.

Generated for GreenTree / Springer Capital backend development practical assignment.
"""
import hashlib
import hmac
import sys
import urllib.parse
from pathlib import Path

import dj_database_url
from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def parse_bool(val) -> bool:
    """Robust boolean parser for environment variables."""
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    val_str = str(val).strip().lower()
    if val_str in {'true', '1', 't', 'yes', 'y'}:
        return True
    if val_str in {'false', '0', 'f', 'no', 'n', ''}:
        return False
    raise ValueError(f"Invalid boolean value for environment variable: '{val}'")


def _is_prohibited_hash(raw_value: str, prohibited_hashes: set[str]) -> bool:
    """Compares the SHA-256 hash of a configured value against a set of prohibited hashes using constant-time comparison."""
    if not raw_value:
        return True
    val_hash = hashlib.sha256(raw_value.strip().encode('utf-8')).hexdigest()
    return any(hmac.compare_digest(val_hash, target_hash) for target_hash in prohibited_hashes)


# SHA-256 hashes of known leaked or placeholder SECRET_KEY values from repository history.
# Real values must never be stored in this codebase in plaintext.
_PROHIBITED_SECRET_KEY_HASHES = {
    'cd52fb323973e36c5895e097899bb2885aa716f13e591f2e12d77eb1a1159165',  # historical leaked key (b7fed16)
    'ebff49a876eabfabec6bad6a0c06941ff52a12f8aa6c75a704cee2aa4c83d4e2',  # historical leaked key (b7fed16)
    'eb379dc263b4690252d0f1f3ef7f6b1b35fce3bea3c1c524b402f6e66763bba1',  # historical leaked key (b28df54)
    '8aa313ec2414e460fb363375ed5f70a7aaa14af264a8fd06ac28fdf8d62bde18',  # historical leaked key (fc8a70b)
    'e74394b2277fee8790f18c7bb8c6cf479b62a4457c0a1f6c9c344065b22e8889',  # dev default key
    'ac94c3bcde948f9a22df685c6f1324a8260f2dc991f0e5bee078cae890b282aa',  # .env.example placeholder
    '6d8deb35eb47ae68b603db2cbd246af6090e5b1dc64b63038553ed94eb55a6b3',  # .env.production.example placeholder
    '9f929ee0b80bb605058059a6f3182dab67cbef5c8002fb670c69aa36a8cd6186',  # placeholder key
    'a4c5eea03c1da8a16a7bd8af0c9a1f867eb9e040d65ecaed21aea9c89ab25661',  # CI test key (blocked outside CI)
}

IS_CI = config('CI', default=False, cast=parse_bool) or config('GITHUB_ACTIONS', default=False, cast=parse_bool)

_CI_TEST_HASHES = {
    'a4c5eea03c1da8a16a7bd8af0c9a1f867eb9e040d65ecaed21aea9c89ab25661',  # CI SECRET_KEY
    '3640bb0393ef253c91ad7bad190fe0104af88b1638fb2c3dd0f321b773d561ef',  # CI API_KEY_SECRET
    '67964f0a632e52a909fe59facc9b501654cb41b18256ed50d2b76bc7f21edf8e',  # CI FIELD_ENCRYPTION_KEY
}

# Security Settings: Defaults to False in production
DEBUG = config('DEBUG', default=False, cast=parse_bool)

# In development/test mode, fall back to a clearly designated dummy key if unset.
# In production (DEBUG=False), require an explicit, strong SECRET_KEY.
_DEFAULT_DEV_SECRET = 'django-insecure-dev-fallback-key-for-local-testing-only-991823719238'
SECRET_KEY = config('SECRET_KEY', default=_DEFAULT_DEV_SECRET if DEBUG else '')
effective_secret_key_prohibited = (
    _PROHIBITED_SECRET_KEY_HASHES
    if not IS_CI
    else (_PROHIBITED_SECRET_KEY_HASHES - _CI_TEST_HASHES)
)
if not DEBUG:
    if not SECRET_KEY:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable must be set securely when running with DEBUG=False. "
            "Generate one with: python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
        )
    if (
        SECRET_KEY == _DEFAULT_DEV_SECRET
        or SECRET_KEY.startswith('django-insecure-')
        or len(SECRET_KEY) < 32
        or _is_prohibited_hash(SECRET_KEY, effective_secret_key_prohibited)
    ):
        raise ImproperlyConfigured(
            "A strong, unique SECRET_KEY of at least 32 characters must be configured in production (DEBUG=False). "
            "Insecure default, placeholder, or previously compromised keys are strictly prohibited. "
            "Generate one with: python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
        )

ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1,0.0.0.0,testserver' if DEBUG else '',
    cast=Csv()
)
if not DEBUG and not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS environment variable must be configured when running with DEBUG=False.")

CSRF_TRUSTED_ORIGINS = config(
    'CSRF_TRUSTED_ORIGINS',
    default='http://localhost:8000,http://127.0.0.1:8000',
    cast=Csv()
)

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party apps
    'rest_framework',
    'django_filters',
    'drf_spectacular',

    # Local apps
    'apps.common',
    'apps.extraction',
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

ROOT_URLCONF = 'config.urls'

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
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

# Database Configuration
_DEFAULT_SQLITE_URL = f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
DATABASE_URL = config('DATABASE_URL', default=_DEFAULT_SQLITE_URL if DEBUG else '')
if not DATABASE_URL and not DEBUG:
    raise ImproperlyConfigured("DATABASE_URL environment variable must be set securely when running with DEBUG=False.")
if not DEBUG and DATABASE_URL.startswith('sqlite'):
    raise ImproperlyConfigured(
        "SQLite is not permitted in production (DEBUG=False). Please configure a PostgreSQL DATABASE_URL. "
        "Example: DATABASE_URL=postgresql://username:password@hostname:5432/dbname?sslmode=require"
    )

_is_postgres = not DATABASE_URL.startswith('sqlite')
_is_local_db = any(h in DATABASE_URL for h in ('localhost', '127.0.0.1', 'testserver'))
_ssl_require = config('DB_SSL_REQUIRE', default=(_is_postgres and not _is_local_db), cast=bool)
_is_pooler = 'pooler.supabase.com' in DATABASE_URL or ':6543' in DATABASE_URL
_conn_max_age = config('DB_CONN_MAX_AGE', default=(0 if _is_pooler or DATABASE_URL.startswith('sqlite') else 600), cast=int)
_disable_server_side_cursors = config('DB_DISABLE_SERVER_SIDE_CURSORS', default=_is_pooler, cast=bool)

_db_config = dj_database_url.parse(
    DATABASE_URL,
    conn_max_age=_conn_max_age,
    ssl_require=_ssl_require,
)
if _disable_server_side_cursors:
    _db_config['DISABLE_SERVER_SIDE_CURSORS'] = True

_is_test_runner = 'pytest' in sys.modules or (len(sys.argv) > 1 and sys.argv[1] == 'test')
if _is_test_runner and _is_pooler:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }
else:
    DATABASES = {
        'default': _db_config
    }

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Django REST Framework Settings
REST_FRAMEWORK = {
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
    'DEFAULT_PARSER_CLASSES': [
        'rest_framework.parsers.JSONParser',
        'rest_framework.parsers.FormParser',
        'rest_framework.parsers.MultiPartParser',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'apps.common.authentication.ApiKeyAuthentication',
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PAGINATION_CLASS': 'apps.common.pagination.StandardLimitOffsetPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'EXCEPTION_HANDLER': 'apps.common.exceptions.custom_exception_handler',
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

# OpenAPI Documentation via drf-spectacular
SPECTACULAR_SETTINGS = {
    'TITLE': 'Data Extraction Service API',
    'DESCRIPTION': (
        'Production-quality REST API for third-party data extraction jobs, status polling, '
        'paginated result retrieval, job lifecycle management, and aggregated statistics.'
    ),
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'TAGS': [
        {'name': 'Scan Operations', 'description': 'Endpoints for triggering, tracking, cancelling, and deleting extraction jobs.'},
        {'name': 'Job Analytics & Management', 'description': 'Endpoints for listing historical jobs and querying aggregated metrics.'},
        {'name': 'Health', 'description': 'Service operational health check.'},
    ],
    'APPEND_COMPONENTS': {
        'securitySchemes': {
            'ApiKeyAuth': {
                'type': 'apiKey',
                'in': 'header',
                'name': 'X-API-KEY',
                'description': 'Enter service API key to authenticate requests',
            }
        }
    },
    'SECURITY': [{'ApiKeyAuth': []}],
}

# API Key Security for API Callers
# In development/test mode, fall back to a clearly designated dev key if unset.
# In production (DEBUG=False), require an explicit unique API_KEY_SECRET.
_DEFAULT_DEV_API_KEY = 'dev-local-api-key-secret-do-not-use-in-production'

# SHA-256 hashes of known leaked or placeholder API_KEY_SECRET values from repository history.
# Real values must never be stored in this codebase in plaintext.
_PROHIBITED_API_KEY_HASHES = {
    '51e350dcd3f6c6380813ed9a9b16f749de49da318cf8a2778fc760e0c5228e42',  # historical leaked key (b28df54 / settings.py)
    '8e9e3fd49b5764f99ce4e55810419f319bdb251ce40e1a37baa9f1d46f33722a',  # historical leaked key (b7fed16)
    '1d8adf17d77e0029a701fe752ee90df9c3cd41d13ac19f70202be51253f6b66c',  # historical leaked key (fc8a70b)
    'e24b59f1597085251e5fbd3f55bcc9c7b36ae4fd5b0ea6415ec1f1c93694073c',  # dev default key
    '5dd1c621da575a39427cf13fd0239d1f36c199ffd75a071dff7bd55d8c78b11d',  # dev placeholder
    '4c806362b613f7496abf284146efd31da90e4b16169fe001841ca17290f427c4',  # test placeholder
    '2ceac6f36363c6246a64cca805cd43ca7a01b14eb2fcc532ceec3f60f2f7df1c',  # test placeholder
    '84a3441a2ab0ae44267ee4a2d80df899b645f71fec8b5d50547946fa820946cc',  # test fixture
    '6fd9cff3ca9c69569c6026ee6f32703cefb6856487f702b210e729acc0b3a802',  # .env.production.example placeholder
    '12cb13916a614e76312ce963e93fd59091f58d2dde445d334e5b6634f09a4fb8',  # .env.example placeholder
    '3640bb0393ef253c91ad7bad190fe0104af88b1638fb2c3dd0f321b773d561ef',  # CI test key (blocked outside CI)
}

API_KEY_SECRET = config('API_KEY_SECRET', default=_DEFAULT_DEV_API_KEY if DEBUG else '')
effective_api_key_prohibited = (
    _PROHIBITED_API_KEY_HASHES
    if not IS_CI
    else (_PROHIBITED_API_KEY_HASHES - _CI_TEST_HASHES)
)
if not DEBUG and (not API_KEY_SECRET or _is_prohibited_hash(API_KEY_SECRET, effective_api_key_prohibited)):
    raise ImproperlyConfigured(
        "A unique, production-grade API_KEY_SECRET must be configured when running with DEBUG=False. "
        "Shared development credentials, placeholder keys, and previously compromised keys are strictly prohibited in production. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )

# Request Signing Security (HMAC-SHA256 Payload & Timestamp Verification)
API_REQUIRE_REQUEST_SIGNING = config('API_REQUIRE_REQUEST_SIGNING', default=False, cast=bool)
API_SIGNING_TOLERANCE_SECONDS = config('API_SIGNING_TOLERANCE_SECONDS', default=300, cast=int)

# SHA-256 hashes of known leaked or placeholder FIELD_ENCRYPTION_KEY values from repository history.
_PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES = {
    '256684f18ff3f29c4cfc74a4efa2761477ce29ae0dfd74d940b24c7ba0bb51d0',  # .env.production.example placeholder
    'e349acf30462e809d38bea698bad2ea8a222f6adc41273ba4f6475fb12b8cc97',  # .env.example placeholder
    '67964f0a632e52a909fe59facc9b501654cb41b18256ed50d2b76bc7f21edf8e',  # CI test key (blocked outside CI)
}

# Field-Level Symmetric Encryption Key for sensitive tokens at rest (Fernet key)
# If unset in development/test mode, deterministically derived from SECRET_KEY.
# In production (DEBUG=False), require an explicit valid 32-byte Fernet key.
FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default=None)
effective_field_encryption_prohibited = (
    _PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES
    if not IS_CI
    else (_PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES - _CI_TEST_HASHES)
)
if not FIELD_ENCRYPTION_KEY and not DEBUG:
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEY environment variable must be set securely when running with DEBUG=False. "
        "Generate a 32-byte Fernet key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
if FIELD_ENCRYPTION_KEY:
    try:
        from cryptography.fernet import Fernet
        raw_keys = (
            [k.strip() for k in FIELD_ENCRYPTION_KEY.split(',') if k.strip()]
            if isinstance(FIELD_ENCRYPTION_KEY, str)
            else [FIELD_ENCRYPTION_KEY]
        )
        if not raw_keys:
            raise ValueError("No encryption keys found in FIELD_ENCRYPTION_KEY.")
        for rk in raw_keys:
            rk_str = rk if isinstance(rk, str) else rk.decode('utf-8', errors='ignore')
            if not DEBUG and _is_prohibited_hash(rk_str, effective_field_encryption_prohibited):
                raise ImproperlyConfigured(
                    "A unique, production-grade FIELD_ENCRYPTION_KEY must be configured when running with DEBUG=False. "
                    "Placeholder, CI, and previously compromised encryption keys are strictly prohibited in production. "
                    "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
                )
            Fernet(rk.encode('utf-8') if isinstance(rk, str) else rk)
    except ImproperlyConfigured:
        raise
    except Exception as exc:
        raise ImproperlyConfigured(
            "Invalid FIELD_ENCRYPTION_KEY. Must be a valid 32-byte url-safe base64-encoded Fernet key "
            "(or comma-separated list of keys for key rotation). "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ) from exc

# Asynchronous background execution (worker / thread runner)
EXTRACTION_ASYNC_EXECUTION = config('EXTRACTION_ASYNC_EXECUTION', default=True, cast=bool)
EXTRACTION_MAX_WORKERS = config('EXTRACTION_MAX_WORKERS', default=4, cast=int)
EXTRACTION_JOB_TIMEOUT_MINUTES = config('EXTRACTION_JOB_TIMEOUT_MINUTES', default=15, cast=int)
EXTRACTION_MAX_RETRIES = config('EXTRACTION_MAX_RETRIES', default=3, cast=int)

# Heartbeat and Stale Job Recovery Configuration
EXTRACTION_HEARTBEAT_INTERVAL_SECONDS = config('EXTRACTION_HEARTBEAT_INTERVAL_SECONDS', default=30, cast=int)
EXTRACTION_STALE_THRESHOLD_MINUTES = config('EXTRACTION_STALE_THRESHOLD_MINUTES', default=15, cast=int)

# External Extraction Service Client Configuration
# Real, documented production default: HubSpot CRM API (https://developers.hubspot.com/docs/api/crm/contacts)
# Fully configurable to other CRM or REST providers via environment variables.
EXTERNAL_API_BASE_URL = config('EXTERNAL_API_BASE_URL', default='https://api.hubapi.com')
EXTERNAL_API_TIMEOUT = config('EXTERNAL_API_TIMEOUT', default=10, cast=int)
EXTERNAL_API_DEFAULT_TOKEN = config('EXTERNAL_API_DEFAULT_TOKEN', default='')
EXTERNAL_API_PAGINATION_TYPE = config('EXTERNAL_API_PAGINATION_TYPE', default='auto')  # auto, cursor, page_number, offset_limit
EXTERNAL_API_ENDPOINT_TEMPLATE = config('EXTERNAL_API_ENDPOINT_TEMPLATE', default='{base_url}/crm/v3/objects/{source_type}')
EXTERNAL_API_RECORDS_KEY = config('EXTERNAL_API_RECORDS_KEY', default='results')
EXTERNAL_API_CURSOR_PARAM = config('EXTERNAL_API_CURSOR_PARAM', default='after')
EXTERNAL_API_PAGE_SIZE = config('EXTERNAL_API_PAGE_SIZE', default=100, cast=int)
# Trade-off: Raising MAX_PAGES to 2000 allows fetching up to 200,000 records with standard page size (100).
# Trade-off note: Higher values risk prolonged execution time and memory consumption during large syncs,
# while lower values protect against runaway loops and upstream billing exhaustion.
EXTERNAL_API_MAX_PAGES = config('EXTERNAL_API_MAX_PAGES', default=2000, cast=int)
EXTERNAL_API_ON_PAGE_LIMIT = config('EXTERNAL_API_ON_PAGE_LIMIT', default='fail')  # 'fail' or 'truncate'
EXTERNAL_API_MAX_RATE_LIMIT_RETRIES = config('EXTERNAL_API_MAX_RATE_LIMIT_RETRIES', default=5, cast=int)
EXTERNAL_API_MAX_RETRY_AFTER_SECONDS = config('EXTERNAL_API_MAX_RETRY_AFTER_SECONDS', default=60.0, cast=float)
EXTERNAL_API_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS = config('EXTERNAL_API_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS', default=300.0, cast=float)

# Allowed destination hosts for external data extraction pagination and redirects.
# Default empty list means only the host specified in EXTERNAL_API_BASE_URL is permitted.
EXTERNAL_API_ALLOWED_HOSTS = config(
    'EXTERNAL_API_ALLOWED_HOSTS',
    default='',
    cast=Csv()
)

# Loopback destination permission for local mock server development.
# Strictly prohibited when DEBUG=False (production).
EXTERNAL_API_ALLOW_LOOPBACK = config('EXTERNAL_API_ALLOW_LOOPBACK', default=False, cast=parse_bool)
if not DEBUG and EXTERNAL_API_ALLOW_LOOPBACK:
    raise ImproperlyConfigured("EXTERNAL_API_ALLOW_LOOPBACK is strictly prohibited in production (DEBUG=False).")

# Validate external API base URL configuration
if EXTERNAL_API_BASE_URL:
    _parsed_base = urllib.parse.urlparse(EXTERNAL_API_BASE_URL)
    if not _parsed_base.scheme or not _parsed_base.netloc:
        raise ImproperlyConfigured(
            f"EXTERNAL_API_BASE_URL must be a valid absolute URL with scheme and host. Got: '{EXTERNAL_API_BASE_URL}'"
        )
    if not DEBUG and _parsed_base.scheme != 'https':
        raise ImproperlyConfigured("EXTERNAL_API_BASE_URL must use HTTPS in production (DEBUG=False).")

# Production Security Headers (active when DEBUG=False)
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=True, cast=parse_bool)
    CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=True, cast=parse_bool)
    SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=parse_bool)
    SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True, cast=parse_bool)
    SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=True, cast=parse_bool)

# Logging Configuration
LOG_LEVEL = config('LOG_LEVEL', default='INFO')
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{asctime}] {levelname} [{name}:{lineno}] {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': LOG_LEVEL,
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': LOG_LEVEL,
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'apps': {
            'handlers': ['console'],
            'level': LOG_LEVEL,
            'propagate': False,
        },
    },
}
