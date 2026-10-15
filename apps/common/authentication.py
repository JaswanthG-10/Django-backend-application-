"""API Key and Request Signing Authentication for service endpoints."""
import time
import hmac
import hashlib
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
    """DRF Authentication backend verifying service API key and optional/enforced request signing.
    
    Accepts:
    - X-API-KEY header
    - Authorization: Api-Key <key>
    - Authorization: Bearer <key>
    - Cryptographic Request Signing: X-Signature (HMAC-SHA256) and X-Timestamp
    """

    def authenticate_header(self, request):
        return 'ApiKey realm="api"'

    def authenticate(self, request):
        expected_key = getattr(settings, 'API_KEY_SECRET', None)
        if not expected_key or not str(expected_key).strip():
            logger.error("API_KEY_SECRET is not configured on the server. Authentication failed (fail-closed).")
            raise AuthenticationFailed("API key authentication is not configured on the server.")

        require_signing = getattr(settings, 'API_REQUIRE_REQUEST_SIGNING', False)
        provided_sig = request.headers.get('X-Signature') or request.META.get('HTTP_X_SIGNATURE')
        provided_ts = request.headers.get('X-Timestamp') or request.META.get('HTTP_X_TIMESTAMP')

        # If request signing is enforced or provided, verify cryptographic signature
        if require_signing or provided_sig:
            if not provided_sig or not provided_ts:
                raise AuthenticationFailed("Request signing required: X-Signature and X-Timestamp headers are mandatory.")

            try:
                ts_float = float(provided_ts)
            except (ValueError, TypeError):
                raise AuthenticationFailed("Invalid X-Timestamp header format. Must be numeric timestamp.")

            tolerance = getattr(settings, 'API_SIGNING_TOLERANCE_SECONDS', 300)
            if abs(time.time() - ts_float) > tolerance:
                raise AuthenticationFailed("Request signature timestamp expired or outside permitted tolerance.")

            # Compute body SHA-256 hash
            body_bytes = request.body if hasattr(request, 'body') and request.body else b''
            body_hash = hashlib.sha256(body_bytes).hexdigest()

            # Canonical signing payload: <timestamp>.<method>.<path>.<body_hash>
            canonical_payload = f"{provided_ts}.{request.method.upper()}.{request.path}.{body_hash}"
            expected_sig = hmac.new(
                expected_key.encode('utf-8'),
                canonical_payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(provided_sig.lower().strip(), expected_sig.lower().strip()):
                raise AuthenticationFailed("Invalid request signature.")

            return (ServiceApiKeyUser(), None)

        # Standard API Key Header Verification
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

