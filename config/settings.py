"""Django settings for Data Extraction Service.

Generated for GreenTree / Springer Capital backend development practical assignment.
"""
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


# Security Settings: Defaults to False in production
DEBUG = config('DEBUG', default=False, cast=parse_bool)

# In development/test mode, fall back to a clearly designated dummy key if unset.
# In production (DEBUG=False), require an explicit, strong SECRET_KEY.
_DEFAULT_DEV_SECRET = 'django-insecure-dev-fallback-key-for-local-testing-only-991823719238'
SECRET_KEY = config('SECRET_KEY', default=_DEFAULT_DEV_SECRET if DEBUG else '')
if not DEBUG:
    if not SECRET_KEY:
        raise ImproperlyConfigured(
            "SECRET_KEY environment variable must be set securely when running with DEBUG=False. "
            "Generate one with: python -c \"from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())\""
        )
    if SECRET_KEY == _DEFAULT_DEV_SECRET or SECRET_KEY.startswith('django-insecure-') or len(SECRET_KEY) < 32:
        raise ImproperlyConfigured(
            "A strong, unique SECRET_KEY of at least 32 characters must be configured in production (DEBUG=False). "
            "Insecure default or fallback keys are strictly prohibited. "
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
_PROHIBITED_PROD_API_KEYS = {
    _DEFAULT_DEV_API_KEY,
    'dev-testing-service-api-key-381923',
    'test-api-key',
    'test-secret-key',
    'replace-with-a-secure-random-api-key-in-production',
    'cbHiMJjDLSWmMxIzGZ8YWWb-qRF7cTsT61am2emYM28',
}
API_KEY_SECRET = config('API_KEY_SECRET', default=_DEFAULT_DEV_API_KEY if DEBUG else '')
if not DEBUG and (not API_KEY_SECRET or API_KEY_SECRET in _PROHIBITED_PROD_API_KEYS):
    raise ImproperlyConfigured(
        "A unique, production-grade API_KEY_SECRET must be configured when running with DEBUG=False. "
        "Shared development credentials and placeholder keys are strictly prohibited in production. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )

# Request Signing Security (HMAC-SHA256 Payload & Timestamp Verification)
API_REQUIRE_REQUEST_SIGNING = config('API_REQUIRE_REQUEST_SIGNING', default=False, cast=bool)
API_SIGNING_TOLERANCE_SECONDS = config('API_SIGNING_TOLERANCE_SECONDS', default=300, cast=int)

# Field-Level Symmetric Encryption Key for sensitive tokens at rest (Fernet key)
# If unset in development/test mode, deterministically derived from SECRET_KEY.
# In production (DEBUG=False), require an explicit valid 32-byte Fernet key.
FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default=None)
if not FIELD_ENCRYPTION_KEY and not DEBUG:
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEY environment variable must be set securely when running with DEBUG=False. "
        "Generate a 32-byte Fernet key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
if FIELD_ENCRYPTION_KEY:
    try:
        from cryptography.fernet import Fernet
        Fernet(FIELD_ENCRYPTION_KEY.encode('utf-8') if isinstance(FIELD_ENCRYPTION_KEY, str) else FIELD_ENCRYPTION_KEY)
    except Exception as exc:
        raise ImproperlyConfigured(
            "Invalid FIELD_ENCRYPTION_KEY. Must be a valid 32-byte url-safe base64-encoded Fernet key. "
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
