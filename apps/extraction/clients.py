"""Client for communicating with external third-party data providers."""
import datetime
import email.utils
import logging
import random
import re
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, NamedTuple

import requests
from django.conf import settings

from apps.common.exceptions import (
    ExternalServiceAuthError,
    ExternalServiceError,
    ExternalServiceTimeoutError,
    PageLimitExceededError,
    RateLimitExceededError,
    ResourceNotFoundError,
)

logger = logging.getLogger('apps.extraction.clients')


class ExtractionResult(NamedTuple):
    """Result object encapsulating extracted records and pagination safety status."""
    records: list[dict[str, Any]]
    truncated: bool = False


def _sleep_with_heartbeat(
    seconds: float,
    heartbeat_callback: Callable[[], None] | None = None,
    chunk_size: float = 30.0
) -> None:
    """Sleeps for given duration in chunks, invoking heartbeat callback between chunks."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, chunk_size)
        time.sleep(chunk)
        remaining -= chunk
        if heartbeat_callback and remaining > 0:
            try:
                heartbeat_callback()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Heartbeat callback exception ignored during retry sleep: %s", exc)


def parse_retry_after(header_val: str | None) -> float | None:
    """Parses Retry-After header into delay in seconds.
    
    Supports both standard RFC 7231 / RFC 2616 formats:
    1. Non-negative integer/decimal seconds (e.g. '120', '5')
    2. HTTP-date (e.g. 'Wed, 21 Oct 2026 07:28:00 GMT')
    
    Returns delay in seconds (>= 0.0), or None if missing, empty, or unparseable.
    """
    if not header_val or not str(header_val).strip():
        return None
    val = str(header_val).strip()

    # Format 1: Non-negative seconds
    try:
        delay = float(val)
        return max(0.0, delay)
    except (ValueError, TypeError):
        pass

    # Format 2: HTTP-date string
    try:
        parsed_dt = email.utils.parsedate_to_datetime(val)
        if parsed_dt:
            if parsed_dt.tzinfo is None:
                parsed_dt = parsed_dt.replace(tzinfo=datetime.UTC)
            now = datetime.datetime.now(datetime.UTC)
            delay = (parsed_dt - now).total_seconds()
            return max(0.0, delay)
    except (ValueError, TypeError) as exc:
        logger.warning("Failed to parse Retry-After header as HTTP-date: %s", exc)

    return None


class ThirdPartyExtractionClient:
    """Production-ready HTTP client for extracting data from external APIs.
    
    Includes:
    - Multi-page pagination traversal without arbitrary page limits
    - Automatic exponential backoff retries on HTTP 429 and 5xx server errors
    - Accurate respect for Retry-After headers (numeric seconds & HTTP-date)
    - Deadline enforcement on maximum rate limit wait times
    - Safe token masking in logs
    - Flexible normalization across real public sources (e.g. HubSpot CRM, REST APIs)
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        max_retries: int = 3,
        session: requests.Session | None = None
    ):
        raw_base_url = (base_url or getattr(settings, 'EXTERNAL_API_BASE_URL', 'https://api.hubapi.com')).rstrip('/')
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
        """Determines target URL based on configured provider endpoint template.
        
        Guarantees clean URL construction without missing or duplicate slashes,
        and validates that the final URL is an absolute HTTP/HTTPS URL.
        """
        template = getattr(settings, 'EXTERNAL_API_ENDPOINT_TEMPLATE', None)
        base = self.base_url.rstrip('/')
        clean_source = source_type.strip().lower()

        # JSONPlaceholder public testing mock
        if 'jsonplaceholder' in base.lower():
            target = 'users' if clean_source in ('crm', 'contacts') else clean_source
            return self._validate_and_normalize_url(f"{base}/{target}")

        # Legacy / mock testing endpoint fallback
        if 'crm-provider.test' in base.lower():
            return self._validate_and_normalize_url(f"{base}/{clean_source}/records")

        # Map common aliases if using HubSpot CRM
        if 'hubapi' in base:
            endpoint_name = 'contacts' if clean_source in ('crm', 'contacts') else clean_source
        else:
            endpoint_name = clean_source

        if template and str(template).strip():
            template_str = str(template).strip()
            if '{base_url}' in template_str:
                formatted = template_str.format(base_url=base, source_type=endpoint_name)
            else:
                rel = template_str.format(source_type=endpoint_name).lstrip('/')
                formatted = f"{base}/{rel}"
        elif 'hubapi' in base:
            formatted = f"{base}/crm/v3/objects/{endpoint_name}"
        else:
            formatted = f"{base}/{endpoint_name}/records"

        return self._validate_and_normalize_url(formatted)

    def _validate_and_normalize_url(self, formatted: str) -> str:
        """Validates URL structure and collapses duplicate path slashes."""
        parsed = urllib.parse.urlsplit(formatted)
        if not parsed.scheme or not parsed.netloc:
            raise ExternalServiceError(
                message=f"Configured external API URL '{formatted}' is invalid. An absolute HTTP/HTTPS URL is required.",
                details={'url': formatted}
            )
        if not getattr(settings, 'DEBUG', True) and parsed.scheme.lower() != 'https':
            raise ExternalServiceError(
                message="External provider URL must use HTTPS in production.",
                details={'url': formatted}
            )

        # Collapse duplicate slashes in URL path (e.g. //crm -> /crm)
        norm_path = re.sub(r'/+', '/', parsed.path)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, norm_path, parsed.query, parsed.fragment))

    def _execute_request_with_retries(
        self,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
        max_retry_after: float | None = None,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> requests.Response:
        """Executes GET request with separate retry budgets for 429 rate limits and 5xx/timeout errors.
        
        Honors server Retry-After (seconds and HTTP-date), enforces per-wait and cumulative total wait
        deadlines, applies chunked heartbeat updates during wait intervals, and guarantees tokens are
        never leaked in 429 logs or error messages.
        """
        server_attempts = 0
        rate_limit_attempts = 0
        total_rate_limit_waited = 0.0
        backoff_delay = 0.2

        max_rate_limit_retries = getattr(settings, 'EXTERNAL_API_MAX_RATE_LIMIT_RETRIES', 5)
        max_total_rate_limit_wait = getattr(settings, 'EXTERNAL_API_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS', 300.0)
        max_allowed_retry_after = (
            max_retry_after
            if max_retry_after is not None
            else getattr(settings, 'EXTERNAL_API_MAX_RETRY_AFTER_SECONDS', 60.0)
        )
        heartbeat_chunk_seconds = getattr(settings, 'EXTRACTION_HEARTBEAT_CHUNK_SECONDS', 30.0)

        while True:
            try:
                response = self.session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout
                )
            except requests.exceptions.Timeout as exc:
                server_attempts += 1
                if server_attempts <= self.max_retries:
                    jitter = random.uniform(0.05, 0.15)
                    delay = backoff_delay + jitter
                    logger.warning(
                        "Request timed out on server attempt %d/%d; retrying in %.2fs | url=%s",
                        server_attempts, self.max_retries, delay, url
                    )
                    time.sleep(delay)
                    backoff_delay *= 2
                    continue
                logger.error("External service timeout after %d attempts | url=%s", server_attempts, url)
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

            # Handle 429 Rate Limit (Separate retry budget from 5xx/timeouts)
            if status_code == 429:
                rate_limit_attempts += 1
                if rate_limit_attempts > max_rate_limit_retries:
                    logger.warning(
                        "Rate limit retry attempts exhausted (%d/%d) | url=%s",
                        rate_limit_attempts, max_rate_limit_retries, url
                    )
                    return response

                retry_after_header = response.headers.get('Retry-After')
                parsed_delay = parse_retry_after(retry_after_header)

                if parsed_delay is not None:
                    # Per-wait ceiling check
                    if parsed_delay > max_allowed_retry_after:
                        logger.warning(
                            "429 Retry-After delay (%.2fs) exceeds maximum allowed per-wait threshold (%.2fs) | url=%s",
                            parsed_delay, max_allowed_retry_after, url
                        )
                        raise RateLimitExceededError(
                            message=(
                                f"External rate limit delay of {parsed_delay:.1f}s "
                                f"exceeds maximum allowed threshold of {max_allowed_retry_after:.1f}s."
                            ),
                            details={'status_code': 429, 'retry_after': parsed_delay, 'max_allowed': max_allowed_retry_after}
                        )
                    wait_time = parsed_delay
                else:
                    # Missing/malformed Retry-After: fall back to exponential backoff with jitter
                    jitter = random.uniform(0.05, 0.15)
                    wait_time = backoff_delay + jitter
                    backoff_delay *= 2

                # Cumulative total wait ceiling check
                if total_rate_limit_waited + wait_time > max_total_rate_limit_wait:
                    logger.warning(
                        "429 total accumulated wait (%.2fs) would exceed maximum total rate limit wait ceiling (%.2fs) | url=%s",
                        total_rate_limit_waited + wait_time, max_total_rate_limit_wait, url
                    )
                    raise RateLimitExceededError(
                        message=(
                            f"Total rate limit wait time ({total_rate_limit_waited + wait_time:.1f}s) "
                            f"exceeded ceiling of {max_total_rate_limit_wait:.1f}s."
                        ),
                        details={
                            'status_code': 429,
                            'total_waited': total_rate_limit_waited,
                            'max_total_wait': max_total_rate_limit_wait
                        }
                    )

                total_rate_limit_waited += wait_time
                logger.warning(
                    "Hit 429 Rate Limit on rate-limit attempt %d/%d; waiting %.2fs (total waited: %.2fs) before retrying | url=%s",
                    rate_limit_attempts, max_rate_limit_retries, wait_time, total_rate_limit_waited, url
                )
                _sleep_with_heartbeat(wait_time, heartbeat_callback, chunk_size=heartbeat_chunk_seconds)
                continue

            # Handle 5xx Server Errors (Separate retry budget from 429s)
            if status_code >= 500:
                server_attempts += 1
                if server_attempts <= self.max_retries:
                    jitter = random.uniform(0.05, 0.15)
                    delay = backoff_delay + jitter
                    logger.warning(
                        "Hit HTTP %d on attempt %d/%d; retrying in %.2fs | url=%s",
                        status_code, server_attempts, self.max_retries, delay, url
                    )
                    time.sleep(delay)
                    backoff_delay *= 2
                    continue
                return response

            # Non-retryable responses returned immediately
            return response

    def _normalize_raw_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalizes diverse third-party schemas into standard entity records."""
        rec_id = str(raw.get('id') or raw.get('id_from_service') or raw.get('uuid') or '')
        props = raw.get('properties') if isinstance(raw.get('properties'), dict) else {}
        email = raw.get('email') or props.get('email')

        first_name = raw.get('first_name', '') or props.get('firstname', '') or props.get('first_name', '')
        last_name = raw.get('last_name', '') or props.get('lastname', '') or props.get('last_name', '')

        # Fallback if provider returns a single 'name' field
        if not first_name and not last_name and raw.get('name'):
            parts = str(raw['name']).strip().split(' ', 1)
            first_name = parts[0]
            last_name = parts[1] if len(parts) > 1 else ''

        custom = raw.get('custom_fields') or props or {}
        if not custom:
            custom = {k: v for k, v in raw.items() if k not in ('id', 'id_from_service', 'email', 'first_name', 'last_name', 'name', 'properties')}

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
        filters: dict[str, Any] | None = None,
        max_pages: int | None = None,
        page_size: int | None = None,
        heartbeat_callback: Callable[[], None] | None = None,
        on_page_limit: str | None = None,
    ) -> ExtractionResult:
        """Fetches and pages through records from external provider endpoint.
        
        Continues fetching pages until the external provider indicates no further records
        or pages exist, supporting cursor, next-url, page-number, and offset pagination.
        Includes cycle detection and heartbeat updates across page iterations.
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

        all_records: list[dict[str, Any]] = []
        seen_ids = set()
        visited_urls = set()
        visited_cursors = set()
        next_cursor = None
        next_url = url
        pagination_type = getattr(settings, 'EXTERNAL_API_PAGINATION_TYPE', 'auto').lower()
        effective_page_size = page_size if page_size is not None else getattr(settings, 'EXTERNAL_API_PAGE_SIZE', 100)
        max_pages_limit = max_pages or getattr(settings, 'EXTERNAL_API_MAX_PAGES', 2000)
        on_page_limit_action = (on_page_limit or getattr(settings, 'EXTERNAL_API_ON_PAGE_LIMIT', 'fail')).lower()
        truncated = False

        page = 0
        while True:
            page += 1
            if page > max_pages_limit:
                if on_page_limit_action == 'fail':
                    logger.error(
                        "Reached maximum safety page limit (%d). Terminating extraction with PAGE_LIMIT_EXCEEDED.",
                        max_pages_limit
                    )
                    raise PageLimitExceededError(
                        message=f"External data extraction exceeded safety limit of {max_pages_limit} pages.",
                        details={'max_pages': max_pages_limit, 'page': page}
                    )
                logger.warning(
                    "Reached maximum safety page limit (%d). Terminating pagination with truncated=True as configured.",
                    max_pages_limit
                )
                truncated = True
                break

            page_params = dict(filters or {})
            current_req_url = next_url or url

            # Apply pagination parameters according to configured or detected pagination style
            if pagination_type == 'cursor' or (pagination_type == 'auto' and next_cursor):
                cursor_param = getattr(settings, 'EXTERNAL_API_CURSOR_PARAM', 'after')
                page_params[cursor_param] = next_cursor
                page_params['limit'] = effective_page_size
            elif pagination_type == 'offset_limit':
                page_params['offset'] = (page - 1) * effective_page_size
                page_params['limit'] = effective_page_size
            else:
                # Default page-number pagination
                page_params['page'] = page
                page_params['limit'] = effective_page_size
                page_params['_page'] = page
                page_params['_limit'] = effective_page_size

            # Cycle detection for cursor and next_url to prevent infinite loops
            if next_cursor:
                if next_cursor in visited_cursors:
                    logger.warning(
                        "Repeated pagination cursor detected ('%s'). Terminating to prevent infinite loop.",
                        next_cursor
                    )
                    truncated = True
                    if on_page_limit_action == 'fail':
                        raise PageLimitExceededError(
                            message=f"Pagination cycle detected: cursor '{next_cursor}' was repeated by external provider.",
                            details={'cursor': next_cursor}
                        )
                    break
                visited_cursors.add(next_cursor)

            if next_url and next_url != url:
                if next_url in visited_urls:
                    logger.warning(
                        "Repeated pagination URL detected ('%s'). Terminating to prevent infinite loop.",
                        next_url
                    )
                    truncated = True
                    if on_page_limit_action == 'fail':
                        raise PageLimitExceededError(
                            message=f"Pagination cycle detected: URL '{next_url}' was repeated by external provider.",
                            details={'url': next_url}
                        )
                    break
                visited_urls.add(next_url)

            response = self._execute_request_with_retries(
                current_req_url,
                headers,
                page_params,
                heartbeat_callback=heartbeat_callback
            )
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

            # Notify heartbeat callback after successfully fetching and ingesting page
            if heartbeat_callback:
                try:
                    heartbeat_callback()
                except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
                    logger.debug("Heartbeat callback exception ignored: %s", exc)

            # Detect cursor or next page link for subsequent requests
            next_cursor = None
            next_url = None
            has_paging_block = False
            if isinstance(payload, dict):
                # HubSpot style: {"paging": {"next": {"after": "..."}}}
                if 'paging' in payload:
                    has_paging_block = True
                    paging = payload.get('paging')
                    if isinstance(paging, dict):
                        next_obj = paging.get('next')
                        if isinstance(next_obj, dict):
                            next_cursor = next_obj.get('after')
                # Generic cursor style: {"next_cursor": "..."} or {"cursor": "..."}
                if not next_cursor and ('next_cursor' in payload or 'cursor' in payload):
                    has_paging_block = True
                    next_cursor = payload.get('next_cursor') or payload.get('cursor')

                # Next URL style (RFC 5988 / REST next link)
                links = getattr(response, 'links', None)
                link_next = links.get('next', {}).get('url') if isinstance(links, dict) else None
                next_url = payload.get('next') if isinstance(payload.get('next'), str) else link_next

            # If cursor pagination is detected or was active
            if has_paging_block or next_cursor:
                pagination_type = 'cursor'

            # If cursor pagination is active and no next cursor was returned, terminate
            if pagination_type == 'cursor' and not next_cursor:
                break

            # If next URL pagination is active
            if next_url and next_url != current_req_url:
                pagination_type = 'next_url'
            elif pagination_type == 'next_url' and not next_url:
                break

            # If no new records were extracted on this page, stop to prevent infinite loops
            if new_in_page == 0:
                break

            # If explicit next cursor or next link is available, continue to next page;
            # otherwise, terminate if fewer than effective_page_size items were returned.
            has_explicit_next = bool(next_cursor) or bool(next_url and next_url != current_req_url)
            if not has_explicit_next and len(page_items) < effective_page_size:
                break

        logger.info(
            "Successfully fetched %d records across pages from external provider (truncated=%s).",
            len(all_records),
            truncated
        )
        return ExtractionResult(records=all_records, truncated=truncated)
