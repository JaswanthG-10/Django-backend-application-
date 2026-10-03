"""FactoryBoy factories for deterministic test data generation."""
import uuid
import factory
from django.utils import timezone
from apps.extraction.models import ExtractionJob, ExtractedRecord, JobStatus, SourceType


class ExtractionJobFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExtractionJob

    id = factory.LazyFunction(uuid.uuid4)
    source_type = SourceType.CRM
    token_hash = factory.LazyAttribute(lambda o: ExtractionJob.hash_token("test-secret-api-token"))
    masked_token = "tes***oken"
    status = JobStatus.PENDING
    record_count = 0
    start_time = None
    end_time = None
    error_message = None
    error_details = factory.LazyFunction(dict)
    metadata = factory.LazyFunction(dict)


class CompletedExtractionJobFactory(ExtractionJobFactory):
    status = JobStatus.COMPLETED
    record_count = 5
    start_time = factory.LazyFunction(lambda: timezone.now() - timezone.timedelta(seconds=45))
    end_time = factory.LazyFunction(timezone.now)


class InProgressExtractionJobFactory(ExtractionJobFactory):
    status = JobStatus.IN_PROGRESS
    start_time = factory.LazyFunction(timezone.now)


class FailedExtractionJobFactory(ExtractionJobFactory):
    status = JobStatus.FAILED
    start_time = factory.LazyFunction(lambda: timezone.now() - timezone.timedelta(seconds=10))
    end_time = factory.LazyFunction(timezone.now)
    error_message = "External service 500 error"
    error_details = factory.LazyFunction(lambda: {"status_code": 500})


class CancelledExtractionJobFactory(ExtractionJobFactory):
    status = JobStatus.CANCELLED
    start_time = factory.LazyFunction(lambda: timezone.now() - timezone.timedelta(seconds=10))
    end_time = factory.LazyFunction(timezone.now)
    error_message = "Job cancelled by user request."


class ExtractedRecordFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ExtractedRecord

    id = factory.LazyFunction(uuid.uuid4)
    job = factory.SubFactory(CompletedExtractionJobFactory)
    id_from_service = factory.Sequence(lambda n: f"ext_contact_{n}")
    email = factory.Sequence(lambda n: f"user_{n}@example.com")
    first_name = factory.Faker('first_name')
    last_name = factory.Faker('last_name')
    custom_fields = factory.LazyFunction(lambda: {"company": "Acme Inc", "title": "Lead Engineer"})
