"""Database models for the Data Extraction Service."""
import uuid
import hashlib
from typing import Optional
from django.db import models
from django.utils import timezone
from apps.common.exceptions import InvalidStateTransitionError


class JobStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    IN_PROGRESS = 'in_progress', 'In Progress'
    COMPLETED = 'completed', 'Completed'
    FAILED = 'failed', 'Failed'
    CANCELLED = 'cancelled', 'Cancelled'


class SourceType(models.TextChoices):
    CRM = 'crm', 'CRM'
    CONTACTS = 'contacts', 'Contacts'
    LEADS = 'leads', 'Leads'
    CUSTOM = 'custom', 'Custom'


class ExtractionJob(models.Model):
    """Represents an asynchronous or scheduled data extraction job."""

    VALID_TRANSITIONS = {
        JobStatus.PENDING: {JobStatus.IN_PROGRESS, JobStatus.CANCELLED, JobStatus.FAILED},
        JobStatus.IN_PROGRESS: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED},
        JobStatus.COMPLETED: set(),
        JobStatus.FAILED: set(),
        JobStatus.CANCELLED: set(),
    }

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text="Globally unique identifier for the extraction job."
    )
    source_type = models.CharField(
        max_length=64,
        choices=SourceType.choices,
        default=SourceType.CRM,
        db_index=True,
        help_text="Target data source type to extract."
    )
    token_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of the API token used for deduplication without storing plaintext."
    )
    dedup_hash = models.CharField(
        max_length=64,
        db_index=True,
        blank=True,
        default='',
        help_text="SHA-256 hash of token + source_type + normalized filters for comprehensive deduplication."
    )
    masked_token = models.CharField(
        max_length=64,
        blank=True,
        default='',
        help_text="Masked token representation for auditing, e.g. 'pat-***1234'."
    )
    encrypted_token = models.TextField(
        blank=True,
        default='',
        help_text="Securely encrypted external provider API token for background worker extraction."
    )
    status = models.CharField(
        max_length=32,
        choices=JobStatus.choices,
        default=JobStatus.PENDING,
        db_index=True,
        help_text="Current lifecycle state of the extraction job."
    )
    record_count = models.PositiveIntegerField(
        default=0,
        help_text="Total number of records successfully extracted."
    )
    retry_count = models.PositiveIntegerField(
        default=0,
        help_text="Number of automatic recovery retries attempted."
    )
    max_retries = models.PositiveIntegerField(
        default=3,
        help_text="Maximum recovery retries allowed before job is permanently marked failed."
    )
    start_time = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp when extraction processing began."
    )
    end_time = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp when extraction finished (completed, failed, or cancelled)."
    )
    error_message = models.TextField(
        null=True,
        blank=True,
        help_text="Descriptive error message if job execution failed or was interrupted."
    )
    error_details = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured diagnostic error payload or HTTP response code from external provider."
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary job configuration, filters, or webhook URLs."
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Creation timestamp."
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="Last modification timestamp."
    )

    class Meta:
        db_table = 'extraction_jobs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'created_at'], name='job_status_created_idx'),
            models.Index(fields=['token_hash', 'status'], name='job_token_status_idx'),
            models.Index(fields=['dedup_hash', 'status'], name='job_dedup_status_idx'),
            models.Index(fields=['source_type', 'status'], name='job_source_status_idx'),
        ]

    def __str__(self) -> str:
        return f"ExtractionJob({self.id}, status={self.status}, records={self.record_count})"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """Produces a deterministic SHA-256 hash of the API token."""
        return hashlib.sha256(raw_token.strip().encode('utf-8')).hexdigest()

    @staticmethod
    def compute_dedup_hash(raw_token: str, source_type: str = 'crm', filters: Optional[dict] = None) -> str:
        """Produces a deterministic SHA-256 hash of token, source_type, and sorted filters."""
        import json
        normalized_filters = json.dumps(filters or {}, sort_keys=True, separators=(',', ':'))
        payload = f"{raw_token.strip()}|{source_type.strip()}|{normalized_filters}"
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @staticmethod
    def mask_token(raw_token: str) -> str:
        """Masks an API token for safe storage and logging."""
        if not raw_token:
            return '***'
        token = raw_token.strip()
        if len(token) <= 8:
            return '***'
        return f"{token[:3]}***{token[-4:]}"

    @classmethod
    def encrypt_token(cls, raw_token: str) -> str:
        """Encrypts a token string using Fernet field encryption."""
        from apps.common.encryption import encrypt_secret
        return encrypt_secret(raw_token) if raw_token else ''

    def set_encrypted_token(self, raw_token: str) -> None:
        """Encrypts and sets the external provider token using Fernet symmetric encryption."""
        if raw_token and raw_token.strip():
            self.encrypted_token = self.encrypt_token(raw_token.strip())
        else:
            self.encrypted_token = ''

    def get_decrypted_token(self) -> Optional[str]:
        """Decrypts and returns the stored external provider token, or None if empty or invalid."""
        from apps.common.encryption import decrypt_secret
        if not self.encrypted_token:
            return None
        try:
            return decrypt_secret(self.encrypted_token)
        except Exception:
            return None

    def clear_encrypted_token(self) -> None:
        """Purges sensitive encrypted credential upon reaching a terminal lifecycle state."""
        self.encrypted_token = ''

    @property
    def is_terminal(self) -> bool:
        """Returns True if the job has reached a terminal state."""
        return self.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def duration_seconds(self) -> Optional[float]:
        """Calculates total execution duration in seconds if start and end times exist."""
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return None

    @property
    def progress_percentage(self) -> int:
        """Computes approximate progress percentage based on lifecycle state."""
        if self.status == JobStatus.PENDING:
            return 0
        if self.status == JobStatus.IN_PROGRESS:
            return 50
        if self.status in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}:
            return 100
        return 0

    def can_transition_to(self, target_status: str) -> bool:
        """Validates if transition from current status to target_status is permissible."""
        allowed = self.VALID_TRANSITIONS.get(self.status, set())
        return target_status in allowed

    def transition_to(
        self,
        target_status: str,
        error_message: Optional[str] = None,
        error_details: Optional[dict] = None,
        record_count: Optional[int] = None
    ) -> None:
        """Applies a verified state transition or raises InvalidStateTransitionError."""
        if not self.can_transition_to(target_status):
            raise InvalidStateTransitionError(
                message=f"Cannot transition job from '{self.status}' to '{target_status}'.",
                details={
                    'current_status': self.status,
                    'target_status': target_status,
                    'allowed_transitions': list(self.VALID_TRANSITIONS.get(self.status, set()))
                }
            )

        now = timezone.now()
        update_fields = [
            'status', 'start_time', 'end_time', 'error_message',
            'error_details', 'record_count', 'updated_at'
        ]

        if target_status == JobStatus.IN_PROGRESS and not self.start_time:
            self.start_time = now
        elif target_status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
            self.end_time = now
            self.clear_encrypted_token()
            update_fields.append('encrypted_token')

        self.status = target_status

        if error_message is not None:
            self.error_message = error_message
        if error_details is not None:
            self.error_details = error_details
        if record_count is not None:
            self.record_count = record_count

        self.save(update_fields=update_fields)


class ExtractedRecord(models.Model):
    """Individual entity record extracted during an ExtractionJob."""

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
        help_text="Unique internal identifier for the extracted record."
    )
    job = models.ForeignKey(
        ExtractionJob,
        on_delete=models.CASCADE,
        related_name='records',
        db_index=True,
        help_text="The extraction job that produced this record."
    )
    id_from_service = models.CharField(
        max_length=255,
        db_index=True,
        help_text="Original identifier from the third-party service."
    )
    email = models.EmailField(
        max_length=255,
        blank=True,
        null=True,
        db_index=True,
        help_text="Email address of the extracted contact/entity."
    )
    first_name = models.CharField(
        max_length=150,
        blank=True,
        default='',
        help_text="First name."
    )
    last_name = models.CharField(
        max_length=150,
        blank=True,
        default='',
        help_text="Last name."
    )
    custom_fields = models.JSONField(
        default=dict,
        blank=True,
        help_text="Arbitrary extra fields returned by the third-party provider."
    )
    extracted_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="Timestamp when the record was persisted."
    )

    class Meta:
        db_table = 'extracted_records'
        ordering = ['id_from_service', 'extracted_at']
        constraints = [
            models.UniqueConstraint(
                fields=['job', 'id_from_service'],
                name='unique_record_per_job'
            )
        ]
        indexes = [
            models.Index(fields=['job', 'email'], name='record_job_email_idx'),
            models.Index(fields=['job', 'id_from_service'], name='record_job_service_id_idx'),
        ]

    def __str__(self) -> str:
        return f"ExtractedRecord({self.id_from_service}, job={self.job_id}, email={self.email})"
