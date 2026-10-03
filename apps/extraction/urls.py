"""URL routing for extraction domain matching GreenTree API test specifications."""
from django.urls import path
from apps.extraction.views import (
    StartScanView,
    ScanStatusView,
    ScanResultView,
    ScanCancelView,
    ScanRemoveView,
    JobListJobsView,
    JobStatisticsView,
    JobAnalyticsView,
    HealthCheckView,
)

app_name = 'extraction'

urlpatterns = [
    # Scan Workflow Endpoints
    path('scan/start', StartScanView.as_view(), name='scan-start'),
    path('scan/status/<str:job_id>', ScanStatusView.as_view(), name='scan-status'),
    path('scan/result/<str:job_id>', ScanResultView.as_view(), name='scan-result'),
    path('scan/cancel/<str:job_id>', ScanCancelView.as_view(), name='scan-cancel'),
    path('scan/remove/<str:job_id>', ScanRemoveView.as_view(), name='scan-remove'),

    # Jobs Analytics & Management Endpoints
    path('jobs/jobs', JobListJobsView.as_view(), name='jobs-list'),
    path('jobs/statistics', JobStatisticsView.as_view(), name='jobs-statistics'),
    path('jobs/analytics', JobAnalyticsView.as_view(), name='jobs-analytics'),

    # Operational Health Endpoint
    path('health', HealthCheckView.as_view(), name='health-check'),
]
