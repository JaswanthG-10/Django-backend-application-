"""Regression tests verifying pagination continuation, URL safety, and background worker reliability."""
import socket
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from cryptography.fernet import Fernet
from django.test import override_settings
from django.utils import timezone

from apps.common.exceptions import ExternalServiceError, PageLimitExceededError
from apps.extraction.clients import ExtractionResult, ThirdPartyExtractionClient
from apps.extraction.models import ExtractedRecord, ExtractionJob, JobStatus, SourceType
from apps.extraction.services import ExtractionService


@pytest.fixture
def extraction_client():
    return ThirdPartyExtractionClient(base_url="https://api.hubapi.com")


class TestPaginationRegressions:
    """Test suite covering edge cases in pagination loop continuation and metadata handling."""

    def test_duplicate_page_with_valid_next_cursor_retrieves_subsequent_records(self, extraction_client):
        """Case 1: A page contains only duplicate records but supplies a valid next cursor.
        The next page contains new records, and those records are retrieved without premature stopping.
        """
        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = {
            "results": [{"id": "rec_1", "email": "user1@example.com"}],
            "paging": {"next": {"after": "cursor_page_2"}}
        }

        # Page 2 returns the exact same record (duplicate, new_in_page == 0) but supplies valid next cursor
        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = {
            "results": [{"id": "rec_1", "email": "user1@example.com"}],
            "paging": {"next": {"after": "cursor_page_3"}}
        }

        # Page 3 returns new record and no further pages
        page3_resp = MagicMock()
        page3_resp.status_code = 200
        page3_resp.json.return_value = {
            "results": [{"id": "rec_2", "email": "user2@example.com"}]
        }

        with patch.object(extraction_client.session, 'get', side_effect=[page1_resp, page2_resp, page3_resp]):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts"
            )

        assert isinstance(result, ExtractionResult)
        assert len(result.records) == 2
        record_ids = [r["id_from_service"] for r in result.records]
        assert record_ids == ["rec_1", "rec_2"]
        assert result.truncated is False

    def test_duplicate_page_with_valid_next_url_retrieves_subsequent_records(self, extraction_client):
        """Case 2: A page contains only duplicate records but supplies a valid next URL.
        The next page contains new records, and those records are retrieved without premature stopping.
        """
        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.links = {}
        page1_resp.json.return_value = {
            "results": [{"id": "rec_1", "email": "user1@example.com"}],
            "next": "https://api.hubapi.com/crm/v3/objects/contacts?page=2"
        }

        # Page 2 returns duplicate only, but next URL to page 3
        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.links = {}
        page2_resp.json.return_value = {
            "results": [{"id": "rec_1", "email": "user1@example.com"}],
            "next": "https://api.hubapi.com/crm/v3/objects/contacts?page=3"
        }

        # Page 3 returns new record and no next URL
        page3_resp = MagicMock()
        page3_resp.status_code = 200
        page3_resp.links = {}
        page3_resp.json.return_value = {
            "results": [{"id": "rec_2", "email": "user2@example.com"}],
            "next": None
        }

        with patch.object(extraction_client.session, 'get', side_effect=[page1_resp, page2_resp, page3_resp]):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts"
            )

        assert len(result.records) == 2
        assert [r["id_from_service"] for r in result.records] == ["rec_1", "rec_2"]
        assert result.truncated is False

    def test_empty_page_terminates_cleanly_when_no_next_metadata(self, extraction_client):
        """Case 3: An actually empty page terminates correctly when there is no next-page metadata."""
        empty_resp = MagicMock()
        empty_resp.status_code = 200
        empty_resp.links = {}
        empty_resp.json.return_value = {"results": []}

        with patch.object(extraction_client.session, 'get', return_value=empty_resp) as mock_get:
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts"
            )

        assert mock_get.call_count == 1
        assert len(result.records) == 0
        assert result.truncated is False

    def test_repeated_cursor_terminates_safely_preventing_infinite_loops(self, extraction_client):
        """Case 4a: Repeated cursors terminate safely without infinite loops."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {
            "results": [{"id": "rec_1"}],
            "paging": {"next": {"after": "cycle_cursor"}}
        }

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {
            "results": [{"id": "rec_2"}],
            "paging": {"next": {"after": "cycle_cursor"}}  # Repeated cursor
        }

        # In fail mode: raises PageLimitExceededError
        with patch.object(extraction_client.session, 'get', side_effect=[resp1, resp2]):
            with pytest.raises(PageLimitExceededError) as exc_info:
                extraction_client.extract_data(
                    api_token="test-crm-token",
                    source_type="contacts",
                    on_page_limit="fail"
                )
            assert "Pagination cycle detected" in str(exc_info.value)

        # In truncate mode: terminates gracefully and marks truncated=True
        with patch.object(extraction_client.session, 'get', side_effect=[resp1, resp2]):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts",
                on_page_limit="truncate"
            )
            assert result.truncated is True
            assert len(result.records) == 2

    def test_repeated_next_url_terminates_safely_preventing_infinite_loops(self, extraction_client):
        """Case 4b: Repeated next URLs terminate safely without infinite loops."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.links = {}
        resp1.json.return_value = {
            "results": [{"id": "rec_1"}],
            "next": "https://api.hubapi.com/crm/v3/objects/contacts?page=repeat"
        }

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.links = {}
        resp2.json.return_value = {
            "results": [{"id": "rec_2"}],
            "next": "https://api.hubapi.com/crm/v3/objects/contacts?page=repeat"  # Repeated URL
        }

        with patch.object(extraction_client.session, 'get', side_effect=[resp1, resp2]):
            with pytest.raises(PageLimitExceededError) as exc_info:
                extraction_client.extract_data(
                    api_token="test-crm-token",
                    source_type="contacts",
                    on_page_limit="fail"
                )
            assert "Pagination cycle detected" in str(exc_info.value)

        with patch.object(extraction_client.session, 'get', side_effect=[resp1, resp2]):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts",
                on_page_limit="truncate"
            )
            assert result.truncated is True
            assert len(result.records) == 2

    def test_max_page_limit_enforced_in_fail_mode(self, extraction_client):
        """Case 5 & 6: Configured maximum page limit is enforced and raises on fail mode."""
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = lambda: {
            "results": [{"id": f"rec_{time.time()}"}],
            "paging": {"next": {"after": f"c_{time.time()}"}}
        }

        with patch.object(extraction_client.session, 'get', return_value=resp):
            with pytest.raises(PageLimitExceededError) as exc_info:
                extraction_client.extract_data(
                    api_token="test-crm-token",
                    source_type="contacts",
                    max_pages=3,
                    on_page_limit="fail"
                )
            assert "exceeded safety limit of 3 pages" in str(exc_info.value)

    def test_max_page_limit_enforced_in_truncate_mode(self, extraction_client):
        """Case 7: Truncate mode returns an explicitly marked truncated result."""
        def make_page(idx):
            m = MagicMock()
            m.status_code = 200
            m.json.return_value = {
                "results": [{"id": f"rec_{idx}"}],
                "paging": {"next": {"after": f"cursor_{idx + 1}"}}
            }
            return m

        with patch.object(extraction_client.session, 'get', side_effect=[make_page(1), make_page(2), make_page(3)]):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts",
                max_pages=2,
                on_page_limit="truncate"
            )
            assert result.truncated is True
            assert len(result.records) == 2

    @override_settings(EXTERNAL_API_ALLOWED_HOSTS=['unintended-external-service.org'])
    def test_unintended_host_in_next_url_strips_authorization_header(self, extraction_client):
        """Case 10: External pagination links do not leak bearer tokens to unintended hosts.
        Legitimate same-provider pagination links preserve the Authorization header.
        """
        initial_resp = MagicMock()
        initial_resp.status_code = 200
        initial_resp.links = {}
        # External pagination link pointing to an untrusted third-party host
        initial_resp.json.return_value = {
            "results": [{"id": "rec_1"}],
            "next": "https://unintended-external-service.org/crm/records?page=2"
        }

        second_resp = MagicMock()
        second_resp.status_code = 200
        second_resp.links = {}
        second_resp.json.return_value = {
            "results": [{"id": "rec_2"}],
            "next": None
        }

        recorded_headers = []

        def capture_get(url, headers, **kwargs):
            recorded_headers.append((url, dict(headers)))
            if len(recorded_headers) == 1:
                return initial_resp
            return second_resp

        with patch.object(extraction_client.session, 'get', side_effect=capture_get):
            result = extraction_client.extract_data(
                api_token="confidential-provider-bearer-token",
                source_type="contacts"
            )

        assert len(recorded_headers) == 2
        # Initial request to api.hubapi.com MUST contain Authorization header
        first_url, first_headers = recorded_headers[0]
        assert "api.hubapi.com" in first_url
        assert first_headers.get("Authorization") == "Bearer confidential-provider-bearer-token"

        # Subsequent request to unintended-external-service.org MUST NOT leak Authorization header
        second_url, second_headers = recorded_headers[1]
        assert "unintended-external-service.org" in second_url
        assert "Authorization" not in second_headers

        assert len(result.records) == 2

    @pytest.mark.parametrize("target_url", [
        "http://127.0.0.1:8000/api/v1/health",
        "http://localhost/records",
        "http://[::1]/next",
        "http://0.0.0.0:8000/api",
        "http://10.0.0.1/data",
        "http://192.168.1.1/data",
        "http://172.16.0.1/data",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1/",
        "https://service.local/api",
        "https://server.internal/crm",
    ])
    def test_pagination_rejects_loopback_private_and_metadata_targets(self, extraction_client, target_url):
        """Prevent SSRF: Pagination URLs targeting private, loopback, or cloud metadata endpoints are rejected."""
        validated = extraction_client._validate_and_normalize_pagination_url(
            target_url,
            "https://api.hubapi.com/crm/v3/objects/contacts"
        )
        assert validated is None

    def test_pagination_rejects_urls_with_userinfo(self, extraction_client):
        """Prevent SSRF & Credential Injection: URLs with embedded credentials (user:pass@host) are rejected."""
        url_with_creds = "https://admin:supersecret@api.hubapi.com/crm/v3/objects/contacts?page=2"
        validated = extraction_client._validate_and_normalize_pagination_url(
            url_with_creds,
            "https://api.hubapi.com/crm/v3/objects/contacts"
        )
        assert validated is None

    def test_pagination_redirect_to_private_ip_is_blocked(self, extraction_client):
        """Prevent Redirect-Based SSRF: Redirect responses pointing to private/metadata IPs are aborted."""
        resp = MagicMock()
        resp.is_redirect = True
        resp.status_code = 302
        resp.url = "https://api.hubapi.com/crm/v3/objects/contacts"
        resp.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

        with pytest.raises(ExternalServiceError) as exc_info:
            extraction_client._check_redirect_ssrf(resp)
        assert "Redirect to restricted destination '169.254.169.254' is prohibited." in str(exc_info.value)
        assert exc_info.value.details.get("reason") == "ssrf_redirect_blocked"

    def test_empty_page_with_valid_next_cursor_continues_to_subsequent_page(self, extraction_client):
        """An empty page (results: []) that provides an explicit continuation cursor proceeds to fetch the next page."""
        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = {
            "results": [],
            "paging": {"next": {"after": "cursor_for_page_2"}}
        }

        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = {
            "results": [{"id": "rec_discovered_on_page2", "email": "p2@example.com"}]
        }

        with patch.object(extraction_client.session, 'get', side_effect=[page1_resp, page2_resp]):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts"
            )

        assert len(result.records) == 1
        assert result.records[0]["id_from_service"] == "rec_discovered_on_page2"
        assert result.truncated is False

    def test_self_referencing_next_url_terminates_gracefully_in_truncate_mode(self, extraction_client):
        """A self-referencing next URL identical to the current page URL terminates cleanly when configured to truncate."""
        base = "https://api.hubapi.com/crm/v3/objects/contacts"
        resp = MagicMock()
        resp.status_code = 200
        resp.links = {}
        resp.json.return_value = {
            "results": [{"id": "rec_loop"}],
            "next": base  # Self-referencing identical URL
        }

        with patch.object(extraction_client.session, 'get', return_value=resp):
            result = extraction_client.extract_data(
                api_token="test-crm-token",
                source_type="contacts",
                on_page_limit="truncate"
            )

        assert result.truncated is True
        assert len(result.records) == 1


@pytest.mark.django_db
class TestBackgroundExtractionReliability:
    """Test suite covering background job claiming, dispatching, recovery, and persistence safety."""

    def test_duplicate_records_not_persisted_more_than_once(self):
        """Case 8: Duplicate records are not persisted more than once in ExtractedRecord."""
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="test-token-dup", source_type=SourceType.CRM)

        # Feed mock data containing repeated service IDs
        mock_records = [
            {"id_from_service": "dup_1", "email": "dup@example.com"},
            {"id_from_service": "dup_1", "email": "dup@example.com"},
            {"id_from_service": "dup_2", "email": "other@example.com"},
        ]

        completed_job = service.process_extraction(
            job_id=job.id,
            api_token="test-token-dup",
            mock_records=mock_records
        )

        assert completed_job.status == JobStatus.COMPLETED
        assert completed_job.record_count == 2
        assert ExtractedRecord.objects.filter(job=job).count() == 2

    @override_settings(EXTRACTION_ASYNC_EXECUTION=True)
    def test_recovered_pending_job_is_scheduled_for_execution(self):
        """Requirement: Confirm that recovered PENDING jobs are actually scheduled for execution."""
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="recoverable-token-123", source_type=SourceType.CRM)

        # Simulate a crashed worker: job was in_progress with a stale heartbeat 30 minutes ago
        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        ExtractionJob.objects.filter(id=job.id).update(
            status=JobStatus.IN_PROGRESS,
            start_time=stale_time,
            heartbeat_at=stale_time
        )

        with patch.object(ExtractionService, 'dispatch_background_extraction') as mock_dispatch:
            recovered_count = ExtractionService.recover_stale_jobs(timeout_minutes=15)
            assert recovered_count == 1

            # Job is reset to PENDING
            job.refresh_from_db()
            assert job.status == JobStatus.PENDING
            assert job.retry_count == 1

            # Verify that dispatch_background_extraction was invoked so the job doesn't sit pending forever
            mock_dispatch.assert_called_once_with(job_id=job.id, api_token="recoverable-token-123")

    def test_atomic_job_claiming_prevents_concurrent_duplicate_claims(self):
        """Requirement: Prevent two workers from processing the same job concurrently."""
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="concurrent-claim-token", source_type=SourceType.CRM)

        # First worker claims the job
        claim1 = ExtractionService.claim_job(job_id=job.id)
        assert claim1 is not None
        assert claim1.id == job.id

        # Second worker attempts to claim the exact same job
        claim2 = ExtractionService.claim_job(job_id=job.id)
        assert claim2 is None

    def test_claim_next_pending_job_advances_under_contention(self):
        """Requirement: Next pending job selection advances across available pending jobs."""
        service = ExtractionService()
        job1, _ = service.initiate_job(api_token="token-cand-1", source_type=SourceType.CRM)
        job2, _ = service.initiate_job(api_token="token-cand-2", source_type=SourceType.CONTACTS)

        # Worker 1 claims next pending job
        claimed_1 = ExtractionService.claim_next_pending_job()
        assert claimed_1 is not None
        assert claimed_1.id == job1.id

        # Worker 2 claims next pending job -> gets job2
        claimed_2 = ExtractionService.claim_next_pending_job()
        assert claimed_2 is not None
        assert claimed_2.id == job2.id

        # No more pending jobs
        claimed_3 = ExtractionService.claim_next_pending_job()
        assert claimed_3 is None

    def test_cancellation_during_extraction_skips_persistence(self):
        """Requirement: Preserve cancellation behavior and avoid persisting records after cancellation."""
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="cancel-token", source_type=SourceType.CRM)

        # Job is cancelled before persistence
        def mock_extract(*args, **kwargs):
            # Cancel job while extraction is in flight
            ExtractionJob.objects.filter(id=job.id).update(status=JobStatus.CANCELLED)
            return ExtractionResult(records=[{"id_from_service": "cancel_rec_1"}], truncated=False)

        with patch.object(service.client, 'extract_data', side_effect=mock_extract):
            result_job = service.process_extraction(job_id=job.id, api_token="cancel-token")

        result_job.refresh_from_db()
        assert result_job.status == JobStatus.CANCELLED
        # Extracted records must NOT be persisted for cancelled job
        assert ExtractedRecord.objects.filter(job=job).count() == 0

    def test_stale_job_recovery_retry_exhaustion_permanently_fails(self):
        """Requirement: Stale job with exhausted retry budget transitions permanently to FAILED."""
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="exhausted-token", source_type=SourceType.CRM)

        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        # Already at max_retries (3)
        ExtractionJob.objects.filter(id=job.id).update(
            status=JobStatus.IN_PROGRESS,
            retry_count=3,
            max_retries=3,
            start_time=stale_time,
            heartbeat_at=stale_time
        )

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered == 1

        job.refresh_from_db()
        assert job.status == JobStatus.FAILED
        assert job.error_details.get("code") == "MAX_RETRIES_EXCEEDED"

    def test_multifernet_key_rotation_supports_decryption_with_previous_key(self):
        """Requirement: The application must not lose access to encrypted API tokens when key changes."""
        import apps.common.encryption as enc
        from apps.common.encryption import decrypt_secret, encrypt_secret

        old_key = Fernet.generate_key().decode()
        new_key = Fernet.generate_key().decode()

        # Encrypt token under old key
        with override_settings(FIELD_ENCRYPTION_KEY=old_key):
            enc._fernet_instance = None
            ciphertext = encrypt_secret("my-super-secret-provider-token")

        # Now configure key rotation: new_key is primary, old_key is fallback
        with override_settings(FIELD_ENCRYPTION_KEY=f"{new_key},{old_key}"):
            enc._fernet_instance = None
            decrypted = decrypt_secret(ciphertext)
            assert decrypted == "my-super-secret-provider-token"

    def test_stale_job_with_undecryptable_token_fails_permanently_without_retrying(self):
        """A stale job whose encrypted token cannot be decrypted (corrupted or rotated without fallback)
        transitions permanently to FAILED with WORKER_ABANDONED_TIMEOUT instead of spinning in PENDING.
        """
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="test-token-to-corrupt", source_type=SourceType.CRM)

        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        # Corrupt the encrypted_token in DB
        ExtractionJob.objects.filter(id=job.id).update(
            status=JobStatus.IN_PROGRESS,
            encrypted_token="corrupted-non-fernet-token-data",
            start_time=stale_time,
            heartbeat_at=stale_time
        )

        with patch.object(ExtractionService, 'dispatch_background_extraction') as mock_dispatch:
            recovered_count = ExtractionService.recover_stale_jobs(timeout_minutes=15)
            assert recovered_count == 1
            mock_dispatch.assert_not_called()

        job.refresh_from_db()
        assert job.status == JobStatus.FAILED
        assert job.error_details.get("code") == "WORKER_ABANDONED_TIMEOUT"
        assert "Missing or undecryptable recovery credentials" in job.error_details.get("reason", "")

    @override_settings(EXTRACTION_ASYNC_EXECUTION=True)
    def test_stale_job_recovery_handles_dispatch_exception_gracefully(self):
        """If dispatch_background_extraction raises an exception during recovery,
        the recovery process catches it, logs an error, and does not crash.
        """
        service = ExtractionService()
        job, _ = service.initiate_job(api_token="dispatch-fail-token", source_type=SourceType.CRM)

        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        ExtractionJob.objects.filter(id=job.id).update(
            status=JobStatus.IN_PROGRESS,
            start_time=stale_time,
            heartbeat_at=stale_time
        )

        with patch.object(ExtractionService, 'dispatch_background_extraction', side_effect=RuntimeError("ThreadPool exhausted")):
            recovered_count = ExtractionService.recover_stale_jobs(timeout_minutes=15)
            assert recovered_count == 1

        job.refresh_from_db()
        assert job.status == JobStatus.PENDING

    def test_get_fernet_returns_multifernet_and_handles_invalid_tokens(self):
        """Verify MultiFernet type and InvalidToken handling when decrypting with wrong key."""
        from cryptography.fernet import MultiFernet

        import apps.common.encryption as enc
        from apps.common.encryption import decrypt_secret, get_fernet

        k1 = Fernet.generate_key().decode()
        k2 = Fernet.generate_key().decode()
        wrong_key = Fernet.generate_key().decode()

        with override_settings(FIELD_ENCRYPTION_KEY=f"{k1},{k2}"):
            enc._fernet_instance = None
            cipher = get_fernet()
            assert isinstance(cipher, MultiFernet)

        # Encrypt with wrong_key, try to decrypt with k1
        cipher_wrong = Fernet(wrong_key.encode()).encrypt(b"secret payload").decode()
        with override_settings(FIELD_ENCRYPTION_KEY=k1):
            enc._fernet_instance = None
            with pytest.raises(ValueError) as exc_info:
                decrypt_secret(cipher_wrong)
            assert "Failed to decrypt stored credential" in str(exc_info.value)


class TestSSRFHardeningAndProtection:
    """Comprehensive regression tests verifying complete SSRF hardening and anti-rebinding defenses."""

    def test_dns_resolution_failure_fails_closed(self, extraction_client):
        """1. DNS resolution failure fails closed: unresolvable host is rejected."""
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(11001, "getaddrinfo failed")):
            validated = extraction_client._validate_and_normalize_pagination_url(
                "https://nonexistent-domain.xyz/records",
                "https://api.hubapi.com/crm/v3/objects/contacts"
            )
            assert validated is None

    def test_mixed_public_and_private_ips_rejected(self, extraction_client):
        """2. Mixed public and private IPs: host resolving to both 93.184.215.14 and 10.0.0.1 is rejected."""
        mixed_addrs = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.215.14', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', 443)),
        ]
        with patch('socket.getaddrinfo', return_value=mixed_addrs):
            is_safe, ip = extraction_client._resolve_and_validate_host("evil-dual-homed.com")
            assert is_safe is False
            assert ip is None

    @pytest.mark.parametrize("target", [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "100.64.0.1",
        "metadata.google.internal",
        "localhost",
        "cluster.internal",
        "printer.local",
    ])
    def test_every_restricted_target_is_rejected(self, extraction_client, target):
        """3. Every restricted target is rejected by host validation."""
        is_safe, ip = extraction_client._resolve_and_validate_host(target)
        assert is_safe is False
        assert ip is None

    def test_dns_rebinding_prevention_pins_initial_ip(self, extraction_client):
        """4. DNS rebinding prevention: PinnedIPAdapter connects to pinned IP and never queries DNS during request."""
        from apps.extraction.clients import PinnedIPAdapter

        adapter = PinnedIPAdapter(pinned_ips={'api.hubapi.com': '199.60.103.28'})
        prep = requests.PreparedRequest()
        prep.url = 'https://api.hubapi.com/crm/v3/objects/contacts'
        prep.headers = {}

        with patch.object(adapter.poolmanager, 'connection_from_host') as mock_conn:
            adapter.get_connection_with_tls_context(prep, verify=True)
            mock_conn.assert_called_once()
            call_kwargs = mock_conn.call_args[1]
            assert call_kwargs['pool_kwargs']['server_hostname'] == 'api.hubapi.com'
            assert call_kwargs['pool_kwargs']['assert_hostname'] == 'api.hubapi.com'
            assert call_kwargs.get('host') == '199.60.103.28' or mock_conn.call_args[0][0] == '199.60.103.28'

    @override_settings(EXTERNAL_API_ALLOWED_HOSTS=['10.0.0.1'])
    def test_redirect_to_private_ip_is_blocked(self, extraction_client):
        """5a. Redirect to private IP is blocked."""
        resp_302 = MagicMock(status_code=302, headers={'Location': 'https://10.0.0.1/stolen'})
        with patch.object(extraction_client.session, 'get', return_value=resp_302):
            with pytest.raises(ExternalServiceError) as exc_info:
                extraction_client._execute_request_with_retries(
                    "https://api.hubapi.com/crm/v3/objects/contacts",
                    headers={'Authorization': 'Bearer secret'},
                    params={}
                )
            assert "restricted or cannot be resolved" in str(exc_info.value.message)

    @override_settings(EXTERNAL_API_ALLOWED_HOSTS=['unintended-external-service.org'])
    def test_redirect_to_allowlisted_public_host_strips_authorization(self, extraction_client):
        """5b. Redirect to allowlisted public host succeeds and Authorization header is not forwarded."""
        resp_302 = MagicMock(status_code=302, headers={'Location': 'https://unintended-external-service.org/v1/records'})
        resp_200 = MagicMock(status_code=200, headers={})

        calls = []

        def capture_get(url, headers, **kwargs):
            calls.append((url, dict(headers)))
            if len(calls) == 1:
                return resp_302
            return resp_200

        with patch.object(extraction_client.session, 'get', side_effect=capture_get):
            final_resp = extraction_client._execute_request_with_retries(
                "https://api.hubapi.com/crm/v3/objects/contacts",
                headers={'Authorization': 'Bearer secret-token'},
                params={}
            )
            assert final_resp == resp_200
            assert len(calls) == 2
            first_url, first_hdr = calls[0]
            assert "api.hubapi.com" in first_url
            assert first_hdr.get('Authorization') == 'Bearer secret-token'

            second_url, second_hdr = calls[1]
            assert "unintended-external-service.org" in second_url
            assert 'Authorization' not in second_hdr

    def test_redirect_loop_detected_and_fails_cleanly(self, extraction_client):
        """5c. Redirect loop detected and fails cleanly."""
        resp_loop = MagicMock(status_code=302, headers={'Location': 'https://api.hubapi.com/crm/v3/objects/contacts'})
        with patch.object(extraction_client.session, 'get', return_value=resp_loop):
            with pytest.raises(ExternalServiceError) as exc_info:
                extraction_client._execute_request_with_retries(
                    "https://api.hubapi.com/crm/v3/objects/contacts",
                    headers={'Authorization': 'Bearer secret'},
                    params={}
                )
            assert "Redirect loop detected" in str(exc_info.value.message)

    def test_redirect_exceeding_three_hops_fails_cleanly(self, extraction_client):
        """5d. Exceeding maximum 3 redirect hops raises ExternalServiceError."""
        def make_redirect_hop(hop):
            return MagicMock(status_code=302, headers={'Location': f'https://api.hubapi.com/hop_{hop}'})

        hops = [make_redirect_hop(1), make_redirect_hop(2), make_redirect_hop(3), make_redirect_hop(4)]
        with patch.object(extraction_client.session, 'get', side_effect=hops):
            with pytest.raises(ExternalServiceError) as exc_info:
                extraction_client._execute_request_with_retries(
                    "https://api.hubapi.com/start",
                    headers={'Authorization': 'Bearer secret'},
                    params={}
                )
            assert "Too many redirects" in str(exc_info.value.message)

    def test_userinfo_in_url_rejected(self, extraction_client):
        """6. Userinfo in URL is rejected in both initial request and pagination."""
        with pytest.raises(ExternalServiceError) as exc_info:
            extraction_client._execute_request_with_retries(
                "https://user:password@api.hubapi.com/crm/v3/objects/contacts",
                headers={},
                params={}
            )
        assert "user credentials" in str(exc_info.value.message)

        norm = extraction_client._validate_and_normalize_pagination_url(
            "https://user:pass@api.hubapi.com/next",
            "https://api.hubapi.com/start"
        )
        assert norm is None

    def test_loopback_allowed_only_in_debug_with_setting(self, extraction_client):
        """7. Loopback allowed only when DEBUG=True and EXTERNAL_API_ALLOW_LOOPBACK=True; rejected in production."""
        with override_settings(DEBUG=True, EXTERNAL_API_ALLOW_LOOPBACK=True):
            is_safe, ip = extraction_client._resolve_and_validate_host("127.0.0.1")
            assert is_safe is True
            assert ip == "127.0.0.1"

        with override_settings(DEBUG=False, EXTERNAL_API_ALLOW_LOOPBACK=True):
            is_safe, ip = extraction_client._resolve_and_validate_host("127.0.0.1")
            assert is_safe is False
            assert ip is None

        with override_settings(DEBUG=True, EXTERNAL_API_ALLOW_LOOPBACK=False):
            is_safe, ip = extraction_client._resolve_and_validate_host("127.0.0.1")
            assert is_safe is False
            assert ip is None

    def test_logs_do_not_contain_token_or_userinfo_for_rejected_urls(self, extraction_client, caplog):
        """8. Logs for failed/rejected URLs do not leak tokens, userinfo, or sensitive query params."""
        import logging
        caplog.set_level(logging.DEBUG)

        sensitive_url = "https://admin:supersecret@api.hubapi.com/crm/v3/objects/contacts?token=leaked_secret_token_123"
        with pytest.raises(ExternalServiceError):
            extraction_client._execute_request_with_retries(
                sensitive_url,
                headers={},
                params={}
            )

        log_text = caplog.text
        assert "supersecret" not in log_text
        assert "leaked_secret_token_123" not in log_text
        assert "admin:" not in log_text
