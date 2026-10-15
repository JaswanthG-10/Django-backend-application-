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
        raw_base_url = (base_url or getattr(settings, 'EXTERNAL_API_BASE_URL', 'https://jsonplaceholder.typicode.com')).rstrip('/')
        if not getattr(settings, 'DEBUG', True) and raw_base_url.lower().startswith('http://'):
            raise ExternalServiceError("External provider base URL must use HTTPS in production.")
        self.base_url = raw_base_url
        self.timeout = timeout or getattr(settings, 'EXTERNAL_API_TIMEOUT', 10)
        self.max_retries = max_retries
        self.session = session or requests.Session()

    @staticmethod
    def _mask_token(token: str) -> str:
        """Safely masks authorization tokens for structured logging."""
        if not token:
            return '***'
        cleaned = token.strip()
        if len(cleaned) <= 8:
            return '***'
        return f"{cleaned[:3]}***{cleaned[-4:]}"

    @staticmethod
    def _is_public_demo_endpoint(url: str) -> bool:
        """Determines if target URL belongs to a public demo/mock sandbox service.
        
        Prevents leaking confidential customer or service authorization tokens across
        the public internet to unauthenticated third-party testing sandboxes.
        """
        lowered = url.lower()
        public_demo_domains = (
            'jsonplaceholder.typicode.com',
            'reqres.in',
            'httpbin.org',
            'mocky.io',
            'dummyjson.com',
        )
        return any(domain in lowered for domain in public_demo_domains)

    def _determine_url(self, source_type: str) -> str:
        """Determines target URL based on configured provider endpoint template."""
        template = getattr(settings, 'EXTERNAL_API_ENDPOINT_TEMPLATE', None)
        if template:
            return template.format(base_url=self.base_url, source_type=source_type)

        # HubSpot CRM object endpoints
        if 'hubapi' in self.base_url.lower():
            endpoint_name = 'contacts' if source_type in ('crm', 'contacts') else source_type
            return f"{self.base_url}/crm/v3/objects/{endpoint_name}"

        # JSONPlaceholder public testing mock
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
        
        Supports configurable provider endpoints, cursor/offset/page pagination,
        exponential backoff retries, and data normalization.
        """
        if not api_token or not api_token.strip():
            raise ExternalServiceAuthError("API token must not be empty.")

        url = self._determine_url(source_type)
        headers = {
            'Accept': 'application/json',
            'User-Agent': 'GreenTree-DataExtractionClient/1.0',
        }

        # Prevent tokens from being sent to public demo/mock sandbox endpoints
        if self._is_public_demo_endpoint(url):
            logger.info(
                "Target URL '%s' is a public demo/sandbox endpoint. "
                "Omitted Authorization header to prevent token leakage.",
                url
            )
        else:
            headers['Authorization'] = f"Bearer {api_token.strip()}"

        masked = self._mask_token(api_token)
        logger.info(
            "Initiating external extraction | url=%s source=%s token=%s params=%s",
            url, source_type, masked, filters
        )

        all_records: List[Dict[str, Any]] = []
        seen_ids = set()
        next_cursor = None
        next_url = url
        pagination_type = getattr(settings, 'EXTERNAL_API_PAGINATION_TYPE', 'auto').lower()

        for page in range(1, max_pages + 1):
            page_params = dict(filters or {})
            current_req_url = next_url or url

            # Apply pagination parameters according to configured or detected pagination style
            if pagination_type == 'cursor' or (pagination_type == 'auto' and next_cursor):
                cursor_param = getattr(settings, 'EXTERNAL_API_CURSOR_PARAM', 'after')
                page_params[cursor_param] = next_cursor
                page_params['limit'] = page_size
            elif pagination_type == 'offset_limit':
                page_params['offset'] = (page - 1) * page_size
                page_params['limit'] = page_size
            else:
                # Default page-number pagination
                page_params['page'] = page
                page_params['limit'] = page_size
                page_params['_page'] = page
                page_params['_limit'] = page_size

            response = self._execute_request_with_retries(current_req_url, headers, page_params)
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

            # Extract records from response payload
            configured_key = getattr(settings, 'EXTERNAL_API_RECORDS_KEY', None)
            if configured_key and isinstance(payload, dict) and configured_key in payload:
                page_items = payload[configured_key]
            elif isinstance(payload, dict):
                page_items = (
                    payload.get('results') or
                    payload.get('data') or
                    payload.get('records') or
                    payload.get('items') or
                    []
                )
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

            # Detect cursor or next page link for subsequent requests
            next_cursor = None
            if isinstance(payload, dict):
                # HubSpot style: {"paging": {"next": {"after": "..."}}}
                paging = payload.get('paging')
                if isinstance(paging, dict):
                    next_obj = paging.get('next')
                    if isinstance(next_obj, dict):
                        next_cursor = next_obj.get('after')
                # Generic cursor style: {"next_cursor": "..."} or {"cursor": "..."}
                if not next_cursor:
                    next_cursor = payload.get('next_cursor') or payload.get('cursor')

                # Next URL style (RFC 5988 / REST next link)
                links = getattr(response, 'links', None)
                link_next = links.get('next', {}).get('url') if isinstance(links, dict) else None
                next_url = payload.get('next') if isinstance(payload.get('next'), str) else link_next

            # If cursor pagination is active and no next cursor was found, terminate
            if pagination_type == 'cursor' and not next_cursor:
                break

            # If no new records were extracted on this page, stop to prevent infinite loops
            if new_in_page == 0:
                break

            # If explicit next cursor or next link is available, continue to next page;
            # otherwise, terminate if fewer than page_size items were returned.
            has_explicit_next = bool(next_cursor) or bool(next_url and next_url != current_req_url)
            if not has_explicit_next and len(page_items) < page_size:
                break

        logger.info("Successfully fetched %d records across pages from external provider.", len(all_records))
        return all_records
