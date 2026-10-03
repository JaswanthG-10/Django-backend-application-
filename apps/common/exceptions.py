"""Standardized exception handling and domain exceptions."""
import logging
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status
from rest_framework.exceptions import APIException

logger = logging.getLogger('apps.common.exceptions')


class BaseDomainException(APIException):
    """Base exception for application business and domain logic errors."""
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = 'DOMAIN_ERROR'
    default_detail = 'A business rule violation occurred.'

    def __init__(self, message=None, code=None, details=None, status_code=None):
        if status_code is not None:
            self.status_code = status_code
        self.code = code or self.default_code
        self.message = message or self.default_detail
        self.details = details or {}
        super().__init__(detail=self.message, code=self.code)


class InvalidStateTransitionError(BaseDomainException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'INVALID_STATE_TRANSITION'
    default_detail = 'The requested state transition is invalid for this job.'


class JobIncompleteError(BaseDomainException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'JOB_NOT_COMPLETED'
    default_detail = 'Extraction job is not completed yet. Results are only available for completed jobs.'


class DuplicateJobConflictError(BaseDomainException):
    status_code = status.HTTP_409_CONFLICT
    default_code = 'DUPLICATE_JOB_ACTIVE'
    default_detail = 'An active extraction job with the same parameters is already pending or in progress.'


class ResourceNotFoundError(BaseDomainException):
    status_code = status.HTTP_404_NOT_FOUND
    default_code = 'RESOURCE_NOT_FOUND'
    default_detail = 'The requested resource was not found.'


class ExternalServiceError(BaseDomainException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = 'EXTERNAL_SERVICE_ERROR'
    default_detail = 'Failed to communicate with external data source service.'


class ExternalServiceTimeoutError(BaseDomainException):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    default_code = 'EXTERNAL_SERVICE_TIMEOUT'
    default_detail = 'Request to external data source timed out.'


class ExternalServiceAuthError(BaseDomainException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_code = 'EXTERNAL_SERVICE_AUTH_ERROR'
    default_detail = 'External service rejected provided authentication token.'


class RateLimitExceededError(BaseDomainException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = 'RATE_LIMIT_EXCEEDED'
    default_detail = 'Rate limit exceeded for extraction requests. Please retry later.'


def custom_exception_handler(exc, context):
    """Custom exception handler guaranteeing consistent error response format across the API.
    
    Response format:
    {
        "error": {
            "code": "ERROR_CODE",
            "message": "Human readable description.",
            "details": {...}
        }
    }
    """
    # Call REST framework's default exception handler first
    response = exception_handler(exc, context)

    # Domain-specific BaseDomainException handling
    if isinstance(exc, BaseDomainException):
        payload = {
            'error': {
                'code': exc.code,
                'message': str(exc.message),
                'details': exc.details or {}
            }
        }
        logger.warning(
            "Domain exception: [%s] %s | Context: %s",
            exc.code,
            exc.message,
            context.get('view', '')
        )
        return Response(payload, status=exc.status_code)

    # Handle standard DRF exceptions
    if response is not None:
        code = 'API_ERROR'
        message = 'An error occurred while processing the request.'
        details = response.data

        if response.status_code == status.HTTP_400_BAD_REQUEST:
            code = 'VALIDATION_ERROR'
            message = 'Invalid request data provided.'
        elif response.status_code == status.HTTP_401_UNAUTHORIZED:
            code = 'UNAUTHORIZED'
            message = 'Authentication credentials were not provided or are invalid.'
        elif response.status_code == status.HTTP_403_FORBIDDEN:
            code = 'FORBIDDEN'
            message = 'You do not have permission to perform this action.'
        elif response.status_code == status.HTTP_404_NOT_FOUND:
            code = 'NOT_FOUND'
            message = 'The requested resource was not found.'
        elif response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
            code = 'METHOD_NOT_ALLOWED'
            message = f"Method '{context.get('request').method if context.get('request') else ''}' not allowed."
        elif response.status_code == status.HTTP_409_CONFLICT:
            code = 'CONFLICT'
            message = 'The request conflicts with the current resource state.'

        if isinstance(details, dict):
            # If 'detail' is present in standard DRF errors
            if 'detail' in details:
                message = str(details.pop('detail'))

        payload = {
            'error': {
                'code': code,
                'message': message,
                'details': details
            }
        }
        return Response(payload, status=response.status_code)

    # Unhandled server exceptions (500)
    logger.exception("Unhandled server exception in API: %s", exc)
    return Response({
        'error': {
            'code': 'INTERNAL_SERVER_ERROR',
            'message': 'An unexpected server error occurred. Please contact support.'
        }
    }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
