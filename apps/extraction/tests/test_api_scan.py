"""Integration tests for Scan Workflow endpoints matching GreenTree guidelines."""
import pytest
from rest_framework import status
from django.urls import reverse
from apps.extraction.models import ExtractionJob, JobStatus


@pytest.mark.django_db
class TestScanWorkflowApi:
    def test_health_check_endpoint(self, api_client):
        """Step 6: Health Check (/api/v1/health)."""
        url = reverse('extraction:health-check')
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'ok'
        assert data['database'] == 'healthy'
        assert 'timestamp' in data

    def test_start_scan_success(self, api_client):
        """Step 2: Start New Extraction (/api/v1/scan/start) with body."""
        url = reverse('extraction:scan-start')
        payload = {
            'api_token': 'test-secret-token-12345',
            'source_type': 'crm',
            'filters': {'created_after': '2026-01-01'}
        }
        response = api_client.post(url, data=payload, format='json')
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert 'job_id' in data
        assert data['status'] == 'pending'
        assert data['is_existing'] is False

        # Confirm persisted in DB
        job = ExtractionJob.objects.get(id=data['job_id'])
        assert job.status == JobStatus.PENDING
        assert job.source_type == 'crm'

    def test_start_scan_with_header_token(self, api_client):
        """Step 2 variation: Start scan providing token via X-API-Token header."""
        url = reverse('extraction:scan-start')
        response = api_client.post(
            url,
            data={'source_type': 'contacts'},
            format='json',
            HTTP_X_API_TOKEN='header-token-secret-999'
        )
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert 'job_id' in data

    def test_get_job_status_pending(self, api_client, seeded_pending_job):
        """Step 2: Verify Job Status for pending state."""
        url = reverse('extraction:scan-status', kwargs={'job_id': seeded_pending_job.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['job_id'] == str(seeded_pending_job.id)
        assert data['status'] == 'pending'
        assert data['record_count'] == 0
        assert data['progress_percentage'] == 0

    def test_get_job_status_completed(self, api_client, seeded_completed_job):
        """Step 2: Verify Job Status for completed state with metrics."""
        url = reverse('extraction:scan-status', kwargs={'job_id': seeded_completed_job.id})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'completed'
        assert data['record_count'] == seeded_completed_job.record_count
        assert data['start_time'] is not None
        assert data['end_time'] is not None
        assert data['progress_percentage'] == 100

    def test_fetch_extraction_results(self, api_client, seeded_completed_job_with_records):
        """Step 3: Fetch Extraction Results (/api/v1/scan/result/<job_id>)."""
        job, records = seeded_completed_job_with_records
        url = reverse('extraction:scan-result', kwargs={'job_id': job.id})

        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()

        assert 'count' in data
        assert data['count'] == 5
        assert len(data['results']) == 5

        # Check fields of extracted records
        first_record = data['results'][0]
        assert 'id' in first_record
        assert 'id_from_service' in first_record
        assert 'email' in first_record
        assert 'first_name' in first_record
        assert 'last_name' in first_record
        assert 'custom_fields' in first_record

    def test_fetch_extraction_results_pagination(self, api_client, seeded_completed_job_with_records):
        """Step 3: Verify pagination on extraction results."""
        job, records = seeded_completed_job_with_records
        url = reverse('extraction:scan-result', kwargs={'job_id': job.id})

        # Request limit=2, offset=0
        response = api_client.get(f"{url}?limit=2&offset=0")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert len(data['results']) == 2
        assert data['count'] == 5
        assert data['next'] is not None
        assert data['previous'] is None

        # Request limit=2, offset=2
        response_page2 = api_client.get(f"{url}?limit=2&offset=2")
        assert response_page2.status_code == status.HTTP_200_OK
        data_page2 = response_page2.json()
        assert len(data_page2['results']) == 2
        assert data_page2['previous'] is not None

    def test_cancel_pending_job(self, api_client, seeded_pending_job):
        """Step 7: Cancel a Pending Job (/api/v1/scan/cancel/<job_id>)."""
        url = reverse('extraction:scan-cancel', kwargs={'job_id': seeded_pending_job.id})
        response = api_client.post(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['status'] == 'cancelled'

        # Follow up with status check
        status_url = reverse('extraction:scan-status', kwargs={'job_id': seeded_pending_job.id})
        status_resp = api_client.get(status_url)
        assert status_resp.status_code == status.HTTP_200_OK
        assert status_resp.json()['status'] == 'cancelled'

    def test_remove_job_data_and_verify_404(self, api_client, seeded_completed_job_with_records):
        """Step 8: Remove Job Data (/api/v1/scan/remove/<job_id>) and check 404."""
        job, records = seeded_completed_job_with_records
        remove_url = reverse('extraction:scan-remove', kwargs={'job_id': job.id})

        response = api_client.delete(remove_url)
        assert response.status_code == status.HTTP_204_NO_CONTENT

        # Status check should now return 404
        status_url = reverse('extraction:scan-status', kwargs={'job_id': job.id})
        assert api_client.get(status_url).status_code == status.HTTP_404_NOT_FOUND

        # Results check should also return 404
        result_url = reverse('extraction:scan-result', kwargs={'job_id': job.id})
        assert api_client.get(result_url).status_code == status.HTTP_404_NOT_FOUND
