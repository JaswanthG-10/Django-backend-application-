"""Management command to re-encrypt stored job tokens with the primary encryption key."""
import sys

from django.core.management.base import BaseCommand

from apps.extraction.services import ExtractionService


class Command(BaseCommand):
    help = "Re-encrypts all stored extraction job tokens with the current primary FIELD_ENCRYPTION_KEY."

    def add_arguments(self, parser):
        parser.add_argument(
            '--verify-only',
            action='store_true',
            help="Check if any jobs depend on old keys without performing re-encryption."
        )

    def handle(self, *args, **options):
        verify_only = options.get('verify_only', False)

        dependent_count = ExtractionService.count_jobs_dependent_on_old_keys()
        if verify_only:
            if dependent_count == 0:
                self.stdout.write(self.style.SUCCESS(
                    "All stored job tokens decrypt cleanly with primary key. Safe to drop old keys."
                ))
                return
            self.stdout.write(self.style.WARNING(
                f"{dependent_count} job token(s) depend on old fallback keys."
            ))
            sys.exit(1)

        if dependent_count == 0:
            self.stdout.write(self.style.SUCCESS(
                "All stored job tokens already decrypt with primary key. No re-encryption required."
            ))
            return

        self.stdout.write(f"Found {dependent_count} job token(s) requiring old keys. Re-encrypting...")
        count = ExtractionService.reencrypt_all_jobs()
        self.stdout.write(self.style.SUCCESS(f"Successfully re-encrypted and verified {count} job token(s)."))

        remaining = ExtractionService.count_jobs_dependent_on_old_keys()
        if remaining > 0:
            self.stderr.write(self.style.ERROR(
                f"Warning: {remaining} job token(s) still cannot be decrypted by primary key."
            ))
            sys.exit(1)

        self.stdout.write(self.style.SUCCESS(
            "Verification complete: 0 records depend on old keys. Safe to drop old keys."
        ))
