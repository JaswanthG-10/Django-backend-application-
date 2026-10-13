"""Client for communicating with external third-party data providers."""
import time
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
    """Production-ready HTTP client for extracting data from external APIs.
    
    Includes:
    - Multi-page pagination traversal
    - Automatic exponential backoff retries on HTTP 429 and 5xx server errors
    - Respect for Retry-After headers
    - Safe token masking in logs
    - Flexible normalization across real public sources (e.g. JSONPlaceholder, ReqRes, HubSpot)
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: Optional[int] = None,
        max_retries: int = 3,
        session: Optional[requests.Session] = None
    ):
        self.base_url = (base_url or getattr(settings, 'EXTERNAL_API_BASE_URL', 'https://jsonplaceholder.typicode.com')).rstrip('/')
        self.timeout = timeout or getattr(settings, 'EXTERNAL_API_TIMEOUT', 10)
        self.max_retries = max_retries
        self.session = session or requests.Session()

    @staticmethod
    def _mask_token(token: str) -> str:
        """Safely masks authorization tokens for structured logging."""
        if not token or len(token) <= 8:
            return '***'
        return f"{token[:3]}***{token[-4:]}"

    def _determine_url(self, source_type: str) -> str:
        """Determines target URL based on provider base URL and source type."""
        # Check if base_url is a known public provider like jsonplaceholder
        if 'jsonplaceholder' in self.base_url.lower():
            if source_type in ('crm', 'contacts', 'users'):
                return f"{self.base_url}/users"
            return f"{self.base_url}/{source_type}"
        # Standard CRM API pattern
        return f"{self.base_url}/{source_type}/records"

    def _execute_request_with_retries(
        self,
        url: str,
        headers: Dict[str, str],
        params: Dict[str, Any]
    ) -> requests.Response:
        """Executes GET request with exponential backoff on 429 and 5xx errors."""
        attempt = 0
        backoff_delay = 0.2

        while True:
            attempt += 1
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout
                )
            except requests.exceptions.Timeout as exc:
                if attempt <= self.max_retries:
                    logger.warning("Request timed out on attempt %d/%d; retrying in %.2fs | url=%s", attempt, self.max_retries, backoff_delay, url)
                    time.sleep(backoff_delay)
                    backoff_delay *= 2
                    continue
                logger.error("External service timeout after %d attempts | url=%s", attempt, url)
                raise ExternalServiceTimeoutError(
                    message=f"External data source timed out after {self.timeout} seconds.",
                    details={'url': url, 'timeout_seconds': self.timeout}
                ) from exc
            except requests.exceptions.RequestException as exc:
                logger.error("External connection error | url=%s error=%s", url, str(exc))
                raise ExternalServiceError(
                    message="Unable to connect to external data extraction service.",
                    details={'url': url, 'reason': 'Connection failure'}
                ) from exc

            status_code = response.status_code

            # Handle 429 Rate Limit with Retry-After if retries remain
            if status_code == 429 and attempt <= self.max_retries:
                retry_after_header = response.headers.get('Retry-After')
                try:
                    retry_seconds = float(retry_after_header) if retry_after_header else backoff_delay
                except (ValueError, TypeError):
                    retry_seconds = backoff_delay
                retry_seconds = min(retry_seconds, 2.0)
                logger.warning("Hit 429 Rate Limit on attempt %d/%d; sleeping %.2fs before retry | url=%s", attempt, self.max_retries, retry_seconds, url)
                time.sleep(retry_seconds)
                backoff_delay *= 2
                continue

            # Handle 5xx Server Errors if retries remain
            if status_code >= 500 and attempt <= self.max_retries:
                logger.warning("Hit HTTP %d on attempt %d/%d; retrying in %.2fs | url=%s", status_code, attempt, self.max_retries, backoff_delay, url)
                time.sleep(backoff_delay)
                backoff_delay *= 2
                continue

            return response

    def _normalize_raw_record(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Normalizes diverse third-party schemas into standard entity records."""
        rec_id = str(raw.get('id') or raw.get('id_from_service') or raw.get('uuid') or '')
        email = raw.get('email') or (raw.get('properties', {}).get('email') if isinstance(raw.get('properties'), dict) else None)

        first_name = raw.get('first_name', '')
        last_name = raw.get('last_name', '')

        # Fallback if provider returns a single 'name' field (e.g. JSONPlaceholder)
        if not first_name and not last_name and raw.get('name'):
            parts = str(raw['name']).strip().split(' ', 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''

        custom = raw.get('custom_fields') or raw.get('properties') or {}
        if not custom:
            # Collect other attributes as custom fields
            custom = {k: v for k, v in raw.items() if k not in ('id', 'id_from_service', 'email', 'first_name', 'last_name', 'name')}

        return {
            'id': rec_id,
            'id_from_service': rec_id,
            'email': email,
            'first_name': str(first_name)[:150],
            'last_name': str(last_name)[:150],
            'custom_fields': custom if isinstance(custom, dict) else {'raw': custom}
        }

    def extract_data(
        self,
        api_token: str,
        source_type: str = 'crm',
        filters: Optional[Dict[str, Any]] = None,
        max_pages: int = 5,
        page_size: int = 20
    ) -> List[Dict[str, Any]]:
        """Fetches and pages through records from external provider endpoint.
        
        Supports pagination across pages, exponential backoff retries, and data normalization.
        """
        if not api_token or not api_token.strip():
            raise ExternalServiceAuthError("API token must not be empty.")

        url = self._determine_url(source_type)
        headers = {
            'Authorization': f"Bearer {api_token.strip()}",
            'Accept': 'application/json',
            'User-Agent': 'GreenTree-DataExtractionClient/1.0',
        }

        masked = self._mask_token(api_token)
        logger.info(
            "Initiating external extraction | url=%s source=%s token=%s params=%s",
            url, source_type, masked, filters
        )

        all_records: List[Dict[str, Any]] = []
        seen_ids = set()

        for page in range(1, max_pages + 1):
            page_params = dict(filters or {})
            # Include standard pagination query parameters
            page_params['page'] = page
            page_params['limit'] = page_size
            page_params['_page'] = page
            page_params['_limit'] = page_size

            response = self._execute_request_with_retries(url, headers, page_params)
            status_code = response.status_code

            if status_code in (401, 403):
                logger.warning("External service authorization failed | status=%d token=%s", status_code, masked)
                raise ExternalServiceAuthError(
                    message="Authentication with external service provider failed. Check your API token.",
                    details={'status_code': status_code}
                )

            if status_code == 404:
                logger.warning("External resource not found | url=%s", url)
                raise ResourceNotFoundError(
                    message="Requested data source resource not found at external provider.",
                    details={'url': url, 'status_code': 404}
                )

            if status_code == 429:
                logger.warning("External service rate limit hit after retries | url=%s", url)
                raise RateLimitExceededError(
                    message="External provider rate limit exceeded. Please back off and retry.",
                    details={'status_code': 429}
                )

            if status_code >= 500:
                logger.error("External service server error | status=%d url=%s", status_code, url)
                raise ExternalServiceError(
                    message=f"External service encountered a server error (HTTP {status_code}).",
                    details={'status_code': status_code}
                )

            if not (200 <= status_code < 300):
                logger.error("External service unexpected HTTP status | status=%d", status_code)
                raise ExternalServiceError(
                    message=f"External service returned unexpected status code {status_code}.",
                    details={'status_code': status_code}
                )

            try:
                payload = response.json()
            except ValueError as exc:
                logger.error("External service returned malformed non-JSON payload | text=%s", response.text[:200])
                raise ExternalServiceError(
                    message="External service returned a malformed non-JSON response.",
                    details={'reason': 'Malformed JSON'}
                ) from exc

            # Normalize container structure
            if isinstance(payload, dict):
                page_items = payload.get('data') or payload.get('results') or payload.get('records') or []
            elif isinstance(payload, list):
                page_items = payload
            else:
                page_items = []

            if not isinstance(page_items, list):
                raise ExternalServiceError("External service returned unexpected records structure.")

            if not page_items:
                # No more records on this page, finish paging
                break

            new_in_page = 0
            for item in page_items:
                if not isinstance(item, dict):
                    continue
                norm = self._normalize_raw_record(item)
                rec_id = norm['id_from_service']
                if rec_id and rec_id not in seen_ids:
                    seen_ids.add(rec_id)
                    all_records.append(norm)
                    new_in_page += 1

            # If fewer than page_size items returned, or no new records found, end pagination
            if len(page_items) < page_size or new_in_page == 0:
                break

        logger.info("Successfully fetched %d records across pages from external provider.", len(all_records))
        return all_records
