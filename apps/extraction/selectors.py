"""Database query selectors and aggregations for Extraction domain."""
from typing import Optional, Dict, Any
from uuid import UUID
from django.db.models import QuerySet, Count, Sum, F, ExpressionWrapper, DurationField, Avg
from django.db.models.functions import Coalesce
from django.utils import timezone
from apps.extraction.models import ExtractionJob, ExtractedRecord, JobStatus


def get_job_by_id(job_id: UUID) -> Optional[ExtractionJob]:
    """Retrieves a single job by UUID or returns None."""
    try:
        return ExtractionJob.objects.filter(id=job_id).first()
    except (ValueError, TypeError):
        return None


def get_active_job_by_token_hash(token_hash: str, source_type: str = 'crm') -> Optional[ExtractionJob]:
    """Finds an existing active (pending or in_progress) job for deduplication."""
    return ExtractionJob.objects.filter(
        token_hash=token_hash,
        source_type=source_type,
        status__in=[JobStatus.PENDING, JobStatus.IN_PROGRESS]
    ).first()


def list_jobs(
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    date_from: Optional[Any] = None,
    date_to: Optional[Any] = None,
    search: Optional[str] = None
) -> QuerySet[ExtractionJob]:
    """Returns an optimized queryset of extraction jobs with optional filtering."""
    qs = ExtractionJob.objects.all().order_by('-created_at')

    if status:
        qs = qs.filter(status=status)
    if source_type:
        qs = qs.filter(source_type=source_type)
    if date_from:
        qs = qs.filter(created_at__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__lte=date_to)
    if search:
        qs = qs.filter(masked_token__icontains=search)

    return qs


def get_job_records(job_id: UUID) -> QuerySet[ExtractedRecord]:
    """Returns an optimized queryset of records for a given job."""
    return ExtractedRecord.objects.filter(job_id=job_id).select_related('job').order_by('id_from_service')


def calculate_job_statistics() -> Dict[str, Any]:
    """Computes system-wide aggregate metrics using database queries."""
    total_jobs = ExtractionJob.objects.count()

    status_counts = dict(
        ExtractionJob.objects.values('status')
        .annotate(count=Count('id'))
        .values_list('status', 'count')
    )

    completed = status_counts.get(JobStatus.COMPLETED, 0)
    failed = status_counts.get(JobStatus.FAILED, 0)
    cancelled = status_counts.get(JobStatus.CANCELLED, 0)
    pending = status_counts.get(JobStatus.PENDING, 0)
    in_progress = status_counts.get(JobStatus.IN_PROGRESS, 0)

    total_records = ExtractionJob.objects.aggregate(
        total=Coalesce(Sum('record_count'), 0)
    )['total']

    # Average duration for completed jobs
    completed_jobs = ExtractionJob.objects.filter(
        status=JobStatus.COMPLETED,
        start_time__isnull=False,
        end_time__isnull=False
    )

    durations = [
        (job.end_time - job.start_time).total_seconds()
        for job in completed_jobs
        if job.end_time and job.start_time and job.end_time >= job.start_time
    ]

    avg_duration = round(sum(durations) / len(durations), 2) if durations else 0.0
    success_rate = round((completed / total_jobs * 100), 2) if total_jobs > 0 else 0.0

    return {
        'total_jobs': total_jobs,
        'completed_jobs': completed,
        'failed_jobs': failed,
        'cancelled_jobs': cancelled,
        'pending_jobs': pending,
        'in_progress_jobs': in_progress,
        'total_records_extracted': total_records,
        'average_duration_seconds': avg_duration,
        'success_rate_percentage': success_rate,
    }


def calculate_job_analytics() -> Dict[str, Any]:
    """Produces detailed analytics breakdown for visualization and reporting."""
    stats = calculate_job_statistics()

    source_breakdown = dict(
        ExtractionJob.objects.values('source_type')
        .annotate(count=Count('id'))
        .values_list('source_type', 'count')
    )

    recent_jobs = list(
        ExtractionJob.objects.order_by('-created_at')[:5]
        .values('id', 'status', 'source_type', 'record_count', 'created_at')
    )

    return {
        'summary': stats,
        'source_distribution': source_breakdown,
        'recent_activity': recent_jobs,
        'generated_at': timezone.now().isoformat()
    }
