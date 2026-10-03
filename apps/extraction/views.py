"""API Views implementing Data Extraction Service endpoints."""
import uuid
import logging
from django.db import connection
from django.utils import timezone
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiResponse

from apps.extraction.models import JobStatus
from apps.extraction.serializers import (
    StartScanRequestSerializer,
    StartScanResponseSerializer,
    JobStatusResponseSerializer,
    ExtractedRecordSerializer,
    JobCancelResponseSerializer,
    JobListSerializer,
    JobStatisticsSerializer,
    JobAnalyticsSerializer,
    HealthResponseSerializer,
)
from apps.extraction.services import ExtractionService
from apps.extraction.selectors import (
    get_job_by_id,
    list_jobs,
    get_job_records,
    calculate_job_statistics,
    calculate_job_analytics,
)
from apps.common.exceptions import (
    ResourceNotFoundError,
    JobIncompleteError,
    BaseDomainException,
)
from apps.common.pagination import StandardLimitOffsetPagination

logger = logging.getLogger('apps.extraction.views')


def parse_uuid_or_404(raw_id: str) -> uuid.UUID:
    """Safely converts string to UUID or raises ResourceNotFoundError."""
    try:
        return uuid.UUID(str(raw_id).strip())
    except (ValueError, AttributeError, TypeError):
        raise ResourceNotFoundError(
            message=f"Invalid or non-existent job ID: '{raw_id}'",
            details={'provided_id': str(raw_id)}
        )


class StartScanView(APIView):
    """Initiates a new data extraction scan from the third-party service."""

    @extend_schema(
        tags=['Scan Operations'],
        summary='Start new data extraction scan',
        description=(
            'Initiates a new asynchronous extraction job using the provided API token. '
            'If an identical scan is already pending or in progress, returns existing job '
            'or 409 Conflict if strict deduplication is requested.'
        ),
        request=StartScanRequestSerializer,
        responses={
            202: OpenApiResponse(response=StartScanResponseSerializer, description='Job accepted and scheduled'),
            400: OpenApiResponse(description='Validation error or missing token'),
            409: OpenApiResponse(description='Active job conflict'),
        }
    )
    def post(self, request, *args, **kwargs):
        # Allow token from Authorization header or X-API-Token if not provided in body
        data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
        if not data.get('api_token'):
            auth_header = request.headers.get('X-API-Token') or request.headers.get('Authorization')
            if auth_header:
                if auth_header.startswith('Bearer '):
                    data['api_token'] = auth_header[7:].strip()
                else:
                    data['api_token'] = auth_header.strip()

        serializer = StartScanRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        validated_data = serializer.validated_data

        api_token = validated_data['api_token']
        source_type = validated_data.get('source_type', 'crm')
        filters = validated_data.get('filters', {})
        strict_dedup = validated_data.get('strict_deduplication', False)

        service = ExtractionService()
        job, created = service.initiate_job(
            api_token=api_token,
            source_type=source_type,
            metadata={'filters': filters},
            deduplicate=True,
            strict_conflict=strict_dedup
        )

        response_data = {
            'job_id': job.id,
            'status': job.status,
            'message': 'Extraction job initiated successfully.' if created else 'Existing active extraction job returned.',
            'is_existing': not created
        }

        return Response(
            StartScanResponseSerializer(response_data).data,
            status=status.HTTP_202_ACCEPTED
        )


class ScanStatusView(APIView):
    """Queries the execution status and progress of a specific extraction job."""

    @extend_schema(
        tags=['Scan Operations'],
        summary='Get extraction job status',
        description='Retrieves current status, record count, and execution progress for a given job.',
        parameters=[
            OpenApiParameter('job_id', str, OpenApiParameter.PATH, description='UUID of the extraction job')
        ],
        responses={
            200: OpenApiResponse(response=JobStatusResponseSerializer, description='Job status details'),
            404: OpenApiResponse(description='Job not found'),
        }
    )
    def get(self, request, job_id, *args, **kwargs):
        parsed_id = parse_uuid_or_404(job_id)
        job = get_job_by_id(parsed_id)
        if not job:
            raise ResourceNotFoundError(
                message=f"Job with ID '{parsed_id}' not found.",
                details={'job_id': str(parsed_id)}
            )

        serializer = JobStatusResponseSerializer(job)
        return Response(serializer.data, status=status.HTTP_200_OK)


class ScanResultView(APIView):
    """Fetches extracted data records for a completed extraction job with pagination."""

    pagination_class = StandardLimitOffsetPagination

    @extend_schema(
        tags=['Scan Operations'],
        summary='Retrieve extracted records for a completed job',
        description=(
            'Returns paginated records for a job. If the job is still pending, in progress, '
            'cancelled, or failed, returns HTTP 409 Conflict.'
        ),
        parameters=[
            OpenApiParameter('job_id', str, OpenApiParameter.PATH, description='UUID of the extraction job'),
            OpenApiParameter('limit', int, OpenApiParameter.QUERY, description='Number of results to return per page (max 100)'),
            OpenApiParameter('offset', int, OpenApiParameter.QUERY, description='The initial index from which to return the results'),
        ],
        responses={
            200: OpenApiResponse(response=ExtractedRecordSerializer(many=True), description='Paginated extracted records'),
            404: OpenApiResponse(description='Job not found'),
            409: OpenApiResponse(description='Job is not completed yet or terminated prematurely'),
        }
    )
    def get(self, request, job_id, *args, **kwargs):
        parsed_id = parse_uuid_or_404(job_id)
        job = get_job_by_id(parsed_id)
        if not job:
            raise ResourceNotFoundError(
                message=f"Job with ID '{parsed_id}' not found.",
                details={'job_id': str(parsed_id)}
            )

        if job.status != JobStatus.COMPLETED:
            raise JobIncompleteError(
                message=f"Cannot retrieve results for job in state '{job.status}'. Only completed jobs contain extraction results.",
                details={'job_id': str(job.id), 'current_status': job.status}
            )

        records_qs = get_job_records(job.id)
        paginator = self.pagination_class()
        paginated_records = paginator.paginate_queryset(records_qs, request)
        serializer = ExtractedRecordSerializer(paginated_records, many=True)
        return paginator.get_paginated_response(serializer.data)


class ScanCancelView(APIView):
    """Cancels a pending or in-progress extraction job."""

    @extend_schema(
        tags=['Scan Operations'],
        summary='Cancel an active extraction job',
        description=(
            'Cancels a pending or in-progress job. If the job is already completed or failed, '
            'returns HTTP 409 Conflict.'
        ),
        parameters=[
            OpenApiParameter('job_id', str, OpenApiParameter.PATH, description='UUID of the extraction job')
        ],
        responses={
            200: OpenApiResponse(response=JobCancelResponseSerializer, description='Job cancelled successfully'),
            404: OpenApiResponse(description='Job not found'),
            409: OpenApiResponse(description='Job cannot be cancelled from its current state'),
        }
    )
    def post(self, request, job_id, *args, **kwargs):
        parsed_id = parse_uuid_or_404(job_id)
        service = ExtractionService()
        job = service.cancel_job(parsed_id)

        response_data = {
            'job_id': job.id,
            'status': job.status,
            'message': 'Extraction job successfully cancelled.'
        }
        return Response(JobCancelResponseSerializer(response_data).data, status=status.HTTP_200_OK)


class ScanRemoveView(APIView):
    """Permanently deletes an extraction job and all associated records."""

    @extend_schema(
        tags=['Scan Operations'],
        summary='Remove extraction job and stored records',
        description='Deletes the job and cascades removal of all associated extracted data.',
        parameters=[
            OpenApiParameter('job_id', str, OpenApiParameter.PATH, description='UUID of the extraction job')
        ],
        responses={
            204: OpenApiResponse(description='Job and data successfully deleted'),
            404: OpenApiResponse(description='Job not found'),
        }
    )
    def delete(self, request, job_id, *args, **kwargs):
        parsed_id = parse_uuid_or_404(job_id)
        service = ExtractionService()
        service.remove_job_data(parsed_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class JobListJobsView(APIView):
    """Lists all historical and active extraction jobs with filtering and pagination."""

    pagination_class = StandardLimitOffsetPagination

    @extend_schema(
        tags=['Job Analytics & Management'],
        summary='List extraction jobs',
        description='Retrieves a paginated list of extraction jobs with optional filtering by status, source, or date.',
        parameters=[
            OpenApiParameter('status', str, OpenApiParameter.QUERY, description='Filter by job status (pending, in_progress, completed, failed, cancelled)'),
            OpenApiParameter('source_type', str, OpenApiParameter.QUERY, description='Filter by source type (crm, contacts, leads)'),
            OpenApiParameter('search', str, OpenApiParameter.QUERY, description='Search by token mask'),
            OpenApiParameter('limit', int, OpenApiParameter.QUERY, description='Number of results to return per page'),
            OpenApiParameter('offset', int, OpenApiParameter.QUERY, description='The initial index from which to return the results'),
        ],
        responses={
            200: OpenApiResponse(response=JobListSerializer(many=True), description='Paginated job list'),
        }
    )
    def get(self, request, *args, **kwargs):
        status_filter = request.query_params.get('status')
        source_filter = request.query_params.get('source_type')
        search_filter = request.query_params.get('search')

        qs = list_jobs(
            status=status_filter,
            source_type=source_filter,
            search=search_filter
        )

        paginator = self.pagination_class()
        paginated_jobs = paginator.paginate_queryset(qs, request)
        serializer = JobListSerializer(paginated_jobs, many=True)
        return paginator.get_paginated_response(serializer.data)


class JobStatisticsView(APIView):
    """Provides aggregated statistics across all historical and active extraction jobs."""

    @extend_schema(
        tags=['Job Analytics & Management'],
        summary='Get job statistics',
        description='Returns system-wide aggregated metrics including total jobs, status counts, records extracted, and average duration.',
        responses={
            200: OpenApiResponse(response=JobStatisticsSerializer, description='Aggregated metrics'),
        }
    )
    def get(self, request, *args, **kwargs):
        stats = calculate_job_statistics()
        serializer = JobStatisticsSerializer(stats)
        return Response(serializer.data, status=status.HTTP_200_OK)


class JobAnalyticsView(APIView):
    """Provides analytics breakdown for dashboard reporting and data visualization."""

    @extend_schema(
        tags=['Job Analytics & Management'],
        summary='Get extraction analytics and source distribution',
        description='Returns metrics breakdown by source, recent execution history, and duration analysis.',
        responses={
            200: OpenApiResponse(response=JobAnalyticsSerializer, description='Analytics breakdown'),
        }
    )
    def get(self, request, *args, **kwargs):
        analytics = calculate_job_analytics()
        serializer = JobAnalyticsSerializer(analytics)
        return Response(serializer.data, status=status.HTTP_200_OK)


class HealthCheckView(APIView):
    """Operational health check confirming API and database connectivity."""

    @extend_schema(
        tags=['Health'],
        summary='Service health check',
        description='Verifies database connectivity and returns service status.',
        responses={
            200: OpenApiResponse(response=HealthResponseSerializer, description='Service healthy'),
            503: OpenApiResponse(description='Database unhealthy or service degraded'),
        }
    )
    def get(self, request, *args, **kwargs):
        db_status = "healthy"
        status_code = status.HTTP_200_OK

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            logger.error("Health check database failure: %s", exc)
            db_status = "unhealthy"
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE

        payload = {
            'status': 'ok' if db_status == 'healthy' else 'degraded',
            'database': db_status,
            'timestamp': timezone.now().isoformat(),
            'version': '1.0.0'
        }
        return Response(HealthResponseSerializer(payload).data, status=status_code)
