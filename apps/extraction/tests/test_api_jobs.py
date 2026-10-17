"""Tests for job listing, filtering, and aggregated statistics endpoints."""
import pytest
from django.urls import reverse
from rest_framework import status

from apps.extraction.models import JobStatus


@pytest.mark.django_db
class TestJobManagementApi:
    def test_list_all_jobs(
        self,
        api_client,
        seeded_pending_job,
        seeded_in_progress_job,
        seeded_completed_job,
        seeded_failed_job,
        seeded_cancelled_job
    ):
        """Step 4: List All Jobs (/api/v1/jobs/jobs)."""
        url = reverse('extraction:jobs-list')
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data['count'] >= 5
        returned_ids = [job['job_id'] for job in data['results']]
        assert str(seeded_pending_job.id) in returned_ids
        assert str(seeded_completed_job.id) in returned_ids
        assert str(seeded_failed_job.id) in returned_ids

    def test_list_jobs_filtering_by_status(self, api_client, seeded_pending_job, seeded_completed_job):
        """Filter jobs list by status."""
        url = reverse('extraction:jobs-list')
        response = api_client.get(f"{url}?status={JobStatus.COMPLETED}")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        for item in data['results']:
            assert item['status'] == JobStatus.COMPLETED

    def test_list_jobs_filtering_by_source_type(self, api_client, seeded_in_progress_job):
        """Filter jobs list by source_type."""
        url = reverse('extraction:jobs-list')
        response = api_client.get(f"{url}?source_type=contacts")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        for item in data['results']:
            assert item['source_type'] == 'contacts'

    def test_retrieve_job_statistics(
        self,
        api_client,
        seeded_pending_job,
        seeded_in_progress_job,
        seeded_completed_job,
        seeded_failed_job,
        seeded_cancelled_job
    ):
        """Step 5: Retrieve Job Statistics (/api/v1/jobs/statistics)."""
        url = reverse('extraction:jobs-statistics')
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        stats = response.json()

        assert stats['total_jobs'] >= 5
        assert stats['completed_jobs'] >= 1
        assert stats['failed_jobs'] >= 1
        assert stats['cancelled_jobs'] >= 1
        assert stats['pending_jobs'] >= 1
        assert stats['in_progress_jobs'] >= 1
        assert stats['total_records_extracted'] >= 3
        assert 'average_duration_seconds' in stats
        assert 'success_rate_percentage' in stats

    def test_retrieve_job_analytics(self, api_client, seeded_completed_job):
        """Analytics endpoint returns source breakdown and recent jobs."""
        url = reverse('extraction:jobs-analytics')
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        analytics = response.json()
        assert 'summary' in analytics
        assert 'source_distribution' in analytics
        assert 'recent_activity' in analytics
        assert 'generated_at' in analytics
