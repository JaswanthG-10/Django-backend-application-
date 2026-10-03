"""Pytest fixtures and seeded test scenarios."""
import pytest
from rest_framework.test import APIClient
from apps.extraction.tests.factories import (
    ExtractionJobFactory,
    CompletedExtractionJobFactory,
    InProgressExtractionJobFactory,
    FailedExtractionJobFactory,
    CancelledExtractionJobFactory,
    ExtractedRecordFactory,
)


@pytest.fixture
def api_client():
    """Provides a fresh DRF APIClient."""
    return APIClient()


@pytest.fixture
def seeded_pending_job(db):
    """A deterministic pending job."""
    return ExtractionJobFactory(source_type='crm')


@pytest.fixture
def seeded_in_progress_job(db):
    """A deterministic in-progress job."""
    return InProgressExtractionJobFactory(source_type='contacts')


@pytest.fixture
def seeded_completed_job(db):
    """A deterministic completed job."""
    return CompletedExtractionJobFactory(source_type='crm', record_count=3)


@pytest.fixture
def seeded_failed_job(db):
    """A deterministic failed job."""
    return FailedExtractionJobFactory(source_type='leads')


@pytest.fixture
def seeded_cancelled_job(db):
    """A deterministic cancelled job."""
    return CancelledExtractionJobFactory(source_type='crm')


@pytest.fixture
def seeded_completed_job_with_records(db, seeded_completed_job):
    """Completed job with 5 seeded records."""
    records = [
        ExtractedRecordFactory(
            job=seeded_completed_job,
            id_from_service=f"srv_id_{i}",
            email=f"contact_{i}@domain.com",
            first_name=f"First_{i}",
            last_name=f"Last_{i}",
            custom_fields={"score": 100 + i, "tier": "gold"}
        )
        for i in range(1, 6)
    ]
    seeded_completed_job.record_count = len(records)
    seeded_completed_job.save(update_fields=['record_count'])
    return seeded_completed_job, records


@pytest.fixture
def sample_external_api_payload():
    """Sample mock payload mimicking a real third-party CRM provider."""
    return [
        {
            "id": "hubspot_101",
            "email": "sarah.connor@cyberdyne.org",
            "first_name": "Sarah",
            "last_name": "Connor",
            "properties": {"lifecyclestage": "customer", "lead_score": 95}
        },
        {
            "id": "hubspot_102",
            "email": "john.connor@resistance.net",
            "first_name": "John",
            "last_name": "Connor",
            "properties": {"lifecyclestage": "lead", "lead_score": 99}
        },
        {
            "id": "hubspot_103",
            "email": "t800@cyberdyne.org",
            "first_name": "Model",
            "last_name": "101",
            "properties": {"lifecyclestage": "prospect", "lead_score": 50}
        }
    ]
