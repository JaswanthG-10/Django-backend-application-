"""Standalone extraction worker command for processing queued jobs."""
import time
import logging
from django.core.management.base import BaseCommand
from django.conf import settings
from apps.extraction.models import ExtractionJob, JobStatus
from apps.extraction.services import ExtractionService

logger = logging.getLogger('apps.extraction.worker')


class Command(BaseCommand):
    help = "Runs the data extraction worker polling loop to pick up pending jobs."

    def add_arguments(self, parser):
        parser.add_argument(
            '--poll-interval',
            type=float,
            default=2.0,
            help="Polling interval in seconds between checks for pending jobs."
        )
        parser.add_argument(
            '--once',
            action='store_true',
            help="Process any currently pending jobs once and exit immediately."
        )

    def handle(self, *args, **options):
        poll_interval = options['poll_interval']
        run_once = options['once']
        self.stdout.write(self.style.SUCCESS(f"Starting extraction worker (interval={poll_interval}s, once={run_once})..."))

        service = ExtractionService()
        default_token = getattr(settings, 'EXTERNAL_API_DEFAULT_TOKEN', '') or 'service-worker-token'

        while True:
            pending_jobs = list(ExtractionJob.objects.filter(status=JobStatus.PENDING).order_by('created_at')[:10])

            if pending_jobs:
                self.stdout.write(f"Found {len(pending_jobs)} pending jobs to process.")
                for job in pending_jobs:
                    self.stdout.write(f"Processing job {job.id}...")
                    try:
                        # Use stored token or default token
                        token = job.metadata.get('api_token') or default_token
                        service.process_extraction(job_id=job.id, api_token=token)
                        self.stdout.write(self.style.SUCCESS(f"Successfully completed job {job.id}."))
                    except Exception as exc:
                        self.stdout.write(self.style.ERROR(f"Failed job {job.id}: {exc}"))

            if run_once:
                self.stdout.write(self.style.SUCCESS("Worker run --once complete."))
                break

            time.sleep(poll_interval)
