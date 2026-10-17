"""Comprehensive tests covering all edge cases specified in Section 7 of GreenTree guidelines."""
import uuid

import pytest
from django.urls import reverse
from rest_framework import status


@pytest.mark.django_db
class TestEdgeCases:
    def test_start_scan_missing_token(self, api_client):
        """Edge case: Start extraction with missing API token."""
        url = reverse('extraction:scan-start')
        response = api_client.post(url, data={}, format='json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        data = response.json()
        assert 'error' in data
        assert data['error']['code'] == 'VALIDATION_ERROR'

    def test_start_scan_empty_or_whitespace_token(self, api_client):
        """Edge case: Start extraction with whitespace-only token."""
        url = reverse('extraction:scan-start')
        response = api_client.post(url, data={'api_token': '   '}, format='json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_start_scan_too_short_token(self, api_client):
        """Edge case: Start extraction with short malformed token."""
        url = reverse('extraction:scan-start')
        response = api_client.post(url, data={'api_token': 'abc'}, format='json')
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_non_existent_job_id_status(self, api_client):
        """Edge case: Request status for non-existent job UUID."""
        random_id = uuid.uuid4()
        url = reverse('extraction:scan-status', kwargs={'job_id': random_id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()['error']['code'] == 'RESOURCE_NOT_FOUND'

    def test_non_existent_job_id_result(self, api_client):
        """Edge case: Request results for non-existent job UUID."""
        random_id = uuid.uuid4()
        url = reverse('extraction:scan-result', kwargs={'job_id': random_id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_non_existent_job_id_cancel(self, api_client):
        """Edge case: Cancel non-existent job UUID."""
        random_id = uuid.uuid4()
        url = reverse('extraction:scan-cancel', kwargs={'job_id': random_id})
        response = api_client.post(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_non_existent_job_id_remove(self, api_client):
        """Edge case: Remove non-existent job UUID."""
        random_id = uuid.uuid4()
        url = reverse('extraction:scan-remove', kwargs={'job_id': random_id})
        response = api_client.delete(url)
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_id_injection_patterns_handled_safely(self, api_client):
        """Edge case: ID injection with SQL/NoSQL or special characters."""
        malicious_id = "'; DROP TABLE extraction_jobs; --"
        url = reverse('extraction:scan-status', kwargs={'job_id': malicious_id})
        response = api_client.get(url)
        # Safely rejected as 404 without internal server 500 error or SQL injection
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_accessing_results_for_pending_job_conflicts(self, api_client, seeded_pending_job):
        """Edge case: Fetching results for incomplete (pending) job -> 409 Conflict."""
        url = reverse('extraction:scan-result', kwargs={'job_id': seeded_pending_job.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_409_CONFLICT
        data = response.json()
        assert data['error']['code'] == 'JOB_NOT_COMPLETED'
        assert "Cannot retrieve results for job in state 'pending'" in data['error']['message']

    def test_accessing_results_for_in_progress_job_conflicts(self, api_client, seeded_in_progress_job):
        """Edge case: Fetching results for in-progress job -> 409 Conflict."""
        url = reverse('extraction:scan-result', kwargs={'job_id': seeded_in_progress_job.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.json()['error']['code'] == 'JOB_NOT_COMPLETED'

    def test_accessing_results_for_failed_job_conflicts(self, api_client, seeded_failed_job):
        """Edge case: Fetching results for failed job -> 409 Conflict."""
        url = reverse('extraction:scan-result', kwargs={'job_id': seeded_failed_job.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_cancelling_already_completed_job_conflicts(self, api_client, seeded_completed_job):
        """Edge case: Cancelling an already completed job -> 409 Conflict."""
        url = reverse('extraction:scan-cancel', kwargs={'job_id': seeded_completed_job.id})
        response = api_client.post(url)
        assert response.status_code == status.HTTP_409_CONFLICT
        data = response.json()
        assert "Cannot cancel job in state 'completed'" in data['error']['message']

    def test_cancelling_already_failed_job_conflicts(self, api_client, seeded_failed_job):
        """Edge case: Cancelling an already failed job -> 409 Conflict."""
        url = reverse('extraction:scan-cancel', kwargs={'job_id': seeded_failed_job.id})
        response = api_client.post(url)
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_cancelling_already_cancelled_job_conflicts(self, api_client, seeded_cancelled_job):
        """Edge case: Cancelling an already cancelled job -> 409 Conflict."""
        url = reverse('extraction:scan-cancel', kwargs={'job_id': seeded_cancelled_job.id})
        response = api_client.post(url)
        assert response.status_code == status.HTTP_409_CONFLICT

    def test_concurrent_scan_strict_conflict(self, api_client, seeded_pending_job):
        """Edge case: Concurrent request with same token and strict_deduplication=True -> 409."""
        url = reverse('extraction:scan-start')
        # seeded_pending_job uses "test-secret-api-token"
        payload = {
            'api_token': 'test-secret-api-token',
            'source_type': seeded_pending_job.source_type,
            'strict_deduplication': True
        }
        response = api_client.post(url, data=payload, format='json')
        assert response.status_code == status.HTTP_409_CONFLICT
        data = response.json()
        assert data['error']['code'] == 'DUPLICATE_JOB_ACTIVE'

    def test_malformed_json_body(self, api_client):
        """Edge case: Malformed body payload -> 400 Bad Request."""
        url = reverse('extraction:scan-start')
        response = api_client.post(
            url,
            data="{invalid_json: 123",
            content_type="application/json"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_pagination_stress_and_bounds(self, api_client, seeded_completed_job_with_records):
        """Edge case: Pagination with extreme or invalid values."""
        job, _records = seeded_completed_job_with_records
        url = reverse('extraction:scan-result', kwargs={'job_id': job.id})

        # Bounded to max limit (100)
        resp = api_client.get(f"{url}?limit=500&offset=0")
        assert resp.status_code == status.HTTP_200_OK
        assert len(resp.json()['results']) == 5

        # Offset beyond total items
        resp_out_of_bounds = api_client.get(f"{url}?limit=10&offset=9999")
        assert resp_out_of_bounds.status_code == status.HTTP_200_OK
        assert len(resp_out_of_bounds.json()['results']) == 0
