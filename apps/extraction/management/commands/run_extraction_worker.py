"""Standalone extraction worker command for processing queued jobs."""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import connection

from apps.extraction.models import JobStatus
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
        parser.add_argument(
            '--recover-stale',
            action='store_true',
            help="Scan for and fail abandoned jobs stuck in in_progress state."
        )

    def handle(self, *args, **options):
        poll_interval = options['poll_interval']
        run_once = options['once']
        recover_stale = options['recover_stale']
        self.stdout.write(self.style.SUCCESS(
            f"Starting extraction worker (interval={poll_interval}s, once={run_once}, recover_stale={recover_stale})..."
        ))

        service = ExtractionService()

        # Automatically execute stale job recovery on startup
        recovered = service.recover_stale_jobs()
        if recovered > 0:
            self.stdout.write(self.style.WARNING(f"Recovered {recovered} stale/abandoned in_progress job(s)."))

        poll_count = 0
        while True:
            poll_count += 1
            if not run_once and (recover_stale or (poll_count % 10 == 0)):
                rec = service.recover_stale_jobs()
                if rec > 0:
                    self.stdout.write(self.style.WARNING(f"Automatically recovered {rec} stale/abandoned job(s)."))

            # Atomically claim next pending job to prevent multi-worker concurrency collisions
            job = service.claim_next_pending_job()

            if job:
                self.stdout.write(f"Claimed job {job.id} for processing...")
                try:
                    # Retrieve decrypted token from secure storage
                    token = job.get_decrypted_token()
                    if not token:
                        self.stdout.write(self.style.ERROR(
                            f"Job {job.id} failed: No valid external API token found in encrypted credentials."
                        ))
                        job.transition_to(
                            JobStatus.FAILED,
                            error_message="Authentication failure: missing or undecryptable external API token.",
                            error_details={'code': 'MISSING_CREDENTIALS', 'category': 'auth_error'}
                        )
                        continue

                    service.process_extraction(job_id=job.id, api_token=token, already_claimed=True)
                    job.refresh_from_db()
                    if job.status == JobStatus.COMPLETED:
                        self.stdout.write(self.style.SUCCESS(
                            f"Successfully completed job {job.id} ({job.record_count} records)."
                        ))
                    elif job.status == JobStatus.CANCELLED:
                        self.stdout.write(self.style.WARNING(f"Job {job.id} was cancelled during execution."))
                except Exception as exc:  # noqa: BLE001 - worker loop catches all job exceptions to maintain polling loop
                    self.stdout.write(self.style.ERROR(f"Failed job {job.id}: {exc}"))
            else:
                if run_once:
                    self.stdout.write(self.style.SUCCESS("Worker run --once complete."))
                    break
                connection.close_if_unusable_or_obsolete()
                time.sleep(poll_interval)
