"""Business service layer for managing extraction job workflows."""
import logging
import threading
import atexit
from concurrent.futures import ThreadPoolExecutor
from typing import Tuple, Optional, Dict, Any
from uuid import UUID
from django.conf import settings
from django.db import transaction, connection
from django.utils import timezone
from apps.extraction.models import ExtractionJob, ExtractedRecord, JobStatus, SourceType
from apps.extraction.selectors import get_job_by_id, get_active_job_by_dedup_hash
from apps.extraction.clients import ThirdPartyExtractionClient
from apps.common.exceptions import (
    ResourceNotFoundError,
    InvalidStateTransitionError,
    DuplicateJobConflictError,
    BaseDomainException,
)

logger = logging.getLogger('apps.extraction.services')

# Bounded thread pool executor for managed asynchronous execution in production
_extraction_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def get_extraction_executor() -> ThreadPoolExecutor:
    """Returns the managed, bounded ThreadPoolExecutor for background extraction tasks."""
    global _extraction_executor
    if _extraction_executor is None:
        with _executor_lock:
            if _extraction_executor is None:
                max_workers = getattr(settings, 'EXTRACTION_MAX_WORKERS', 4)
                _extraction_executor = ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="extraction-worker"
                )
                atexit.register(shutdown_extraction_executor)
                logger.info("Initialized bounded extraction thread pool with max_workers=%d", max_workers)
    return _extraction_executor


def shutdown_extraction_executor(wait: bool = False) -> None:
    """Gracefully shuts down the background extraction thread pool."""
    global _extraction_executor
    if _extraction_executor is not None:
        with _executor_lock:
            if _extraction_executor is not None:
                _extraction_executor.shutdown(wait=wait, cancel_futures=True)
                _extraction_executor = None


class ExtractionService:
    """Orchestrates job lifecycle transitions, deduplication, and data extraction pipelines."""

    def __init__(self, client: Optional[ThirdPartyExtractionClient] = None):
        self.client = client or ThirdPartyExtractionClient()

    @classmethod
    def dispatch_background_extraction(cls, job_id: UUID, api_token: str) -> None:
        """Dispatches extraction to a managed bounded thread pool worker.
        
        Guarantees that the job transitions through pending -> in_progress -> completed (or failed)
        in real production execution without blocking the 202 Accepted HTTP response, while
        preventing unbounded thread creation or sudden termination.
        """
        def _worker():
            try:
                connection.close()
                service = cls()
                service.process_extraction(job_id=job_id, api_token=api_token)
            except Exception as exc:
                logger.warning(
                    "Background extraction task completed with status update for job %s: %s",
                    job_id, exc
                )
            finally:
                connection.close()

        executor = get_extraction_executor()
        executor.submit(_worker)

    @transaction.atomic
    def initiate_job(
        self,
        api_token: str,
        source_type: str = SourceType.CRM,
        metadata: Optional[Dict[str, Any]] = None,
        deduplicate: bool = True,
        strict_conflict: bool = False
    ) -> Tuple[ExtractionJob, bool]:
        """Creates a new extraction job or returns/conflicts on an active duplicate.
        
        Deduplication checks token_hash + source_type + normalized filters. If filters differ,
        a new distinct job is created.
        """
        filters = (metadata or {}).get('filters', {})
        dedup_hash = ExtractionJob.compute_dedup_hash(
            raw_token=api_token,
            source_type=source_type,
            filters=filters
        )
        token_hash = ExtractionJob.hash_token(api_token)
        masked_token = ExtractionJob.mask_token(api_token)

        if deduplicate:
            active_job = get_active_job_by_dedup_hash(dedup_hash)
            if active_job:
                if strict_conflict:
                    logger.warning("Duplicate active job detected (strict) for token=%s", masked_token)
                    raise DuplicateJobConflictError(
                        message=f"An active extraction job ({active_job.id}) is already in progress.",
                        details={'job_id': str(active_job.id), 'status': active_job.status}
                    )
                logger.info("Found existing active extraction job %s for dedup_hash=%s", active_job.id, dedup_hash[:8])
                return active_job, False

        job = ExtractionJob.objects.create(
            source_type=source_type,
            token_hash=token_hash,
            dedup_hash=dedup_hash,
            masked_token=masked_token,
            status=JobStatus.PENDING,
            metadata=metadata or {}
        )
        logger.info("Created new extraction job %s (status=pending, source=%s)", job.id, source_type)
        return job, True

    @transaction.atomic
    def cancel_job(self, job_id: UUID) -> ExtractionJob:
        """Cancels a pending or in-progress extraction job.
        
        Raises:
            ResourceNotFoundError: If job ID is not found.
            InvalidStateTransitionError: If job is already in a terminal state (completed, failed, cancelled).
        """
        job = get_job_by_id(job_id)
        if not job:
            raise ResourceNotFoundError(
                message=f"Job with ID '{job_id}' not found.",
                details={'job_id': str(job_id)}
            )

        if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise InvalidStateTransitionError(
                message=f"Cannot cancel job in state '{job.status}'. Only pending or in-progress jobs can be cancelled.",
                details={'job_id': str(job.id), 'current_status': job.status}
            )

        job.transition_to(
            JobStatus.CANCELLED,
            error_message="Job cancelled by user request."
        )
        logger.info("Extraction job %s successfully transitioned to CANCELLED.", job.id)
        return job

    @transaction.atomic
    def remove_job_data(self, job_id: UUID) -> None:
        """Removes an extraction job and all associated extracted records.
        
        Raises:
            ResourceNotFoundError: If job ID is not found.
        """
        job = get_job_by_id(job_id)
        if not job:
            raise ResourceNotFoundError(
                message=f"Job with ID '{job_id}' not found.",
                details={'job_id': str(job_id)}
            )

        job_id_str = str(job.id)
        job.delete()
        logger.info("Extraction job %s and all associated records deleted.", job_id_str)

    def process_extraction(
        self,
        job_id: UUID,
        api_token: str,
        mock_records: Optional[list] = None
    ) -> ExtractionJob:
        """Executes the extraction pipeline for a job.
        
        Fetches records from external provider, saves them in batch, and updates job status.
        Never stores raw tracebacks or internal exception text into error_details.
        """
        job = get_job_by_id(job_id)
        if not job:
            raise ResourceNotFoundError(f"Job with ID '{job_id}' not found.")

        # Mark in progress
        job.transition_to(JobStatus.IN_PROGRESS)

        try:
            if mock_records is not None:
                records_data = mock_records
            else:
                filters = job.metadata.get('filters', {})
                records_data = self.client.extract_data(
                    api_token=api_token,
                    source_type=job.source_type,
                    filters=filters
                )

            # Persist extracted records using bulk_create
            record_objects = []
            seen_ids = set()

            for item in records_data:
                service_id = str(item.get('id_from_service') or item.get('id') or '')
                if not service_id or service_id in seen_ids:
                    continue
                seen_ids.add(service_id)

                record_objects.append(
                    ExtractedRecord(
                        job=job,
                        id_from_service=service_id,
                        email=item.get('email'),
                        first_name=item.get('first_name', '')[:150],
                        last_name=item.get('last_name', '')[:150],
                        custom_fields=item.get('custom_fields', {}) or item.get('properties', {})
                    )
                )

            with transaction.atomic():
                if record_objects:
                    ExtractedRecord.objects.bulk_create(record_objects, ignore_conflicts=True)
                job.transition_to(
                    JobStatus.COMPLETED,
                    record_count=len(record_objects)
                )

            logger.info("Extraction job %s completed successfully with %d records.", job.id, len(record_objects))
            return job

        except BaseDomainException as exc:
            logger.error("Domain exception during extraction for job %s: %s", job.id, exc.message)
            # Sanitize error_details to prevent internal leaks
            safe_details = {
                'code': getattr(exc, 'code', 'EXTRACTION_ERROR'),
                'category': 'external_service_error'
            }
            if isinstance(exc.details, dict) and 'status_code' in exc.details:
                safe_details['status_code'] = exc.details['status_code']
            job.transition_to(
                JobStatus.FAILED,
                error_message=exc.message,
                error_details=safe_details
            )
            raise

        except Exception as exc:
            logger.exception("Unexpected error during extraction for job %s: %s", job.id, str(exc))
            # Never leak raw str(exc), tracebacks, or internal database errors to client status
            job.transition_to(
                JobStatus.FAILED,
                error_message="Unexpected internal error during data extraction.",
                error_details={
                    'code': 'INTERNAL_EXTRACTION_FAILURE',
                    'category': 'internal_server_error'
                }
            )
            raise
