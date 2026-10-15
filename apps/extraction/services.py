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
        in real production execution without blocking the 202 Accepted HTTP response.
        Both inline thread workers and standalone background worker processes must
        claim the job atomically before processing.
        """
        def _worker():
            try:
                connection.close_if_unusable_or_obsolete()
                service = cls()
                # Unified atomic claiming in the thread worker path:
                claimed = service.claim_job(job_id=job_id)
                if not claimed:
                    logger.info("Background thread skipped job %s (already claimed or cancelled).", job_id)
                    return
                # Retrieve token from parameter or decrypted storage
                token = api_token or claimed.get_decrypted_token()
                service.process_extraction(job_id=job_id, api_token=token, already_claimed=True)
            except Exception as exc:
                logger.warning(
                    "Background extraction task completed with status update for job %s: %s",
                    job_id, exc
                )
            finally:
                connection.close_if_unusable_or_obsolete()

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
        # Automatically run background stale job recovery
        self.recover_stale_jobs()

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

        job = ExtractionJob(
            source_type=source_type,
            token_hash=token_hash,
            dedup_hash=dedup_hash,
            masked_token=masked_token,
            status=JobStatus.PENDING,
            metadata=metadata or {}
        )
        job.set_encrypted_token(api_token)
        job.save()
        logger.info("Created new extraction job %s (status=pending, source=%s)", job.id, source_type)
        return job, True

    @classmethod
    def claim_job(cls, job_id: Optional[UUID] = None) -> Optional[ExtractionJob]:
        """Atomically claims a specific or next pending extraction job without race conditions.
        
        Uses conditional update (status=PENDING -> IN_PROGRESS) to guarantee that across both
        inline thread workers and standalone worker processes, each job is claimed exclusively
        by exactly one worker.
        """
        now = timezone.now()
        if job_id is not None:
            rows_updated = ExtractionJob.objects.filter(
                id=job_id,
                status=JobStatus.PENDING
            ).update(
                status=JobStatus.IN_PROGRESS,
                start_time=now,
                updated_at=now
            )
            if rows_updated == 1:
                job = ExtractionJob.objects.get(id=job_id)
                logger.info("Worker atomically claimed specific job %s", job.id)
                return job
            return None

        # Next pending job ordered by created_at
        candidate = ExtractionJob.objects.filter(
            status=JobStatus.PENDING
        ).order_by('created_at').values_list('id', flat=True).first()

        if not candidate:
            return None

        rows_updated = ExtractionJob.objects.filter(
            id=candidate,
            status=JobStatus.PENDING
        ).update(
            status=JobStatus.IN_PROGRESS,
            start_time=now,
            updated_at=now
        )

        if rows_updated == 1:
            job = ExtractionJob.objects.get(id=candidate)
            logger.info("Worker atomically claimed next job %s", job.id)
            return job
        return None

    @classmethod
    def claim_next_pending_job(cls) -> Optional[ExtractionJob]:
        """Convenience method for worker loops."""
        return cls.claim_job()

    @classmethod
    def recover_stale_jobs(cls, timeout_minutes: Optional[int] = None, allow_retry: bool = True) -> int:
        """Identifies and recovers stale/abandoned in_progress jobs automatically.
        
        Decides whether interrupted jobs can safely be retried or should permanently fail:
        - If the job has valid encrypted credentials and retry_count < max_retries,
          it cleans up any partial records for idempotency and resets the job to PENDING.
        - If max retries have been reached or credentials cannot be recovered,
          it permanently transitions the job to FAILED.
        """
        minutes = timeout_minutes or getattr(settings, 'EXTRACTION_JOB_TIMEOUT_MINUTES', 15)
        max_retries_setting = getattr(settings, 'EXTRACTION_MAX_RETRIES', 3)
        threshold = timezone.now() - timezone.timedelta(minutes=minutes)
        stale_jobs = list(ExtractionJob.objects.filter(
            status=JobStatus.IN_PROGRESS,
            start_time__lt=threshold
        ))
        recovered_count = 0
        for job in stale_jobs:
            logger.warning(
                "Recovering stale abandoned extraction job %s (started %s, retry %d/%d)",
                job.id, job.start_time, job.retry_count, job.max_retries or max_retries_setting
            )
            try:
                can_retry = allow_retry and (job.retry_count < (job.max_retries or max_retries_setting)) and bool(job.encrypted_token)

                if can_retry:
                    # Clean up partial records from interrupted run to guarantee idempotent retry
                    ExtractedRecord.objects.filter(job=job).delete()
                    job.record_count = 0
                    job.retry_count += 1
                    job.status = JobStatus.PENDING
                    job.start_time = None
                    job.end_time = None
                    job.error_message = f"Job was interrupted and automatically recovered for retry attempt {job.retry_count}."
                    job.error_details = {
                        'code': 'AUTOMATIC_RECOVERY_RETRY',
                        'retry_count': job.retry_count,
                        'max_retries': job.max_retries or max_retries_setting,
                        'recovered_at': timezone.now().isoformat()
                    }
                    job.save(update_fields=[
                        'status', 'retry_count', 'record_count',
                        'start_time', 'end_time', 'error_message', 'error_details', 'updated_at'
                    ])
                    logger.info("Stale job %s safely recovered and re-queued to PENDING (retry %d)", job.id, job.retry_count)
                    recovered_count += 1
                else:
                    reason = "Max retries exceeded" if job.retry_count >= (job.max_retries or max_retries_setting) else "Missing recovery credentials"
                    code = 'MAX_RETRIES_EXCEEDED' if 'Max' in reason else 'WORKER_ABANDONED_TIMEOUT'
                    job.transition_to(
                        JobStatus.FAILED,
                        error_message=f"Job timed out and was abandoned after {minutes} minutes ({reason}).",
                        error_details={
                            'code': code,
                            'category': 'worker_failure',
                            'retry_count': job.retry_count,
                            'reason': reason,
                            'threshold_minutes': minutes
                        }
                    )
                    recovered_count += 1
            except Exception as exc:
                logger.error("Failed to recover stale job %s: %s", job.id, exc)
        return recovered_count

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
        api_token: Optional[str] = None,
        mock_records: Optional[list] = None,
        already_claimed: bool = False
    ) -> ExtractionJob:
        """Executes the extraction pipeline for a job.
        
        Fetches records from external provider, saves them in batch, and updates job status.
        Never stores raw tracebacks or internal exception text into error_details.
        Both execution paths must claim the job atomically before processing.
        """
        if not already_claimed:
            claimed_job = self.claim_job(job_id=job_id)
            if not claimed_job:
                # If could not claim, check if it was cancelled or already in progress
                job = get_job_by_id(job_id)
                if not job:
                    raise ResourceNotFoundError(f"Job with ID '{job_id}' not found.")
                logger.info("Job %s was not claimed (status=%s). Returning.", job.id, job.status)
                return job
            job = claimed_job
        else:
            job = get_job_by_id(job_id)
            if not job:
                raise ResourceNotFoundError(f"Job with ID '{job_id}' not found.")

        # Resolve API token: either explicitly passed or decrypted from secure storage
        active_token = api_token or job.get_decrypted_token()
        if not active_token and mock_records is None:
            job.transition_to(
                JobStatus.FAILED,
                error_message="Authentication failed: No valid API token available for extraction.",
                error_details={'code': 'MISSING_CREDENTIALS', 'category': 'auth_error'}
            )
            from apps.common.exceptions import ExternalServiceAuthError
            raise ExternalServiceAuthError("No valid external API token available for extraction job.")

        # Mark in progress if pending
        if job.status == JobStatus.PENDING:
            job.transition_to(JobStatus.IN_PROGRESS)
        elif job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            logger.warning("Attempted to process extraction on terminal job %s (status=%s)", job.id, job.status)
            return job

        try:
            if mock_records is not None:
                records_data = mock_records
            else:
                filters = job.metadata.get('filters', {})
                records_data = self.client.extract_data(
                    api_token=active_token,
                    source_type=job.source_type,
                    filters=filters
                )

            # Check if job was cancelled while external network request was executing
            job.refresh_from_db()
            if job.status == JobStatus.CANCELLED:
                logger.info(
                    "Extraction job %s was cancelled during external extraction; skipping record persistence.",
                    job.id
                )
                return job

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
                job.refresh_from_db()
                if job.status == JobStatus.CANCELLED:
                    logger.info("Extraction job %s was cancelled during atomic commit; aborting completion.", job.id)
                    return job

                if record_objects:
                    ExtractedRecord.objects.bulk_create(record_objects, ignore_conflicts=True)

                # Accurately reflect actual persisted count in database
                actual_persisted_count = ExtractedRecord.objects.filter(job=job).count()
                job.transition_to(
                    JobStatus.COMPLETED,
                    record_count=actual_persisted_count
                )

            logger.info("Extraction job %s completed successfully with %d records.", job.id, actual_persisted_count)
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
            job.refresh_from_db()
            if job.status != JobStatus.CANCELLED:
                job.transition_to(
                    JobStatus.FAILED,
                    error_message=exc.message,
                    error_details=safe_details
                )
            raise

        except Exception as exc:
            logger.exception("Unexpected error during extraction for job %s: %s", job.id, str(exc))
            # Never leak raw str(exc), tracebacks, or internal database errors to client status
            job.refresh_from_db()
            if job.status != JobStatus.CANCELLED:
                job.transition_to(
                    JobStatus.FAILED,
                    error_message="Unexpected internal error during data extraction.",
                    error_details={
                        'code': 'INTERNAL_EXTRACTION_FAILURE',
                        'category': 'internal_server_error'
                    }
                )
            raise
