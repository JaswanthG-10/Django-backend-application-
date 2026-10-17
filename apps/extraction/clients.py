"""Client for communicating with external third-party data providers."""
import datetime
import email.utils
import ipaddress
import logging
import random
import re
import socket
import time
import urllib.parse
from collections.abc import Callable
from typing import Any, NamedTuple

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from requests.utils import prepend_scheme_if_needed, select_proxy

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
            except Exception as exc:  # noqa: BLE001 - heartbeat callback failure must not crash rate limit sleep intervals
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


_CGNAT_NETWORK = ipaddress.ip_network('100.64.0.0/10')
_IPV6_ULA_NETWORK = ipaddress.ip_network('fc00::/7')


class PinnedIPAdapter(HTTPAdapter):
    """Custom HTTPAdapter that pins DNS resolution to a pre-validated IP address.

    Eliminates Time-of-Check to Time-of-Use (TOCTOU) DNS rebinding attacks while
    preserving TLS SNI, TLS certificate validation, and HTTP Host headers.
    """

    def __init__(self, pinned_ips: dict[str, str] | None = None, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.pinned_ips = {k.lower().strip(): v.strip() for k, v in (pinned_ips or {}).items()}

    def get_connection_with_tls_context(
        self,
        request: requests.PreparedRequest,
        verify: Any,
        proxies: dict[str, str] | None = None,
        cert: Any = None,
    ):
        proxy = select_proxy(request.url, proxies)
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request, verify, cert
        )
        parsed = urllib.parse.urlsplit(request.url)
        hostname = (parsed.hostname or '').lower().strip()
        if hostname in self.pinned_ips:
            pinned_ip = self.pinned_ips[hostname]
            host_params['host'] = pinned_ip
            if host_params.get('scheme') == 'https':
                pool_kwargs['assert_hostname'] = hostname
                pool_kwargs['server_hostname'] = hostname
        if proxy:
            proxy = prepend_scheme_if_needed(proxy, 'http')
            proxy_manager = self.proxy_manager_for(proxy)
            return proxy_manager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


def _is_restricted_ip(ip_str: str, allow_loopback: bool = False) -> bool:
    """Checks if an IP address string belongs to any private, loopback, or reserved range.

    Covers:
    - Standard private (RFC 1918, RFC 4193)
    - Loopback (127.0.0.0/8, ::1) unless explicitly permitted in debug mode
    - Link-local (169.254.0.0/16, fe80::/10)
    - Cloud metadata IP (169.254.169.254)
    - Carrier-grade NAT / CGNAT (100.64.0.0/10 - RFC 6598)
    - IPv6 ULA (fc00::/7, including fd00::/8)
    - IPv4-mapped IPv6 (::ffff:127.0.0.1, etc.)
    - Multicast, reserved, unspecified (0.0.0.0, ::)
    """
    try:
        ip = ipaddress.ip_address(ip_str.strip())
    except ValueError:
        return True

    # Unwrap IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if allow_loopback and ip.is_loopback:
        return False

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True

    if str(ip) == '169.254.169.254':
        return True

    if ip.version == 4 and ip in _CGNAT_NETWORK:
        return True

    return bool(ip.version == 6 and ip in _IPV6_ULA_NETWORK)


def _sanitize_url_for_logging(url: str) -> str:
    """Strips query parameters and userinfo credentials from URLs for safe logging."""
    if not url:
        return ''
    try:
        parsed = urllib.parse.urlsplit(url)
        netloc = parsed.hostname or ''
        if parsed.port:
            netloc += f":{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, '', ''))
    except Exception:  # noqa: BLE001 - fallback if URL parsing fails
        return '[sanitized-url]'


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

    @classmethod
    def _is_loopback_allowed(cls) -> bool:
        """Determines if loopback destinations are permitted (only when DEBUG=True and explicitly configured)."""
        return bool(getattr(settings, 'DEBUG', False) and getattr(settings, 'EXTERNAL_API_ALLOW_LOOPBACK', False))

    @classmethod
    def _is_host_allowed(cls, hostname: str, base_url: str | None = None) -> bool:
        """Validates hostname against configured EXTERNAL_API_ALLOWED_HOSTS and base URL host."""
        if not hostname:
            return False
        clean_host = hostname.lower().strip()

        allowed_setting = getattr(settings, 'EXTERNAL_API_ALLOWED_HOSTS', None) or []
        allowed_list = [h.lower().strip() for h in allowed_setting if h and str(h).strip()]

        raw_base = base_url or getattr(settings, 'EXTERNAL_API_BASE_URL', '')
        base_host = (urllib.parse.urlsplit(raw_base).hostname or '').lower().strip()

        if clean_host == base_host:
            return True
        return clean_host in allowed_list

    @classmethod
    def _resolve_and_validate_host(cls, hostname: str) -> tuple[bool, str | None]:
        """Resolves hostname and validates all returned IP addresses against SSRF filters.

        Fails closed: if hostname cannot be resolved, or if ANY resolved IP is restricted,
        returns (False, None).
        If all resolved IPs are safe, returns (True, first_safe_ip).
        """
        if not hostname:
            return False, None
        lowered = hostname.lower().strip()
        allow_loopback = cls._is_loopback_allowed()

        # Check cloud metadata and internal domain patterns first
        if lowered in ('metadata.google.internal', 'metadata', 'instance-data'):
            return False, None
        if lowered.endswith(('.internal', '.local', '.localdomain')):
            return False, None
        if lowered.endswith('.localhost') and not allow_loopback:
            return False, None

        if not allow_loopback and lowered in ('localhost', '0.0.0.0'):
            return False, None

        # Check direct IP address literal
        try:
            ip = ipaddress.ip_address(lowered)
            if _is_restricted_ip(str(ip), allow_loopback=allow_loopback):
                return False, None
            return True, str(ip)
        except ValueError:
            pass

        # Resolve hostname via DNS - FAIL CLOSED on error
        try:
            addr_infos = socket.getaddrinfo(lowered, None, proto=socket.IPPROTO_TCP)
        except (socket.gaierror, OSError) as exc:
            logger.warning(
                "DNS resolution failed (failing closed) for target host: %s | error=%s",
                lowered,
                exc
            )
            return False, None

        if not addr_infos:
            logger.warning("DNS resolution returned no addresses for host: %s", lowered)
            return False, None

        # Check EVERY resolved IP address. If ANY is restricted, reject!
        first_safe_ip = None
        for addr in addr_infos:
            ip_str = addr[4][0]
            if _is_restricted_ip(ip_str, allow_loopback=allow_loopback):
                logger.warning(
                    "Host '%s' resolved to restricted IP address '%s'. Failing closed.",
                    lowered,
                    ip_str
                )
                return False, None
            if first_safe_ip is None:
                first_safe_ip = ip_str

        return True, first_safe_ip

    @staticmethod
    def _is_same_provider_host(target_url: str, base_url: str) -> bool:
        """Determines if target URL belongs to the same provider host as base URL.

        Protects bearer tokens from being leaked to unintended external hosts during
        next-page URL pagination. Compares hostnames (without port) to handle
        port variations within the same provider.
        """
        if not target_url or not base_url:
            return False
        target_parsed = urllib.parse.urlsplit(target_url)
        base_parsed = urllib.parse.urlsplit(base_url)
        target_host = target_parsed.hostname or ''
        base_host = base_parsed.hostname or ''
        if not target_host:
            # Relative URL - resolves against provider base host
            return True
        return target_host.lower() == base_host.lower()

    def _validate_and_normalize_pagination_url(self, raw_url: str, current_url: str) -> str | None:
        """Validates and normalizes pagination next URLs with complete SSRF protection."""
        if not raw_url or not isinstance(raw_url, str) or not raw_url.strip():
            return None
        cleaned = raw_url.strip()
        resolved = urllib.parse.urljoin(current_url, cleaned)
        parsed = urllib.parse.urlsplit(resolved)
        sanitized_for_log = _sanitize_url_for_logging(resolved)

        if parsed.scheme.lower() not in ('http', 'https'):
            logger.warning("Rejected pagination URL with unsupported scheme: %s", parsed.scheme)
            return None
        if not getattr(settings, 'DEBUG', True) and parsed.scheme.lower() != 'https':
            logger.warning("Rejected non-HTTPS pagination URL in production: %s", sanitized_for_log)
            return None
        # Block userinfo in URL (e.g. https://user:pass@evil.com) to prevent credential injection
        if parsed.username or parsed.password:
            logger.warning("Rejected pagination URL containing userinfo credentials.")
            return None
        hostname = parsed.hostname
        if not hostname:
            logger.warning("Rejected pagination URL with missing host.")
            return None
        if not self._is_host_allowed(hostname, self.base_url):
            logger.warning(
                "Rejected pagination URL: host '%s' is not in configured allowlist.",
                hostname
            )
            return None
        is_safe, _ = self._resolve_and_validate_host(hostname)
        if not is_safe:
            logger.warning(
                "Rejected pagination URL targeting restricted/unresolvable destination: %s",
                hostname
            )
            return None

        # Return resolved URL without fragment
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ''))

    def _check_redirect_ssrf(self, response: requests.Response, *args: Any, **kwargs: Any) -> None:
        """Validates redirect targets against SSRF protections."""
        resp_status = getattr(response, 'status_code', None)
        if not (getattr(response, 'is_redirect', False) or (isinstance(resp_status, int) and 300 <= resp_status < 400)):
            return
        location = None
        if hasattr(response, 'headers') and hasattr(response.headers, 'get'):
            raw_loc = response.headers.get('Location')
            if isinstance(raw_loc, str):
                location = raw_loc.strip()

        if not location:
            return

        current_url = getattr(response, 'url', self.base_url)
        target_url = urllib.parse.urljoin(current_url, location)
        parsed = urllib.parse.urlsplit(target_url)
        hostname = (parsed.hostname or '').lower().strip()
        if not hostname:
            raise ExternalServiceError(
                message="Redirect target has no valid host.",
                details={'reason': 'ssrf_redirect_blocked', 'url': _sanitize_url_for_logging(target_url)}
            )
        is_safe, _ = self._resolve_and_validate_host(hostname)
        if not is_safe:
            raise ExternalServiceError(
                message=f"Redirect to restricted destination '{hostname}' is prohibited.",
                details={'reason': 'ssrf_redirect_blocked', 'host': hostname, 'url': _sanitize_url_for_logging(target_url)}
            )

    def _execute_request_with_retries(
        self,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
        max_retry_after: float | None = None,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> requests.Response:
        """Executes GET request with separate retry budgets for 429 rate limits and 5xx/timeout errors.

        Applies DNS pinning (anti-TOCTOU), manual redirect following (max 3 hops),
        per-hop host allowlisting and IP re-validation, Authorization header stripping
        on host change, and chunked heartbeat updates during wait intervals.
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
            current_url = url
            redirect_count = 0
            visited_redirect_urls: set[str] = set()
            req_headers = dict(headers)
            req_params = dict(params)
            response = None

            while True:
                sanitized_url = _sanitize_url_for_logging(current_url)
                parsed = urllib.parse.urlsplit(current_url)
                if parsed.scheme.lower() not in ('http', 'https'):
                    raise ExternalServiceError(
                        message=f"Unsupported URL scheme '{parsed.scheme}'.",
                        details={'url': sanitized_url}
                    )
                if not getattr(settings, 'DEBUG', True) and parsed.scheme.lower() != 'https':
                    raise ExternalServiceError(
                        message="External provider URL must use HTTPS in production.",
                        details={'url': sanitized_url}
                    )
                if parsed.username or parsed.password:
                    raise ExternalServiceError(
                        message="URL containing user credentials is prohibited.",
                        details={'url': sanitized_url}
                    )
                if not parsed.hostname or not self._is_host_allowed(parsed.hostname, self.base_url):
                    raise ExternalServiceError(
                        message=f"Target host '{parsed.hostname}' is not permitted by host allowlist.",
                        details={'host': parsed.hostname}
                    )

                is_safe, pinned_ip = self._resolve_and_validate_host(parsed.hostname)
                if not is_safe or not pinned_ip:
                    raise ExternalServiceError(
                        message=f"Target host '{parsed.hostname}' is restricted or cannot be resolved.",
                        details={'host': parsed.hostname}
                    )

                # Ensure Authorization header is stripped on host change or demo endpoint
                if not self._is_same_provider_host(current_url, self.base_url) or self._is_public_demo_endpoint(current_url):
                    req_headers.pop('Authorization', None)

                # Pin validated IP address to eliminate TOCTOU rebinding
                pinned_adapter = PinnedIPAdapter(pinned_ips={parsed.hostname: pinned_ip})
                self.session.mount('https://', pinned_adapter)
                self.session.mount('http://', pinned_adapter)

                try:
                    response = self.session.get(
                        current_url,
                        headers=req_headers,
                        params=req_params if redirect_count == 0 else None,
                        timeout=self.timeout,
                        allow_redirects=False,
                    )
                except requests.exceptions.Timeout as exc:
                    server_attempts += 1
                    if server_attempts <= self.max_retries:
                        jitter = random.uniform(0.05, 0.15)
                        delay = backoff_delay + jitter
                        logger.warning(
                            "Request timed out on server attempt %d/%d; retrying in %.2fs | url=%s",
                            server_attempts, self.max_retries, delay, sanitized_url
                        )
                        time.sleep(delay)
                        backoff_delay *= 2
                        response = None
                        break
                    logger.error("External service timeout after %d attempts | url=%s", server_attempts, sanitized_url)
                    raise ExternalServiceTimeoutError(
                        message=f"External data source timed out after {self.timeout} seconds.",
                        details={'url': sanitized_url, 'timeout_seconds': self.timeout}
                    ) from exc
                except requests.exceptions.RequestException as exc:
                    logger.error("External connection error | url=%s error=%s", sanitized_url, str(exc))
                    raise ExternalServiceError(
                        message="Unable to connect to external data extraction service.",
                        details={'url': sanitized_url, 'reason': 'Connection failure'}
                    ) from exc

                # Check for redirect responses (301, 302, 303, 307, 308)
                resp_status = getattr(response, 'status_code', None)
                if isinstance(resp_status, int) and (resp_status in (301, 302, 303, 307, 308) or (300 <= resp_status < 400)):
                    redirect_count += 1
                    if redirect_count > 3:
                        logger.error("Too many redirects: exceeded maximum 3 hops | url=%s", sanitized_url)
                        raise ExternalServiceError(
                            message="Too many redirects: exceeded maximum hop limit of 3.",
                            details={'hops': redirect_count, 'url': sanitized_url}
                        )
                    location = None
                    if hasattr(response, 'headers') and hasattr(response.headers, 'get'):
                        raw_loc = response.headers.get('Location')
                        if isinstance(raw_loc, str):
                            location = raw_loc.strip()

                    if not location:
                        break

                    next_target = urllib.parse.urljoin(current_url, location)
                    sanitized_next = _sanitize_url_for_logging(next_target)
                    if next_target in visited_redirect_urls:
                        logger.error("Redirect loop detected | url=%s", sanitized_next)
                        raise ExternalServiceError(
                            message=f"Redirect loop detected: '{sanitized_next}' was previously visited.",
                            details={'url': sanitized_next}
                        )
                    visited_redirect_urls.add(current_url)
                    visited_redirect_urls.add(next_target)
                    current_url = next_target
                    continue

                break

            if response is None:
                continue

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

        sanitized_init_url = _sanitize_url_for_logging(url)
        # Prevent tokens from being sent to public demo/mock sandbox endpoints
        if self._is_public_demo_endpoint(url):
            logger.info(
                "Target URL '%s' is a public demo/sandbox endpoint. "
                "Omitted Authorization header to prevent token leakage.",
                sanitized_init_url
            )
        else:
            headers['Authorization'] = f"Bearer {api_token.strip()}"

        masked = self._mask_token(api_token)
        logger.info(
            "Initiating external extraction | url=%s source=%s token=%s params=%s",
            sanitized_init_url, source_type, masked, filters
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

            # Ensure bearer token is not leaked to unintended external hosts or public demo sandboxes
            req_headers = dict(headers)
            if not self._is_same_provider_host(current_req_url, url) or self._is_public_demo_endpoint(current_req_url):
                req_headers.pop('Authorization', None)

            response = self._execute_request_with_retries(
                current_req_url,
                req_headers,
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

            sanitized_current_url = _sanitize_url_for_logging(current_req_url)
            if status_code == 404:
                logger.warning("External resource not found | url=%s", sanitized_current_url)
                raise ResourceNotFoundError(
                    message="Requested data source resource not found at external provider.",
                    details={'url': sanitized_current_url, 'status_code': 404}
                )

            if status_code == 429:
                logger.warning("External service rate limit hit after retries | url=%s", sanitized_current_url)
                raise RateLimitExceededError(
                    message="External provider rate limit exceeded. Please back off and retry.",
                    details={'status_code': 429}
                )

            if status_code >= 500:
                logger.error("External service server error | status=%d url=%s", status_code, sanitized_current_url)
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

                # Next URL style (RFC 5988 / REST next link or payload 'next')
                links = getattr(response, 'links', None)
                link_next = links.get('next', {}).get('url') if isinstance(links, dict) else None
                raw_next_url = payload.get('next') if isinstance(payload.get('next'), str) else link_next
                if raw_next_url:
                    next_url = self._validate_and_normalize_pagination_url(raw_next_url, current_req_url)

            # Strip empty or whitespace-only cursors
            if next_cursor is not None and not str(next_cursor).strip():
                next_cursor = None

            # Determine if explicit pagination metadata for a subsequent page exists
            has_explicit_next = bool(next_cursor) or bool(next_url and next_url != current_req_url)

            # An actually empty page with no explicit next metadata terminates pagination cleanly
            if not page_items and not has_explicit_next:
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

            # Detect repeated self-referencing next URL as a cycle before requesting next page
            if next_url and next_url == current_req_url:
                logger.warning(
                    "Pagination cycle detected: external provider returned self-referencing next URL ('%s').",
                    next_url
                )
                truncated = True
                if on_page_limit_action == 'fail':
                    raise PageLimitExceededError(
                        message=f"Pagination cycle detected: external provider returned self-referencing next URL '{next_url}'.",
                        details={'url': next_url}
                    )
                break

            # Update detected pagination type
            if has_paging_block or next_cursor:
                pagination_type = 'cursor'
            elif next_url and next_url != current_req_url:
                pagination_type = 'next_url'

            # Decision to proceed to next page or terminate:
            if has_explicit_next:
                # Follow valid pagination metadata even when current page contains only duplicate records (new_in_page == 0)
                pass
            else:
                # No explicit next metadata provided by external API:
                # For cursor-based pagination, absence of next_cursor marks the end of stream
                if pagination_type == 'cursor':
                    break
                # For next-URL pagination, absence of next_url marks the end of stream
                if pagination_type == 'next_url':
                    break
                # For offset-limit or page-number pagination:
                # Terminate if page returned fewer items than requested page size
                if len(page_items) < effective_page_size:
                    break
                # Terminate if no new records were found on this page and no next metadata exists
                if new_in_page == 0:
                    break

        logger.info(
            "Successfully fetched %d records across pages from external provider (truncated=%s).",
            len(all_records),
            truncated
        )
        return ExtractionResult(records=all_records, truncated=truncated)
