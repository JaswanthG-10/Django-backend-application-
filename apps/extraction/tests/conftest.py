import socket

import pytest
from django.conf import settings
from rest_framework.test import APIClient

from apps.extraction.tests.factories import (
    CancelledExtractionJobFactory,
    CompletedExtractionJobFactory,
    ExtractedRecordFactory,
    ExtractionJobFactory,
    FailedExtractionJobFactory,
    InProgressExtractionJobFactory,
)


@pytest.fixture(autouse=True)
def disable_async_extraction_for_unit_tests(settings):
    """Disables background worker threads by default during unit tests for deterministic assertions."""
    settings.EXTRACTION_ASYNC_EXECUTION = False
    settings.SECURE_SSL_REDIRECT = False


@pytest.fixture(autouse=True)
def mock_dns_for_offline_test_domains(monkeypatch):
    """Provides deterministic mock DNS resolution for standard test domains in offline unit tests."""
    original_getaddrinfo = socket.getaddrinfo

    test_domain_ips = {
        'api.hubapi.com': '199.60.103.28',
        'unintended-external-service.org': '93.184.215.14',
        'crm-provider.test': '93.184.215.15',
        'api.crm-provider.test': '93.184.215.15',
        'api.thirdparty-crm.com': '93.184.215.17',
        'jsonplaceholder.typicode.com': '104.21.4.197',
        'crm.myenterprise.com': '93.184.215.16',
    }

    def _test_getaddrinfo(host, port=None, *args, **kwargs):
        clean_host = (host or '').lower().strip()
        if clean_host in test_domain_ips:
            resolved_ip = test_domain_ips[clean_host]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (resolved_ip, port or 443))]
        return original_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', _test_getaddrinfo)


@pytest.fixture
def api_client():
    """Provides a fresh DRF APIClient authenticated with the service API key."""
    client = APIClient()
    key = getattr(settings, 'API_KEY_SECRET', 'test-only-api-key')
    client.credentials(HTTP_X_API_KEY=key)
    return client


@pytest.fixture
def unauthenticated_client():
    """Provides an unauthenticated DRF APIClient without the service API key."""
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
