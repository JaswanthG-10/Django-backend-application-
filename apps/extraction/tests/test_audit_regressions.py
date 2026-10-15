"""Regression tests verifying audit fixes and stability requirements."""
import pytest
from unittest.mock import MagicMock, patch
from io import StringIO
from django.urls import reverse
from django.utils import timezone
from django.core.management import call_command
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
