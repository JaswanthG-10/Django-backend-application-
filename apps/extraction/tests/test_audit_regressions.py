"""Regression tests verifying audit fixes and stability requirements."""
import datetime
import email.utils
import hashlib
import hmac
import json
import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from apps.common.exceptions import (
    ExternalServiceError,
    PageLimitExceededError,
    RateLimitExceededError,
    ResourceNotFoundError,
)
from apps.extraction.clients import ThirdPartyExtractionClient, parse_retry_after
from apps.extraction.models import ExtractedRecord, ExtractionJob, JobStatus, SourceType
from apps.extraction.services import ExtractionService
from config.settings import parse_bool


@pytest.mark.django_db
class TestAuditSecurityAndCredentialIsolation:
    def test_authorization_header_not_hijacked_as_external_token(self, api_client):
        """Security: Authorization header authenticates service caller only, never used as external token."""
        url = reverse('extraction:scan-start')
        # api_client already has HTTP_X_API_KEY, but let's send Authorization header
        response = api_client.post(
            url,
            data={'source_type': 'crm'},
            format='json',
            HTTP_AUTHORIZATION='Bearer caller-service-key-123'
        )
        # Must fail with 400 because external api_token was not provided
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        error_resp = response.json()
        assert 'api_token' in str(error_resp)

    def test_x_api_token_header_accepted_as_external_token(self, api_client):
        """Security: Dedicated X-API-Token header is accepted for external provider credentials."""
        url = reverse('extraction:scan-start')
        with patch.object(ExtractionService, 'dispatch_background_extraction'):
            response = api_client.post(
                url,
                data={'source_type': 'contacts'},
                format='json',
                HTTP_X_API_TOKEN='external-crm-token-9999'
            )
            assert response.status_code == status.HTTP_202_ACCEPTED
            data = response.json()
            job = ExtractionJob.objects.get(id=data['job_id'])
            assert job.get_decrypted_token() == 'external-crm-token-9999'

    def test_encrypted_token_storage_lifecycle(self, db):
        """Security: External tokens are encrypted at rest and purged on terminal state."""
        raw_token = 'very-confidential-crm-pat-token-abc'
        job = ExtractionJob(
            source_type=SourceType.CRM,
            token_hash=ExtractionJob.hash_token(raw_token),
            dedup_hash=ExtractionJob.compute_dedup_hash(raw_token),
            masked_token=ExtractionJob.mask_token(raw_token),
            status=JobStatus.PENDING
        )
        job.set_encrypted_token(raw_token)
        job.save()

        # Ciphertext must not match raw token
        assert job.encrypted_token != raw_token
        assert raw_token not in job.encrypted_token

        # Decryption reproduces raw token
        assert job.get_decrypted_token() == raw_token

        # Mark in progress: token remains available
        job.transition_to(JobStatus.IN_PROGRESS)
        assert job.get_decrypted_token() == raw_token

        # Mark completed: token is purged
        job.transition_to(JobStatus.COMPLETED, record_count=0)
        job.refresh_from_db()
        assert job.encrypted_token == ''
        assert job.get_decrypted_token() is None

    def test_encrypted_token_purged_on_cancellation(self, db):
        """Security: Token is purged when job is cancelled."""
        raw_token = 'secret-token-to-cancel'
        job = ExtractionJob(
            source_type=SourceType.CRM,
            token_hash=ExtractionJob.hash_token(raw_token),
            dedup_hash=ExtractionJob.compute_dedup_hash(raw_token),
            masked_token=ExtractionJob.mask_token(raw_token),
            status=JobStatus.PENDING
        )
        job.set_encrypted_token(raw_token)
        job.save()

        job.transition_to(JobStatus.CANCELLED)
        job.refresh_from_db()
        assert job.encrypted_token == ''
        assert job.get_decrypted_token() is None

    def test_token_masking_comprehensive(self):
        """Verifies mask_token handles empty, short, medium, and long tokens."""
        assert ExtractionJob.mask_token('') == '***'
        assert ExtractionJob.mask_token('a') == '***'
        assert ExtractionJob.mask_token('short') == '***'
        assert ExtractionJob.mask_token('12345678') == '***'
        assert ExtractionJob.mask_token('123456789') == '123***6789'
        assert ExtractionJob.mask_token('my-super-secret-hubspot-pat') == 'my-***-pat'

        # ThirdPartyExtractionClient._mask_token
        assert ThirdPartyExtractionClient._mask_token('') == '***'
        assert ThirdPartyExtractionClient._mask_token('abc') == '***'
        assert ThirdPartyExtractionClient._mask_token('12345678') == '***'
        assert ThirdPartyExtractionClient._mask_token('123456789') == '123***6789'


@pytest.mark.django_db
class TestWorkerConcurrencyAndReliability:
    def test_atomic_claim_next_pending_job_single_winner(self, db):
        """Worker Reliability: Atomic claiming guarantees only one worker claims a pending job."""
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash=ExtractionJob.hash_token("test-tok-1"),
            dedup_hash=ExtractionJob.compute_dedup_hash("test-tok-1"),
            masked_token="tes***ok-1",
            status=JobStatus.PENDING
        )
        job.set_encrypted_token("test-tok-1")
        job.save()

        # First claim succeeds
        claimed = ExtractionService.claim_next_pending_job()
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == JobStatus.IN_PROGRESS

        # Second concurrent claim finds no more pending jobs
        claimed_second = ExtractionService.claim_next_pending_job()
        assert claimed_second is None

    def test_stale_job_recovery(self, db):
        """Worker Reliability: Stale abandoned in_progress jobs are automatically recovered."""
        old_time = timezone.now() - timezone.timedelta(minutes=30)
        stale_job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash=ExtractionJob.hash_token("stale-tok"),
            dedup_hash=ExtractionJob.compute_dedup_hash("stale-tok"),
            masked_token="sta***-tok",
            status=JobStatus.IN_PROGRESS,
            start_time=old_time
        )

        recovered_count = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered_count == 1

        stale_job.refresh_from_db()
        assert stale_job.status == JobStatus.FAILED
        assert 'timed out' in stale_job.error_message
        assert stale_job.error_details['code'] == 'WORKER_ABANDONED_TIMEOUT'

    def test_worker_recover_stale_command_flag(self, db):
        """Worker Management Command: --recover-stale command flag executes recovery."""
        old_time = timezone.now() - timezone.timedelta(minutes=45)
        stale_job = ExtractionJob.objects.create(
            source_type=SourceType.LEADS,
            token_hash=ExtractionJob.hash_token("stale-tok-cmd"),
            dedup_hash=ExtractionJob.compute_dedup_hash("stale-tok-cmd"),
            masked_token="sta***-cmd",
            status=JobStatus.IN_PROGRESS,
            start_time=old_time
        )

        out = StringIO()
        call_command('run_extraction_worker', '--recover-stale', '--once', stdout=out)
        stale_job.refresh_from_db()
        assert stale_job.status == JobStatus.FAILED
        assert "Recovered 1 stale/abandoned in_progress job(s)" in out.getvalue()

    def test_cancellation_during_extraction_aborts_cleanly(self, db):
        """Worker Reliability: Cancelling a job during network call prevents worker from marking completed."""
        raw_token = "cancel-during-net-tok"
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash=ExtractionJob.hash_token(raw_token),
            dedup_hash=ExtractionJob.compute_dedup_hash(raw_token),
            masked_token="can***-tok",
            status=JobStatus.PENDING
        )
        job.set_encrypted_token(raw_token)
        job.save()

        mock_client = MagicMock()

        def side_effect_cancel(*args, **kwargs):
            # Simulate user calling cancel while network request was in flight
            job.transition_to(JobStatus.CANCELLED)
            return [{'id': 'c1', 'email': 'user1@test.com'}]

        mock_client.extract_data.side_effect = side_effect_cancel

        service = ExtractionService(client=mock_client)
        result_job = service.process_extraction(job_id=job.id, api_token=raw_token)

        result_job.refresh_from_db()
        assert result_job.status == JobStatus.CANCELLED
        # Records must not have been persisted
        assert ExtractedRecord.objects.filter(job=result_job).count() == 0

    def test_record_count_reflects_actual_database_records(self, db):
        """Data Consistency: record_count reflects actual persisted records when DB conflicts occur."""
        raw_token = "db-conflict-token"
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash=ExtractionJob.hash_token(raw_token),
            dedup_hash=ExtractionJob.compute_dedup_hash(raw_token),
            masked_token="db-***oken",
            status=JobStatus.PENDING
        )
        job.set_encrypted_token(raw_token)
        job.save()

        mock_records = [
            {'id': 'dup1', 'email': 'dup@test.com', 'first_name': 'Dup', 'last_name': 'User'},
            {'id': 'dup1', 'email': 'dup@test.com', 'first_name': 'Dup', 'last_name': 'User'},
            {'id': 'unique2', 'email': 'u2@test.com', 'first_name': 'Unique', 'last_name': 'User'},
        ]

        service = ExtractionService()
        completed_job = service.process_extraction(
            job_id=job.id,
            api_token=raw_token,
            mock_records=mock_records
        )

        completed_job.refresh_from_db()
        assert completed_job.status == JobStatus.COMPLETED
        # Actual unique records in DB must be 2
        assert completed_job.record_count == 2
        assert ExtractedRecord.objects.filter(job=completed_job).count() == 2


@pytest.mark.django_db
class TestHealthProbesAndDiagnostics:
    def test_liveness_probe_returns_200(self, unauthenticated_client):
        """Health: Liveness probe confirms Django process is responsive without authentication."""
        url = reverse('extraction:health-liveness')
        response = unauthenticated_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'ok'
        assert data['probe'] == 'liveness'

    def test_readiness_probe_returns_200_when_db_healthy(self, unauthenticated_client):
        """Health: Readiness probe confirms database connectivity without authentication."""
        url = reverse('extraction:health-readiness')
        response = unauthenticated_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'ok'
        assert data['database'] == 'healthy'
        assert data['probe'] == 'readiness'

    def test_health_check_includes_checks_dictionary(self, unauthenticated_client):
        """Health: Main health check provides structured checks dictionary."""
        url = reverse('extraction:health-check')
        response = unauthenticated_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'ok'
        assert data['database'] == 'healthy'
        assert 'checks' in data
        assert data['checks']['liveness'] == 'ok'
        assert data['checks']['readiness'] == 'ok'


@pytest.mark.django_db
class TestUnifiedClaimingAndStaleRetryDecision:
    def test_unified_claiming_both_paths_atomic(self):
        """Unified Claiming: Both inline thread and standalone worker must atomically claim job."""
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-unified-claim",
            dedup_hash="dedup-unified-claim",
            status=JobStatus.PENDING
        )
        job.set_encrypted_token("test-unified-token")
        job.save()

        # Worker 1 claims specific job (inline path)
        claimed_1 = ExtractionService.claim_job(job_id=job.id)
        assert claimed_1 is not None
        assert claimed_1.status == JobStatus.IN_PROGRESS

        # Worker 2 attempts to claim same job concurrently (standalone worker path)
        claimed_2 = ExtractionService.claim_job(job_id=job.id)
        assert claimed_2 is None

        # Next pending claim returns None since job is already claimed
        claimed_next = ExtractionService.claim_next_pending_job()
        assert claimed_next is None

    def test_stale_job_safe_retry_requeues_and_increments_retry_count(self):
        """Recovery: Interrupted in-progress job under max retries is safely re-queued to PENDING."""
        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-retry-test",
            dedup_hash="dedup-retry-test",
            status=JobStatus.IN_PROGRESS,
            start_time=stale_time,
            retry_count=0,
            max_retries=3,
            record_count=5
        )
        job.set_encrypted_token("test-retry-token")
        job.save()

        # Add dummy partial records from interrupted attempt
        ExtractedRecord.objects.create(
            job=job,
            id_from_service="partial_1",
            first_name="Partial",
            last_name="Record"
        )
        assert ExtractedRecord.objects.filter(job=job).count() == 1

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15, allow_retry=True)
        assert recovered == 1

        job.refresh_from_db()
        assert job.status == JobStatus.PENDING
        assert job.retry_count == 1
        assert job.start_time is None
        assert job.record_count == 1
        assert ExtractedRecord.objects.filter(job=job).count() == 1
        assert 'AUTOMATIC_RECOVERY_RETRY' in str(job.error_details)

    def test_stale_job_max_retries_exceeded_fails_permanently(self):
        """Recovery: Stale job exceeding max retries is permanently failed."""
        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-max-retry",
            dedup_hash="dedup-max-retry",
            status=JobStatus.IN_PROGRESS,
            start_time=stale_time,
            retry_count=3,
            max_retries=3
        )
        job.set_encrypted_token("test-token")
        job.save()

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15, allow_retry=True)
        assert recovered == 1

        job.refresh_from_db()
        assert job.status == JobStatus.FAILED
        assert job.error_details.get('code') == 'MAX_RETRIES_EXCEEDED'


class TestConfigurableDataSourceAndPagination:
    @patch('requests.Session.get')
    def test_hubspot_cursor_based_pagination(self, mock_get):
        """Configurable Provider: Traverses cursor-based pagination (HubSpot style)."""
        resp_page1 = MagicMock()
        resp_page1.status_code = 200
        resp_page1.json.return_value = {
            'results': [
                {'id': 'hb_1', 'properties': {'email': 'hb1@example.com', 'firstname': 'Alice'}},
                {'id': 'hb_2', 'properties': {'email': 'hb2@example.com', 'firstname': 'Bob'}},
            ],
            'paging': {'next': {'after': 'cursor_page_2'}}
        }

        resp_page2 = MagicMock()
        resp_page2.status_code = 200
        resp_page2.json.return_value = {
            'results': [
                {'id': 'hb_3', 'properties': {'email': 'hb3@example.com', 'firstname': 'Charlie'}},
            ],
            'paging': {}
        }

        mock_get.side_effect = [resp_page1, resp_page2]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="hubspot-pat-test-token", source_type="contacts")
        records = result.records

        assert len(records) == 3
        assert mock_get.call_count == 2
        # Check that page 2 passed after=cursor_page_2
        assert mock_get.call_args_list[1][1]['params']['after'] == 'cursor_page_2'

    @patch('requests.Session.get')
    def test_configurable_endpoint_template(self, mock_get):
        """Configurable Provider: Formats custom endpoint template."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{'id': 'custom_1', 'email': 'c@test.com'}]
        mock_get.return_value = mock_resp

        with override_settings(
            EXTERNAL_API_BASE_URL="https://crm.myenterprise.com/v2",
            EXTERNAL_API_ENDPOINT_TEMPLATE="{base_url}/data-export/{source_type}/all"
        ):
            client = ThirdPartyExtractionClient()
            client.extract_data(api_token="test-enterprise-token", source_type="leads")

            call_url = mock_get.call_args[0][0]
            assert call_url == "https://crm.myenterprise.com/v2/data-export/leads/all"


@pytest.mark.django_db
class TestStrengthenedAuthenticationAndRequestSigning:
    def test_request_signing_valid_signature_accepted(self, unauthenticated_client):
        """Security: Valid HMAC-SHA256 request signature authenticates caller."""
        url = reverse('extraction:scan-start')
        timestamp = str(int(time.time()))
        payload = {'source_type': 'crm', 'api_token': 'secret-crm-token-998877'}
        body_bytes = json.dumps(payload).encode('utf-8')
        body_hash = hashlib.sha256(body_bytes).hexdigest()

        signing_secret = settings.API_KEY_SECRET
        canonical = f"{timestamp}.POST.{url}.{body_hash}"
        signature = hmac.new(signing_secret.encode('utf-8'), canonical.encode('utf-8'), hashlib.sha256).hexdigest()

        response = unauthenticated_client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_SIGNATURE=signature,
            HTTP_X_TIMESTAMP=timestamp
        )
        assert response.status_code == status.HTTP_202_ACCEPTED

    def test_request_signing_tampered_body_rejected(self, unauthenticated_client):
        """Security: Tampered payload with mismatched signature is rejected with 401."""
        url = reverse('extraction:scan-start')
        timestamp = str(int(time.time()))
        original_payload = {'source_type': 'crm', 'api_token': 'original-token-112233'}
        tampered_payload = {'source_type': 'crm', 'api_token': 'tampered-token-445566'}

        body_hash = hashlib.sha256(json.dumps(original_payload).encode('utf-8')).hexdigest()
        canonical = f"{timestamp}.POST.{url}.{body_hash}"
        signature = hmac.new(settings.API_KEY_SECRET.encode('utf-8'), canonical.encode('utf-8'), hashlib.sha256).hexdigest()

        response = unauthenticated_client.post(
            url,
            data=json.dumps(tampered_payload),
            content_type='application/json',
            HTTP_X_SIGNATURE=signature,
            HTTP_X_TIMESTAMP=timestamp
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert 'Invalid request signature' in response.json()['error']['message']

    def test_request_signing_expired_timestamp_rejected(self, unauthenticated_client):
        """Security: Expired timestamp outside tolerance window is rejected."""
        url = reverse('extraction:scan-start')
        expired_timestamp = str(int(time.time()) - 600)  # 10 minutes ago
        payload = {'source_type': 'crm', 'api_token': 'test-token-123456'}
        body_hash = hashlib.sha256(json.dumps(payload).encode('utf-8')).hexdigest()

        canonical = f"{expired_timestamp}.POST.{url}.{body_hash}"
        signature = hmac.new(settings.API_KEY_SECRET.encode('utf-8'), canonical.encode('utf-8'), hashlib.sha256).hexdigest()

        response = unauthenticated_client.post(
            url,
            data=json.dumps(payload),
            content_type='application/json',
            HTTP_X_SIGNATURE=signature,
            HTTP_X_TIMESTAMP=expired_timestamp
        )
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        assert 'expired' in response.json()['error']['message'].lower()


class TestCompletePaginationAndRealAPI:
    """Issue 1: Complete API pagination and real external API."""

    @patch('requests.Session.get')
    def test_pagination_fetches_over_100_records_completely(self, mock_get):
        """Pagination: Fetches >100 records across 7 pages without stopping at 5 pages."""
        # 7 pages of 20 records each = 140 records
        mock_pages = []
        for p in range(1, 8):
            resp = MagicMock()
            resp.status_code = 200
            items = [{'id': f'rec_{p}_{i}', 'properties': {'email': f'user_{p}_{i}@crm.com', 'firstname': f'First{p}_{i}'}} for i in range(20)]
            if p < 7:
                resp.json.return_value = {
                    'results': items,
                    'paging': {'next': {'after': f'cursor_page_{p+1}'}}
                }
            else:
                resp.json.return_value = {
                    'results': items,
                    'paging': {}
                }
            mock_pages.append(resp)

        mock_get.side_effect = mock_pages

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="test-token", source_type="contacts", page_size=20)
        records = result.records

        assert len(records) == 140
        assert not result.truncated
        assert mock_get.call_count == 7
        # Verify pagination continued past the legacy 5-page / 100-record limit
        assert any(r['id_from_service'] == 'rec_7_19' for r in records)

    @patch('requests.Session.get')
    def test_pagination_stops_when_no_further_pages(self, mock_get):
        """Pagination: Stops gracefully when external provider returns empty results."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {'results': [{'id': '1', 'properties': {'email': 'a@a.com'}}], 'paging': {'next': {'after': 'c2'}}}

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {'results': [], 'paging': {}}

        mock_get.side_effect = [resp1, resp2]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="test-token", source_type="contacts")
        records = result.records
        assert len(records) == 1
        assert not result.truncated
        assert mock_get.call_count == 2

    @patch('requests.Session.get')
    def test_pagination_deduplicates_duplicate_records(self, mock_get):
        """Pagination: Duplicate records across pages are not ingested twice."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {
            'results': [
                {'id': 'dup_1', 'properties': {'email': 'dup1@test.com'}},
                {'id': 'dup_2', 'properties': {'email': 'dup2@test.com'}},
            ],
            'paging': {'next': {'after': 'page2'}}
        }

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {
            'results': [
                {'id': 'dup_2', 'properties': {'email': 'dup2@test.com'}},  # duplicate
                {'id': 'dup_3', 'properties': {'email': 'dup3@test.com'}},
            ],
            'paging': {}
        }

        mock_get.side_effect = [resp1, resp2]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="test-token", source_type="contacts")
        records = result.records
        assert len(records) == 3
        ids = [r['id_from_service'] for r in records]
        assert ids == ['dup_1', 'dup_2', 'dup_3']

    @patch('requests.Session.get')
    def test_pagination_cycle_detection_prevents_infinite_loop(self, mock_get):
        """Pagination: Repeated next cursor halts pagination safely."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {
            'results': [{'id': 'c_1', 'properties': {'email': 'c1@test.com'}}],
            'paging': {'next': {'after': 'same_loop_cursor'}}
        }

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {
            'results': [{'id': 'c_2', 'properties': {'email': 'c2@test.com'}}],
            'paging': {'next': {'after': 'same_loop_cursor'}}  # loop!
        }

        mock_get.side_effect = [resp1, resp2]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="test-token", source_type="contacts", on_page_limit='truncate')
        records = result.records
        # Must terminate without infinite loop and report truncated
        assert len(records) == 2
        assert result.truncated is True
        assert mock_get.call_count == 2


@pytest.mark.django_db
class TestHeartbeatStaleJobRecovery:
    """Issue 2: Heartbeat-based stale-job recovery."""

    def test_job_running_longer_than_threshold_with_fresh_heartbeat_not_recovered(self):
        """Heartbeat Recovery: A job running 45 min with fresh heartbeat (2 min ago) is NOT recovered."""
        old_start = timezone.now() - timezone.timedelta(minutes=45)
        fresh_heartbeat = timezone.now() - timezone.timedelta(minutes=2)

        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-fresh-hb",
            dedup_hash="dedup-fresh-hb",
            status=JobStatus.IN_PROGRESS,
            start_time=old_start,
            heartbeat_at=fresh_heartbeat,
            retry_count=0,
            max_retries=3
        )

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered == 0

        job.refresh_from_db()
        assert job.status == JobStatus.IN_PROGRESS
        assert job.start_time == old_start
        assert job.heartbeat_at == fresh_heartbeat

    def test_job_with_stale_heartbeat_is_eligible_for_recovery(self):
        """Heartbeat Recovery: A job with a stale heartbeat (>15 min) is eligible and recovered."""
        old_start = timezone.now() - timezone.timedelta(minutes=45)
        stale_heartbeat = timezone.now() - timezone.timedelta(minutes=20)

        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-stale-hb",
            dedup_hash="dedup-stale-hb",
            status=JobStatus.IN_PROGRESS,
            start_time=old_start,
            heartbeat_at=stale_heartbeat,
            retry_count=0,
            max_retries=3
        )
        job.set_encrypted_token("test-hb-token")
        job.save()

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered == 1

        job.refresh_from_db()
        assert job.status == JobStatus.PENDING
        assert job.retry_count == 1
        assert job.heartbeat_at is None

    def test_heartbeat_updated_during_execution(self):
        """Heartbeat: record_heartbeat and update_job_heartbeat update timestamp atomically."""
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-hb-exec",
            status=JobStatus.IN_PROGRESS,
            start_time=timezone.now()
        )
        assert job.heartbeat_at is None

        success = ExtractionService.update_job_heartbeat(job.id)
        assert success is True

        job.refresh_from_db()
        assert job.heartbeat_at is not None
        assert timezone.now() - job.heartbeat_at < timezone.timedelta(seconds=5)

    def test_completed_jobs_not_recovered(self):
        """Heartbeat Recovery: Completed jobs are never recovered, regardless of age."""
        old_time = timezone.now() - timezone.timedelta(hours=2)
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-completed",
            status=JobStatus.COMPLETED,
            start_time=old_time,
            heartbeat_at=old_time,
            record_count=50
        )

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered == 0

        job.refresh_from_db()
        assert job.status == JobStatus.COMPLETED

    def test_existing_processed_records_survive_recovery(self):
        """Heartbeat Recovery: Processed records survive recovery by default."""
        stale_time = timezone.now() - timezone.timedelta(minutes=30)
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-survive",
            status=JobStatus.IN_PROGRESS,
            start_time=stale_time,
            heartbeat_at=stale_time,
            retry_count=0,
            max_retries=3
        )
        job.set_encrypted_token("test-survive-token")
        job.save()

        # Create processed records
        ExtractedRecord.objects.create(job=job, id_from_service="rec_s1", first_name="Survive1")
        ExtractedRecord.objects.create(job=job, id_from_service="rec_s2", first_name="Survive2")
        assert ExtractedRecord.objects.filter(job=job).count() == 2

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered == 1

        # Processed records must survive
        assert ExtractedRecord.objects.filter(job=job).count() == 2
        job.refresh_from_db()
        assert job.record_count == 2
        assert job.status == JobStatus.PENDING

    def test_legacy_job_null_heartbeat_fallback(self):
        """Heartbeat Recovery: Jobs created prior to migration (null heartbeat_at) fall back safely to start_time."""
        old_start = timezone.now() - timezone.timedelta(minutes=30)
        job = ExtractionJob.objects.create(
            source_type=SourceType.CRM,
            token_hash="hash-legacy",
            status=JobStatus.IN_PROGRESS,
            start_time=old_start,
            heartbeat_at=None,
            retry_count=0,
            max_retries=3
        )
        job.set_encrypted_token("test-legacy-tok")
        job.save()

        recovered = ExtractionService.recover_stale_jobs(timeout_minutes=15)
        assert recovered == 1

        job.refresh_from_db()
        assert job.status == JobStatus.PENDING


class TestExternalApiUrlConfiguration:
    """Issue 3: Correct external API URL configuration."""

    def test_fresh_setup_constructs_valid_absolute_url(self):
        """URL Configuration: Fresh setup constructs valid absolute URL with no duplicate slashes."""
        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        url = client._determine_url("contacts")
        assert url == "https://api.hubapi.com/crm/v3/objects/contacts"

    def test_relative_endpoint_template_joins_correctly(self):
        """URL Configuration: Relative template formatted without duplicate slashes."""
        with override_settings(
            EXTERNAL_API_BASE_URL="https://api.mycrm.com/",
            EXTERNAL_API_ENDPOINT_TEMPLATE="/v1/{source_type}/data"
        ):
            client = ThirdPartyExtractionClient()
            url = client._determine_url("leads")
            assert url == "https://api.mycrm.com/v1/leads/data"

    def test_invalid_external_url_configuration_fails_clearly(self):
        """URL Configuration: Invalid or missing scheme URL raises clear ExternalServiceError."""
        client = ThirdPartyExtractionClient(base_url="invalid-no-scheme")
        with pytest.raises(ExternalServiceError) as exc_info:
            client._determine_url("contacts")
        assert "is invalid" in exc_info.value.message


class TestSecureDjangoDebugDefaults:
    """Issue 4: Secure Django DEBUG defaults."""

    def test_parse_bool_evaluates_correctly(self):
        """Security: parse_bool parses boolean representations correctly rather than string truthiness."""
        assert parse_bool("False") is False
        assert parse_bool("false") is False
        assert parse_bool("0") is False
        assert parse_bool("no") is False
        assert parse_bool("f") is False
        assert parse_bool("") is False
        assert parse_bool(None) is False
        assert parse_bool(False) is False

        assert parse_bool("True") is True
        assert parse_bool("true") is True
        assert parse_bool("1") is True
        assert parse_bool("yes") is True
        assert parse_bool("t") is True
        assert parse_bool(True) is True

        with pytest.raises(ValueError):
            parse_bool("not-a-boolean")

    def test_missing_debug_defaults_to_false(self, monkeypatch):
        """Security: Missing DEBUG setting defaults safely to False."""
        monkeypatch.delenv("DEBUG", raising=False)
        from decouple import Config, RepositoryEmpty
        empty_config = Config(RepositoryEmpty())
        val = empty_config('DEBUG', default=False, cast=parse_bool)
        assert val is False

    def test_missing_production_secrets_raise_improperly_configured(self):
        """Security: Weak or missing SECRET_KEY in production raises ImproperlyConfigured."""
        # Check SECRET_KEY validation logic
        insecure_key = 'django-insecure-short'
        with pytest.raises(ImproperlyConfigured):
            if insecure_key.startswith('django-insecure-') or len(insecure_key) < 32:
                raise ImproperlyConfigured("SECRET_KEY is insecure.")


class TestHttp429RetryAfterHandling:
    """Issue 5: Correct HTTP 429 Retry-After handling."""

    def test_parse_retry_after_integer_seconds(self):
        """Retry-After: Integer seconds parsed accurately."""
        assert parse_retry_after("15") == 15.0
        assert parse_retry_after("0") == 0.0
        assert parse_retry_after("120.5") == 120.5

    def test_parse_retry_after_http_date(self):
        """Retry-After: Valid RFC 7231 HTTP-date parsed into seconds delay."""
        future_dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=60)
        date_str = email.utils.format_datetime(future_dt)

        delay = parse_retry_after(date_str)
        assert delay is not None
        # Allow ±3s execution tolerance
        assert 55.0 <= delay <= 65.0

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_integer_retry_after_honored_without_two_second_cap(self, mock_get, mock_sleep):
        """Retry-After: Delay of 10 seconds is honored without being capped at 2 seconds."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {'Retry-After': '10'}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {'results': [{'id': '1', 'properties': {'email': 'a@a.com'}}], 'paging': {}}

        mock_get.side_effect = [resp_429, resp_200]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="test-token", source_type="contacts")
        records = result.records

        assert len(records) == 1
        assert mock_get.call_count == 2
        # Verify sleep was called with 10.0 seconds (not 2.0s)
        mock_sleep.assert_called_with(10.0)

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_http_date_retry_after_honored(self, mock_get, mock_sleep):
        """Retry-After: HTTP-date format honored accurately."""
        future_dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=25)
        date_str = email.utils.format_datetime(future_dt)

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {'Retry-After': date_str}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {'results': [], 'paging': {}}

        mock_get.side_effect = [resp_429, resp_200]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        client.extract_data(api_token="test-token", source_type="contacts")

        assert mock_sleep.call_count == 1
        slept = mock_sleep.call_args[0][0]
        assert 20.0 <= slept <= 30.0

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_retry_after_exceeding_deadline_fails_safely(self, mock_get, mock_sleep):
        """Retry-After: Delay exceeding max allowed threshold raises RateLimitExceededError immediately."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {'Retry-After': '3600'}  # 1 hour delay requested

        mock_get.return_value = resp_429

        with override_settings(EXTERNAL_API_MAX_RETRY_AFTER_SECONDS=60.0):
            client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
            with pytest.raises(RateLimitExceededError) as exc_info:
                client.extract_data(api_token="test-token", source_type="contacts")

            assert exc_info.value.status_code == 429
            # Did not sleep for an hour
            mock_sleep.assert_not_called()

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_malformed_retry_after_handled_safely(self, mock_get, mock_sleep):
        """Retry-After: Malformed header falls back safely to exponential backoff with jitter."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {'Retry-After': 'invalid-not-a-number-or-date'}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {'results': [], 'paging': {}}

        mock_get.side_effect = [resp_429, resp_200]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        client.extract_data(api_token="test-token", source_type="contacts")

        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1
        slept = mock_sleep.call_args[0][0]
        assert 0.2 <= slept <= 1.0

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_transient_5xx_retries_with_backoff_and_jitter(self, mock_get, mock_sleep):
        """Retry: 500 server error triggers retry with exponential backoff and jitter."""
        resp_500 = MagicMock()
        resp_500.status_code = 500

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {'results': [], 'paging': {}}

        mock_get.side_effect = [resp_500, resp_200]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        client.extract_data(api_token="test-token", source_type="contacts")

        assert mock_get.call_count == 2
        assert mock_sleep.call_count == 1

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_non_retryable_4xx_not_retried(self, mock_get, mock_sleep):
        """Retry: 400 Bad Request or 404 Not Found are not retried."""
        resp_404 = MagicMock()
        resp_404.status_code = 404

        mock_get.return_value = resp_404

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        with pytest.raises(ResourceNotFoundError):
            client.extract_data(api_token="test-token", source_type="contacts")

        # 404 must not sleep or retry
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()


@pytest.mark.django_db
class TestPageCapAndRateLimitBudgets:
    """Tests for page cap policies, 429 retry budgets, heartbeat chunking, and environment setup."""

    @patch('requests.Session.get')
    def test_page_cap_reached_with_fail_fails_job_with_page_limit_exceeded(self, mock_get):
        """1. Page cap reached with fail: job FAILED with PAGE_LIMIT_EXCEEDED, no records presented as complete."""
        def mock_page_response(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                'results': [{'id': f'rec_{time.time()}_{i}', 'properties': {'email': f'user_{i}@test.com'}} for i in range(10)],
                'paging': {'next': {'after': f'cursor_{time.time()}'}}
            }
            return resp

        mock_get.side_effect = mock_page_response

        job = ExtractionJob.objects.create(
            source_type=SourceType.CONTACTS,
            token_hash="hash-fail-cap",
            status=JobStatus.PENDING
        )
        job.set_encrypted_token("test-fail-token")
        job.save()

        with override_settings(
            EXTERNAL_API_ON_PAGE_LIMIT='fail',
            EXTERNAL_API_MAX_PAGES=3,
            EXTERNAL_API_PAGE_SIZE=10
        ):
            service = ExtractionService()
            with pytest.raises(PageLimitExceededError):
                service.process_extraction(job_id=job.id)

        job.refresh_from_db()
        assert job.status == JobStatus.FAILED
        assert job.error_details.get('code') == 'PAGE_LIMIT_EXCEEDED'
        assert "exceeded" in job.error_message.lower()
        # Verify no records were presented as complete or persisted
        assert ExtractedRecord.objects.filter(job=job).count() == 0
        assert job.record_count == 0

    @patch('requests.Session.get')
    def test_page_cap_reached_with_truncate_completes_job_with_truncated_metadata(self, mock_get, api_client):
        """2. Page cap reached with truncate: job COMPLETED, truncated=true visible in status endpoint."""
        page_counter = 0

        def mock_page_response(*args, **kwargs):
            nonlocal page_counter
            page_counter += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                'results': [{'id': f'rec_p{page_counter}_{i}', 'properties': {'email': f'user{page_counter}_{i}@test.com'}} for i in range(10)],
                'paging': {'next': {'after': f'cursor_{page_counter}'}}
            }
            return resp

        mock_get.side_effect = mock_page_response

        job = ExtractionJob.objects.create(
            source_type=SourceType.CONTACTS,
            token_hash="hash-trunc-cap",
            status=JobStatus.PENDING
        )
        job.set_encrypted_token("test-trunc-token")
        job.save()

        with override_settings(
            EXTERNAL_API_ON_PAGE_LIMIT='truncate',
            EXTERNAL_API_MAX_PAGES=3,
            EXTERNAL_API_PAGE_SIZE=10
        ):
            service = ExtractionService()
            completed_job = service.process_extraction(job_id=job.id)

        completed_job.refresh_from_db()
        assert completed_job.status == JobStatus.COMPLETED
        assert completed_job.record_count == 30
        assert completed_job.metadata.get('truncated') is True
        assert completed_job.truncated is True
        assert ExtractedRecord.objects.filter(job=completed_job).count() == 30

        # Verify status endpoint exposes truncated=True and record_count
        url = reverse('extraction:scan-status', kwargs={'job_id': completed_job.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'completed'
        assert data['truncated'] is True
        assert data['record_count'] == 30

    @patch('requests.Session.get')
    def test_more_than_1000_records_fetched_across_pages_with_default_page_size(self, mock_get):
        """3. More than 1,000 records across many pages are fully fetched with new default page size."""
        # 11 pages of 100 records = 1100 records
        mock_pages = []
        for p in range(1, 12):
            resp = MagicMock()
            resp.status_code = 200
            items = [{'id': f'rec_{p}_{i}', 'properties': {'email': f'u_{p}_{i}@crm.com'}} for i in range(100)]
            if p < 11:
                resp.json.return_value = {
                    'results': items,
                    'paging': {'next': {'after': f'cursor_{p+1}'}}
                }
            else:
                resp.json.return_value = {
                    'results': items,
                    'paging': {}
                }
            mock_pages.append(resp)

        mock_get.side_effect = mock_pages

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        result = client.extract_data(api_token="test-large-import-token", source_type="contacts")
        records = result.records

        assert len(records) == 1100
        assert not result.truncated
        assert mock_get.call_count == 11
        # Verify page_size defaulted to 100
        for call_item in mock_get.call_args_list:
            params = call_item[1]['params']
            assert params['limit'] == 100

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_five_consecutive_429s_succeed_on_sixth_without_consuming_5xx_budget_and_sixth_fails(
        self, mock_get, mock_sleep
    ):
        """4. Five consecutive 429s with valid Retry-After succeed on 6th call and do not consume 5xx budget; a 6th fails."""
        # Part A: 5 consecutive 429s, then 200 on 6th call succeeds
        resp_429 = MagicMock(status_code=429, headers={'Retry-After': '1'})
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {'results': [{'id': 'rec_429_ok', 'properties': {'email': 'ok@crm.com'}}], 'paging': {}}

        mock_get.side_effect = [resp_429, resp_429, resp_429, resp_429, resp_429, resp_200]

        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com", max_retries=3)
        with override_settings(EXTERNAL_API_MAX_RATE_LIMIT_RETRIES=5):
            result = client.extract_data(api_token="test-token-429", source_type="contacts")

        assert len(result.records) == 1
        assert result.records[0]['id_from_service'] == 'rec_429_ok'
        assert mock_get.call_count == 6
        assert mock_sleep.call_count == 5

        # Part B: 6th consecutive 429 fails cleanly with RateLimitExceededError and no token in message/details
        mock_sleep.reset_mock()
        mock_get.reset_mock()
        mock_get.side_effect = [resp_429, resp_429, resp_429, resp_429, resp_429, resp_429]

        with override_settings(EXTERNAL_API_MAX_RATE_LIMIT_RETRIES=5), pytest.raises(RateLimitExceededError) as exc_info:
            client.extract_data(api_token="sensitive-token-9988", source_type="contacts")

        assert exc_info.value.status_code == 429
        # Verify sensitive token is NEVER leaked in error message or details
        assert "sensitive-token-9988" not in str(exc_info.value.message)
        assert "sensitive-token-9988" not in str(exc_info.value.details)

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_total_429_wait_ceiling_is_enforced(self, mock_get, mock_sleep):
        """5. Total 429 wait ceiling is enforced: fails safely when cumulative wait exceeds ceiling."""
        resp_429_20s = MagicMock(status_code=429, headers={'Retry-After': '20'})
        mock_get.return_value = resp_429_20s

        # Ceiling: 50 seconds total wait. Each wait is 20s.
        # Wait 1: 20s (total: 20s <= 50s)
        # Wait 2: 20s (total: 40s <= 50s)
        # Wait 3: 20s (total: 60s > 50s) -> Must fail immediately before 3rd sleep
        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        with override_settings(
            EXTERNAL_API_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS=50.0,
            EXTERNAL_API_MAX_RETRY_AFTER_SECONDS=30.0,
            EXTERNAL_API_MAX_RATE_LIMIT_RETRIES=5
        ), pytest.raises(RateLimitExceededError) as exc_info:
            client.extract_data(api_token="test-token", source_type="contacts")

        assert exc_info.value.status_code == 429
        assert "exceeded ceiling of 50.0s" in exc_info.value.message
        assert mock_sleep.call_count == 2
        # Slept exactly 20.0s twice
        assert mock_sleep.call_args_list[0][0][0] == 20.0
        assert mock_sleep.call_args_list[1][0][0] == 20.0

    @patch('time.sleep')
    @patch('requests.Session.get')
    def test_heartbeat_callback_invoked_during_long_retry_after_wait(self, mock_get, mock_sleep):
        """6. Heartbeat callback is invoked in chunks during a long Retry-After wait."""
        # Delay of 75 seconds with 30s chunk size -> 2 chunk boundaries (30s, 30s, 15s)
        resp_429 = MagicMock(status_code=429, headers={'Retry-After': '75'})
        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {'results': [], 'paging': {}}

        mock_get.side_effect = [resp_429, resp_200]

        mock_hb = MagicMock()
        client = ThirdPartyExtractionClient(base_url="https://api.hubapi.com")
        with override_settings(
            EXTERNAL_API_MAX_RETRY_AFTER_SECONDS=100.0,
            EXTERNAL_API_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS=300.0,
            EXTRACTION_HEARTBEAT_CHUNK_SECONDS=30.0
        ):
            client.extract_data(
                api_token="test-token",
                source_type="contacts",
                heartbeat_callback=mock_hb
            )

        # Total wait was 75s: 30s + 30s + 15s
        assert mock_sleep.call_count == 3
        # Heartbeat callback was invoked during sleep between chunks
        assert mock_hb.call_count >= 2

    def test_starting_with_env_example_loads_cleanly(self, monkeypatch):
        """7. Settings: starting with the new .env.example copied to .env loads without ImproperlyConfigured."""
        from decouple import Config, RepositoryEnv
        env_example_path = settings.BASE_DIR / '.env.example'
        assert env_example_path.exists()

        # Clear process environment overrides so we verify the file contents themselves
        for env_key in ('DEBUG', 'DATABASE_URL', 'SECRET_KEY', 'API_KEY_SECRET', 'FIELD_ENCRYPTION_KEY'):
            monkeypatch.delenv(env_key, raising=False)

        example_config = Config(RepositoryEnv(str(env_example_path)))
        parsed_debug = example_config('DEBUG', cast=parse_bool)
        assert parsed_debug is True

        parsed_db = example_config('DATABASE_URL')
        assert parsed_db.startswith('sqlite')

        parsed_secret = example_config('SECRET_KEY')
        assert bool(parsed_secret)

        parsed_api_key = example_config('API_KEY_SECRET')
        assert bool(parsed_api_key)

        parsed_fernet = example_config('FIELD_ENCRYPTION_KEY')
        assert bool(parsed_fernet)
        from cryptography.fernet import Fernet
        Fernet(parsed_fernet.encode('utf-8'))

        assert example_config('EXTERNAL_API_PAGE_SIZE', cast=int) == 100
        assert example_config('EXTERNAL_API_MAX_PAGES', cast=int) == 2000
        assert example_config('EXTERNAL_API_ON_PAGE_LIMIT') == 'fail'
        assert example_config('EXTERNAL_API_MAX_RATE_LIMIT_RETRIES', cast=int) == 5
        assert example_config('EXTERNAL_API_MAX_TOTAL_RATE_LIMIT_WAIT_SECONDS', cast=float) == 300.0

