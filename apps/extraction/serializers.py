"""DRF Serializers for Data Extraction API inputs and outputs."""
from rest_framework import serializers
from apps.extraction.models import ExtractionJob, ExtractedRecord, SourceType, JobStatus


class StartScanRequestSerializer(serializers.Serializer):
    """Input payload for initiating a data extraction scan."""
    api_token = serializers.CharField(
        required=True,
        min_length=5,
        max_length=512,
        write_only=True,
        help_text="Third-party service API token or bearer secret."
    )
    source_type = serializers.ChoiceField(
        choices=SourceType.choices,
        default=SourceType.CRM,
        required=False,
        help_text="Target data domain (crm, contacts, leads)."
    )
    filters = serializers.DictField(
        required=False,
        default=dict,
        help_text="Optional query filters to pass to the external provider."
    )
    strict_deduplication = serializers.BooleanField(
        required=False,
        default=False,
        help_text="If True, returns 409 Conflict when a matching active job exists."
    )

    def validate_api_token(self, value: str) -> str:
        token = value.strip()
        if not token:
            raise serializers.ValidationError("API token cannot be empty or whitespace only.")
        if len(token) < 5:
            raise serializers.ValidationError("API token is too short to be valid.")
        return token


class StartScanResponseSerializer(serializers.Serializer):
    """Response returned upon successful acceptance of scan initiation."""
    job_id = serializers.UUIDField(help_text="Unique ID assigned to the extraction job.")
    status = serializers.CharField(help_text="Current state (pending or in_progress).")
    message = serializers.CharField(help_text="Status summary message.")
    is_existing = serializers.BooleanField(help_text="True if an already running job was reused.")


class JobStatusResponseSerializer(serializers.ModelSerializer):
    """Detailed progress and lifecycle status of a specific job."""
    job_id = serializers.UUIDField(source='id', read_only=True)
    duration_seconds = serializers.FloatField(read_only=True)
    progress_percentage = serializers.IntegerField(read_only=True)

    class Meta:
        model = ExtractionJob
        fields = [
            'job_id',
            'status',
            'source_type',
            'record_count',
            'retry_count',
            'max_retries',
            'start_time',
            'heartbeat_at',
            'end_time',
            'duration_seconds',
            'progress_percentage',
            'error_message',
            'error_details',
            'metadata',
            'created_at',
            'updated_at',
        ]


class ExtractedRecordSerializer(serializers.ModelSerializer):
    """Output serializer for an extracted record."""
    job_id = serializers.UUIDField(source='job.id', read_only=True)

    class Meta:
        model = ExtractedRecord
        fields = [
            'id',
            'job_id',
            'id_from_service',
            'email',
            'first_name',
            'last_name',
            'custom_fields',
            'extracted_at',
        ]


class JobCancelResponseSerializer(serializers.Serializer):
    """Response confirmation for job cancellation."""
    job_id = serializers.UUIDField()
    status = serializers.CharField()
    message = serializers.CharField()


class JobListSerializer(serializers.ModelSerializer):
    """Compact summary serializer for listing historical jobs."""
    job_id = serializers.UUIDField(source='id', read_only=True)

    class Meta:
        model = ExtractionJob
        fields = [
            'job_id',
            'status',
            'source_type',
            'record_count',
            'start_time',
            'end_time',
            'created_at',
        ]


class JobStatisticsSerializer(serializers.Serializer):
    """Aggregated metrics across all extraction jobs."""
    total_jobs = serializers.IntegerField()
    completed_jobs = serializers.IntegerField()
    failed_jobs = serializers.IntegerField()
    cancelled_jobs = serializers.IntegerField()
    pending_jobs = serializers.IntegerField()
    in_progress_jobs = serializers.IntegerField()
    total_records_extracted = serializers.IntegerField()
    average_duration_seconds = serializers.FloatField()
    success_rate_percentage = serializers.FloatField()


class JobAnalyticsSerializer(serializers.Serializer):
    """Analytics distribution and recent activity."""
    summary = JobStatisticsSerializer()
    source_distribution = serializers.DictField()
    recent_activity = serializers.ListField()
    generated_at = serializers.CharField()


class HealthResponseSerializer(serializers.Serializer):
    """Operational health check response."""
    status = serializers.CharField()
    database = serializers.CharField()
    timestamp = serializers.CharField()
    version = serializers.CharField()
    checks = serializers.DictField(required=False)

