"""Permission classes for securing API endpoints."""
import hmac
import logging
from django.conf import settings
from rest_framework.permissions import BasePermission
from rest_framework.exceptions import AuthenticationFailed, NotAuthenticated

logger = logging.getLogger('apps.common.permissions')


class HasServiceApiKey(BasePermission):
    """Requires callers to provide a valid API key in request headers."""

    message = "Valid service API key is required to access this endpoint."

    def has_permission(self, request, view):
        if request.user and getattr(request.user, 'is_authenticated', False):
            return True

        expected_key = getattr(settings, 'API_KEY_SECRET', None)
        if not expected_key or not str(expected_key).strip():
            logger.error("API_KEY_SECRET is not configured on the server. Rejecting request (fail-closed).")
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
            raise NotAuthenticated(self.message)

        if not hmac.compare_digest(provided_key.strip(), expected_key.strip()):
            raise AuthenticationFailed("Invalid service API key.")

        return True
