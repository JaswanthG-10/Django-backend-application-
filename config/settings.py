"""Django settings for Data Extraction Service.

Generated for GreenTree / Springer Capital backend development practical assignment.
"""
from pathlib import Path
from django.core.exceptions import ImproperlyConfigured
from decouple import config, Csv
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Security Settings
DEBUG = config('DEBUG', default=True, cast=bool)

# In development/test mode, fall back to a clearly designated dummy key if unset.
# In production (DEBUG=False), require an explicit SECRET_KEY.
_DEFAULT_DEV_SECRET = 'django-insecure-dev-fallback-key-for-local-testing-only-991823719238'
SECRET_KEY = config('SECRET_KEY', default=_DEFAULT_DEV_SECRET if DEBUG else '')
if not SECRET_KEY or (not DEBUG and SECRET_KEY == _DEFAULT_DEV_SECRET):
    raise ImproperlyConfigured("SECRET_KEY environment variable must be set securely when running with DEBUG=False.")

ALLOWED_HOSTS = config(
    'ALLOWED_HOSTS',
    default='localhost,127.0.0.1,0.0.0.0,testserver' if DEBUG else '',
    cast=Csv()
)

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
DATABASE_URL = config('DATABASE_URL', default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")
DATABASES = {
    'default': dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600 if not DATABASE_URL.startswith('sqlite') else 0,
    )
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
# In production (DEBUG=False), require an explicit API_KEY_SECRET.
_DEFAULT_DEV_API_KEY = 'dev-local-api-key-secret-do-not-use-in-production'
API_KEY_SECRET = config('API_KEY_SECRET', default=_DEFAULT_DEV_API_KEY if DEBUG else '')
if not API_KEY_SECRET and not DEBUG:
    raise ImproperlyConfigured("API_KEY_SECRET environment variable must be set when running with DEBUG=False.")

# Asynchronous background execution (worker / thread runner)
EXTRACTION_ASYNC_EXECUTION = config('EXTRACTION_ASYNC_EXECUTION', default=True, cast=bool)
EXTRACTION_MAX_WORKERS = config('EXTRACTION_MAX_WORKERS', default=4, cast=int)

# External Extraction Service Client Configuration
# Defaults to jsonplaceholder.typicode.com as a live accessible data source, or any CRM URL
EXTERNAL_API_BASE_URL = config('EXTERNAL_API_BASE_URL', default='https://jsonplaceholder.typicode.com')
EXTERNAL_API_TIMEOUT = config('EXTERNAL_API_TIMEOUT', default=10, cast=int)
EXTERNAL_API_DEFAULT_TOKEN = config('EXTERNAL_API_DEFAULT_TOKEN', default='test-sample-service-token')

# Production Security Headers (active when DEBUG=False)
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=True, cast=bool)
    CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=True, cast=bool)
    SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
    SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True, cast=bool)
    SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=True, cast=bool)

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
