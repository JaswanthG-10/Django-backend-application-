"""API Key Authentication for service endpoints."""
import hmac
import logging
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from drf_spectacular.extensions import OpenApiAuthenticationExtension

logger = logging.getLogger('apps.common.authentication')


class ServiceApiKeyUser:
    """Lightweight principal representing an authenticated API service caller."""
    is_authenticated = True
    is_anonymous = False
    is_active = True

    def __str__(self):
        return "ServiceApiKeyUser"


class ApiKeyAuthentication(BaseAuthentication):
    """DRF Authentication backend verifying the service API key.
    
    Accepts:
    - X-API-KEY header
    - Authorization: Api-Key <key>
    - Authorization: Bearer <key>
    """

    def authenticate_header(self, request):
        return 'ApiKey realm="api"'

    def authenticate(self, request):
        expected_key = getattr(settings, 'API_KEY_SECRET', None)
        if not expected_key or not str(expected_key).strip():
            logger.error("API_KEY_SECRET is not configured on the server. Authentication failed (fail-closed).")
            raise AuthenticationFailed("API key authentication is not configured on the server.")

        provided_key = None
        if 'HTTP_X_API_KEY' in request.META:
            provided_key = request.META['HTTP_X_API_KEY']
        elif 'X-API-KEY' in request.headers:
            provided_key = request.headers['X-API-KEY']
        elif 'Authorization' in request.headers:
            auth_val = request.headers['Authorization']
            if auth_val.startswith('Api-Key '):
                provided_key = auth_val[8:].strip()
            elif auth_val.startswith('Bearer '):
                token_val = auth_val[7:].strip()
                if hmac.compare_digest(token_val, expected_key):
                    provided_key = token_val

        if not provided_key:
            return None

        if not hmac.compare_digest(provided_key.strip(), expected_key.strip()):
            raise AuthenticationFailed("Invalid service API key.")

        return (ServiceApiKeyUser(), None)


class ApiKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    """Registers ApiKeyAuthentication with drf-spectacular OpenAPI generator."""
    target_class = 'apps.common.authentication.ApiKeyAuthentication'
    name = 'ApiKeyAuth'

    def get_security_definition(self, auto_schema):
        return {
            'type': 'apiKey',
            'in': 'header',
            'name': 'X-API-KEY',
            'description': (
                'Service API key passed via X-API-KEY header, '
                'Authorization: Api-Key <key>, or Authorization: Bearer <key>'
            ),
        }

