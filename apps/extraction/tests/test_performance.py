"""Performance and N+1 query prevention tests."""
import pytest
from django.urls import reverse
from apps.extraction.tests.factories import (
    CompletedExtractionJobFactory,
    ExtractedRecordFactory,
)


@pytest.mark.django_db
class TestQueryPerformance:
    def test_scan_results_query_efficiency(self, api_client, django_assert_num_queries):
        """Verify that fetching results uses constant queries regardless of record count (no N+1)."""
        job = CompletedExtractionJobFactory(record_count=10)
        for i in range(10):
            ExtractedRecordFactory(job=job, id_from_service=f"perf_{i}")

        url = reverse('extraction:scan-result', kwargs={'job_id': job.id})

        # 1 query for job lookup + 1 query for count + 1 query for paginated results
        with django_assert_num_queries(3):
            response = api_client.get(f"{url}?limit=10&offset=0")
            assert response.status_code == 200
            assert len(response.json()['results']) == 10

    def test_job_list_query_efficiency(self, api_client, django_assert_num_queries):
        """Verify that listing jobs uses constant queries (no N+1)."""
        for i in range(15):
            CompletedExtractionJobFactory(record_count=i)

        url = reverse('extraction:jobs-list')

        # 1 query for count + 1 query for sliced results
        with django_assert_num_queries(2):
            response = api_client.get(f"{url}?limit=10&offset=0")
            assert response.status_code == 200
            assert len(response.json()['results']) == 10
