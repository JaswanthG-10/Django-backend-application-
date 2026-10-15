"""Regression tests verifying audit fixes and stability requirements."""
import hashlib
import hmac
import json
import time
import pytest
from unittest.mock import MagicMock, patch
from io import StringIO
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
from django.test import override_settings
from rest_framework import status

from apps.extraction.models import ExtractionJob, ExtractedRecord, JobStatus, SourceType
from apps.extraction.services import ExtractionService
from apps.extraction.clients import ThirdPartyExtractionClient
from apps.common.encryption import encrypt_secret, decrypt_secret


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
        assert job.record_count == 0
        assert ExtractedRecord.objects.filter(job=job).count() == 0
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
        records = client.extract_data(api_token="hubspot-pat-test-token", source_type="contacts")

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
