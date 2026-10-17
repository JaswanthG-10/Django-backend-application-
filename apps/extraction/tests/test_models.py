"""Tests for ExtractionJob and ExtractedRecord database models."""
import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.common.exceptions import InvalidStateTransitionError
from apps.extraction.models import ExtractedRecord, ExtractionJob, JobStatus


@pytest.mark.django_db
class TestExtractionJobModel:
    def test_token_hashing_and_masking(self):
        token = "pat-na1-12345678-abcdef"
        h = ExtractionJob.hash_token(token)
        m = ExtractionJob.mask_token(token)

        assert len(h) == 64
        assert m == "pat***cdef"
        assert token not in m

    def test_short_token_masking(self):
        assert ExtractionJob.mask_token("short") == "***"

    def test_valid_state_transitions(self):
        job = ExtractionJob.objects.create(
            token_hash="dummyhash",
            status=JobStatus.PENDING
        )
        assert job.status == JobStatus.PENDING
        assert not job.is_terminal

        # PENDING -> IN_PROGRESS
        job.transition_to(JobStatus.IN_PROGRESS)
        assert job.status == JobStatus.IN_PROGRESS
        assert job.start_time is not None
        assert not job.is_terminal

        # IN_PROGRESS -> COMPLETED
        job.transition_to(JobStatus.COMPLETED, record_count=10)
        assert job.status == JobStatus.COMPLETED
        assert job.record_count == 10
        assert job.end_time is not None
        assert job.is_terminal
        assert job.duration_seconds is not None

    def test_cancellation_from_pending(self):
        job = ExtractionJob.objects.create(
            token_hash="dummyhash",
            status=JobStatus.PENDING
        )
        job.transition_to(JobStatus.CANCELLED)
        assert job.status == JobStatus.CANCELLED
        assert job.is_terminal

    def test_cancellation_from_in_progress(self):
        job = ExtractionJob.objects.create(
            token_hash="dummyhash",
            status=JobStatus.IN_PROGRESS,
            start_time=timezone.now()
        )
        job.transition_to(JobStatus.CANCELLED)
        assert job.status == JobStatus.CANCELLED

    def test_invalid_transition_from_completed_raises(self):
        job = ExtractionJob.objects.create(
            token_hash="dummyhash",
            status=JobStatus.COMPLETED,
            start_time=timezone.now(),
            end_time=timezone.now()
        )
        with pytest.raises(InvalidStateTransitionError):
            job.transition_to(JobStatus.IN_PROGRESS)

    def test_invalid_transition_from_cancelled_raises(self):
        job = ExtractionJob.objects.create(
            token_hash="dummyhash",
            status=JobStatus.CANCELLED
        )
        with pytest.raises(InvalidStateTransitionError):
            job.transition_to(JobStatus.COMPLETED)

    def test_progress_percentage(self):
        job = ExtractionJob(status=JobStatus.PENDING)
        assert job.progress_percentage == 0
        job.status = JobStatus.IN_PROGRESS
        assert job.progress_percentage == 50
        job.status = JobStatus.COMPLETED
        assert job.progress_percentage == 100


@pytest.mark.django_db
class TestExtractedRecordModel:
    def test_record_creation_and_cascade(self, seeded_completed_job):
        record = ExtractedRecord.objects.create(
            job=seeded_completed_job,
            id_from_service="rec_001",
            email="test@user.com",
            first_name="Jane",
            last_name="Doe",
            custom_fields={"role": "admin"}
        )
        assert str(record.job.id) == str(seeded_completed_job.id)
        assert record.id_from_service == "rec_001"

        # Cascade delete test
        seeded_completed_job.delete()
        assert not ExtractedRecord.objects.filter(id=record.id).exists()

    def test_unique_record_per_job_constraint(self, seeded_completed_job):
        ExtractedRecord.objects.create(
            job=seeded_completed_job,
            id_from_service="duplicate_id",
            email="user1@example.com"
        )
        with pytest.raises(IntegrityError):
            ExtractedRecord.objects.create(
                job=seeded_completed_job,
                id_from_service="duplicate_id",
                email="user2@example.com"
            )
