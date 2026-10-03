"""Client for communicating with external third-party data providers."""
import logging
from typing import List, Dict, Any, Optional
import requests
from django.conf import settings
from apps.common.exceptions import (
    ExternalServiceError,
    ExternalServiceTimeoutError,
    ExternalServiceAuthError,
    ResourceNotFoundError,
    RateLimitExceededError,
)

logger = logging.getLogger('apps.extraction.clients')


class ThirdPartyExtractionClient:
    """Production-ready HTTP client for extracting data from external third-party APIs.
    
    Includes timeout handling, token masking in logs, HTTP status mapping to domain exceptions,
    and resilience against network anomalies or malformed payloads.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        session: Optional[requests.Session] = None
    ):
        self.base_url = (base_url or getattr(settings, 'EXTERNAL_API_BASE_URL', 'https://api.thirdparty-crm.com/v1')).rstrip('/')
        self.timeout = timeout or getattr(settings, 'EXTERNAL_API_TIMEOUT', 10)
        self.session = session or requests.Session()

    @staticmethod
    def _mask_token(token: str) -> str:
        """Safely masks authorization tokens for structured logging."""
        if not token or len(token) <= 8:
            return '***'
        return f"{token[:3]}***{token[-4:]}"

    def extract_data(
        self,
        api_token: str,
        source_type: str = 'crm',
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Fetches raw records from external provider endpoint.
        
        Args:
            api_token: The client's bearer token or API key.
            source_type: The data source domain (crm, contacts, leads).
            filters: Optional dictionary of query parameters.
            
        Returns:
            A list of dictionary records containing id_from_service, email, names, and custom attributes.
            
        Raises:
            ExternalServiceAuthError: If external service returns 401 or 403.
            ResourceNotFoundError: If external service returns 404.
            RateLimitExceededError: If external service returns 429.
            ExternalServiceTimeoutError: If the HTTP request times out.
            ExternalServiceError: If communication fails, returns 5xx, or yields invalid JSON.
        """
        if not api_token or not api_token.strip():
            raise ExternalServiceAuthError("API token must not be empty.")

        url = f"{self.base_url}/{source_type}/records"
        headers = {
            'Authorization': f"Bearer {api_token.strip()}",
            'Accept': 'application/json',
            'User-Agent': 'GreenTree-DataExtractionClient/1.0',
        }
        params = filters or {}

        masked = self._mask_token(api_token)
        logger.info(
            "Initiating external extraction | url=%s source=%s token=%s params=%s",
            url, source_type, masked, params
        )

        try:
            response = self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=self.timeout
            )
        except requests.exceptions.Timeout as exc:
            logger.error("External service timeout after %ds | url=%s", self.timeout, url)
            raise ExternalServiceTimeoutError(
                message=f"External data source timed out after {self.timeout} seconds.",
                details={'url': url, 'timeout_seconds': self.timeout}
            ) from exc
        except requests.exceptions.RequestException as exc:
            logger.error("External connection error | url=%s error=%s", url, str(exc))
            raise ExternalServiceError(
                message="Unable to connect to external data extraction service.",
                details={'url': url, 'reason': str(exc)}
            ) from exc

        # Handle HTTP Status Codes
        status_code = response.status_code
        if status_code in (401, 403):
            logger.warning("External service authorization failed | status=%d token=%s", status_code, masked)
            raise ExternalServiceAuthError(
                message="Authentication with external service provider failed. Check your API token.",
                details={'status_code': status_code, 'provider_response': response.text[:200]}
            )

        if status_code == 404:
            logger.warning("External resource not found | url=%s", url)
            raise ResourceNotFoundError(
                message="Requested data source resource not found at external provider.",
                details={'url': url, 'status_code': 404}
            )

        if status_code == 429:
            logger.warning("External service rate limit hit | url=%s", url)
            raise RateLimitExceededError(
                message="External provider rate limit exceeded. Please back off and retry.",
                details={'status_code': 429}
            )

        if status_code >= 500:
            logger.error("External service 5xx server error | status=%d url=%s", status_code, url)
            raise ExternalServiceError(
                message=f"External service encountered a server error (HTTP {status_code}).",
                details={'status_code': status_code, 'provider_response': response.text[:200]}
            )

        if not (200 <= status_code < 300):
            logger.error("External service unexpected HTTP status | status=%d", status_code)
            raise ExternalServiceError(
                message=f"External service returned unexpected status code {status_code}.",
                details={'status_code': status_code, 'provider_response': response.text[:200]}
            )

        # Parse JSON response
        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("External service returned malformed non-JSON payload | text=%s", response.text[:200])
            raise ExternalServiceError(
                message="External service returned a malformed non-JSON response.",
                details={'provider_response': response.text[:200]}
            ) from exc

        # Normalize data format (e.g. data might be in payload['data'] or payload['results'] or direct list)
        if isinstance(payload, dict):
            records = payload.get('data') or payload.get('results') or payload.get('records') or []
        elif isinstance(payload, list):
            records = payload
        else:
            records = []

        if not isinstance(records, list):
            raise ExternalServiceError("External service returned unexpected records structure.")

        logger.info("Successfully fetched %d records from external provider.", len(records))
        return records
