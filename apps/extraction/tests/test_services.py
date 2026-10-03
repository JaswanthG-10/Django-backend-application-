"""Tests for ExtractionService orchestration and business rules."""
import uuid
from unittest.mock import MagicMock
import pytest
from apps.extraction.models import ExtractionJob, ExtractedRecord, JobStatus
from apps.extraction.services import ExtractionService
from apps.common.exceptions import (
    ResourceNotFoundError,
    InvalidStateTransitionError,
    DuplicateJobConflictError,
    ExternalServiceError,
)


@pytest.mark.django_db
class TestExtractionService:
    def test_initiate_job_creates_new(self):
        service = ExtractionService()
        job, created = service.initiate_job(
            api_token="unique-token-abc-123",
            source_type="crm"
        )
        assert created is True
        assert job.status == JobStatus.PENDING
        assert job.token_hash == ExtractionJob.hash_token("unique-token-abc-123")

    def test_initiate_job_reuses_active_duplicate(self):
        service = ExtractionService()
        job1, created1 = service.initiate_job(
            api_token="duplicate-token-xyz",
            source_type="crm"
        )
        assert created1 is True

        job2, created2 = service.initiate_job(
            api_token="duplicate-token-xyz",
            source_type="crm",
            deduplicate=True,
            strict_conflict=False
        )
        assert created2 is False
        assert job1.id == job2.id

    def test_initiate_job_strict_conflict_raises(self):
        service = ExtractionService()
        service.initiate_job(
            api_token="strict-token-abc",
            source_type="crm"
        )
        with pytest.raises(DuplicateJobConflictError):
            service.initiate_job(
                api_token="strict-token-abc",
                source_type="crm",
                deduplicate=True,
                strict_conflict=True
            )

    def test_cancel_pending_job(self, seeded_pending_job):
        service = ExtractionService()
        cancelled_job = service.cancel_job(seeded_pending_job.id)
        assert cancelled_job.status == JobStatus.CANCELLED
        assert cancelled_job.error_message == "Job cancelled by user request."

    def test_cancel_completed_job_raises_error(self, seeded_completed_job):
        service = ExtractionService()
        with pytest.raises(InvalidStateTransitionError):
            service.cancel_job(seeded_completed_job.id)

    def test_cancel_nonexistent_job_raises_404(self):
        service = ExtractionService()
        with pytest.raises(ResourceNotFoundError):
            service.cancel_job(uuid.uuid4())

    def test_remove_job_data(self, seeded_completed_job_with_records):
        job, records = seeded_completed_job_with_records
        service = ExtractionService()
        service.remove_job_data(job.id)

        assert not ExtractionJob.objects.filter(id=job.id).exists()
        assert not ExtractedRecord.objects.filter(job_id=job.id).exists()

    def test_process_extraction_success(self, seeded_pending_job, sample_external_api_payload):
        mock_client = MagicMock()
        mock_client.extract_data.return_value = sample_external_api_payload

        service = ExtractionService(client=mock_client)
        completed_job = service.process_extraction(
            job_id=seeded_pending_job.id,
            api_token="valid-test-token"
        )

        assert completed_job.status == JobStatus.COMPLETED
        assert completed_job.record_count == 3
        assert ExtractedRecord.objects.filter(job=completed_job).count() == 3

        first_record = ExtractedRecord.objects.get(job=completed_job, id_from_service="hubspot_101")
        assert first_record.email == "sarah.connor@cyberdyne.org"
        assert first_record.first_name == "Sarah"

    def test_process_extraction_failure_updates_status(self, seeded_pending_job):
        mock_client = MagicMock()
        mock_client.extract_data.side_effect = ExternalServiceError("Provider 500 error", details={"code": 500})

        service = ExtractionService(client=mock_client)
        with pytest.raises(ExternalServiceError):
            service.process_extraction(
                job_id=seeded_pending_job.id,
                api_token="valid-test-token"
            )

        job = ExtractionJob.objects.get(id=seeded_pending_job.id)
        assert job.status == JobStatus.FAILED
        assert "Provider 500 error" in job.error_message
        assert job.error_details == {"code": 500}
