"""Safe local secret rotation helper.

Generates fresh, cryptographically secure secrets and updates the local .env file.
Adheres strictly to zero-leakage security rules:
- Secrets are generated in-memory and written directly to disk.
- No secret value is ever printed to stdout, stderr, or logs.
- Refuses to run if target file is not named '.env' or is tracked in git.
- Automatically creates a timestamped .env.bak backup before making modifications.
- Supports zero-downtime key rotation for FIELD_ENCRYPTION_KEY (<NEW>,<OLD>).
"""
from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet

# Ensure project root is in sys.path when invoked directly as a script
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))


def is_tracked_by_git(filepath: Path) -> bool:
    """Checks whether the specified file is currently tracked by git.
    
    Fails closed:
    - Returns True if git confirms the file is tracked.
    - Returns False only if git explicitly confirms the file is untracked (code 1, pathspec unmatched).
    - Raises RuntimeError if git is missing, fails, or target is outside a git repository.
    """
    target = filepath.resolve()
    working_dir = target.parent if target.parent.exists() else Path.cwd()
    try:
        res = subprocess.run(
            ['git', 'ls-files', '--error-unmatch', str(target)],
            cwd=working_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        raise RuntimeError(
            f"Git executable is unavailable or failed to execute: {exc}. "
            "Cannot verify git tracking status; failing closed to prevent secret leakage."
        ) from exc

    if res.returncode == 0:
        return True

    stderr_text = res.stderr.lower()
    stdout_text = res.stdout.lower()
    if res.returncode == 1 and (
        "did not match any file" in stderr_text
        or "did not match any file" in stdout_text
        or "error: pathspec" in stderr_text
    ):
        return False

    raise RuntimeError(
        f"Unable to verify Git tracking status for '{target}': {res.stderr.strip() or 'Unknown git error'} "
        f"(exit code {res.returncode}). Failing closed."
    )


def validate_fernet_key(key: str) -> None:
    """Validates that a string is a valid 32-byte url-safe base64-encoded Fernet key."""
    if not isinstance(key, str) or not key.strip():
        raise ValueError("Encryption key cannot be empty or non-string.")
    try:
        Fernet(key.strip().encode('utf-8'))
    except Exception as exc:
        raise ValueError(f"Invalid Fernet encryption key format: {exc}") from exc


def check_dependent_encrypted_data(primary_key: str | None = None) -> int:
    """Checks whether any stored database records depend on older encryption keys.
    
    Returns 0 if and only if a successful database query confirms no stored records
    depend on older keys.
    Fails closed: raises RuntimeError on database connection, configuration, decryption,
    or environment error.
    """
    try:
        import os

        import django

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from apps.extraction.services import ExtractionService
        return ExtractionService.count_jobs_dependent_on_old_keys(primary_key=primary_key)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to verify dependent encrypted data: {exc}. "
            "Failing closed to prevent potential data loss. Ensure database is reachable or use --force-drop."
        ) from exc


def reencrypt_stored_data(primary_key: str | None = None, fallback_keys: list[str] | None = None) -> int:
    """Re-encrypts all stored database records with the specified primary key.
    
    Returns the number of successfully re-encrypted jobs.
    """
    import os

    import django

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    from apps.extraction.services import ExtractionService
    return ExtractionService.reencrypt_all_jobs(primary_key=primary_key, fallback_keys=fallback_keys)


def rotate_secrets(
    env_path: Path,
    keep_old_encryption_key: bool = True,
    force_drop: bool = False,
    reencrypt: bool = False,
    prune_old_keys_only: bool = False,
    dry_run: bool = False,
) -> dict[str, str]:
    """Rotates SECRET_KEY, API_KEY_SECRET, and FIELD_ENCRYPTION_KEY in the specified .env file.
    
    Returns a dictionary of action summaries (containing NO secret values).
    """
    target = Path(env_path).resolve()

    if target.name != '.env':
        raise ValueError(f"Target file must be named exactly '.env' (got '{target.name}').")

    if not target.parent.exists():
        raise ValueError(f"Target parent directory '{target.parent}' does not exist.")

    if is_tracked_by_git(target):
        raise RuntimeError(f"Target file '{target}' is tracked in git repository. Refusing to modify.")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    # Read existing content if file exists
    existing_lines: list[str] = []
    old_field_encryption_key: str | None = None
    old_keys: list[str] = []
    if target.exists():
        existing_lines = target.read_text(encoding='utf-8').splitlines(keepends=True)
        # Scan for existing FIELD_ENCRYPTION_KEY
        for line in existing_lines:
            match = re.match(r'^\s*FIELD_ENCRYPTION_KEY\s*=\s*["\']?([^"\'\r\n#]+)["\']?', line)
            if match:
                old_field_encryption_key = match.group(1).strip()
                break

        # Validate all existing encryption keys format
        if old_field_encryption_key:
            old_keys = [k.strip() for k in old_field_encryption_key.split(',') if k.strip()]
            for k in old_keys:
                validate_fernet_key(k)

        # Create timestamped backup (safely avoiding collisions so prior backups are never overwritten)
        backup_path = target.with_name(f"{target.name}.bak-{timestamp}")
        if backup_path.exists():
            backup_path = target.with_name(f"{target.name}.bak-{timestamp}-{secrets.token_hex(4)}")
        if not dry_run:
            backup_path.write_text(''.join(existing_lines), encoding='utf-8')
            try:
                backup_path.chmod(0o600)
            except OSError:
                pass
            backup_msg = f"Created backup: {backup_path.name}"
        else:
            backup_msg = f"[DRY RUN] Would create backup: {backup_path.name}"
    else:
        backup_msg = "[DRY RUN] Target file does not exist; would create fresh .env" if dry_run else "Target file did not exist; creating fresh .env"

    new_values: dict[str, str] = {}

    if prune_old_keys_only:
        if not old_field_encryption_key or not old_keys:
            raise ValueError("No FIELD_ENCRYPTION_KEY found in target file to prune.")
        primary_key = old_keys[0]
        dep_count = check_dependent_encrypted_data(primary_key=primary_key)
        if dep_count > 0:
            if reencrypt:
                if not dry_run:
                    reencrypt_stored_data(primary_key=primary_key, fallback_keys=old_keys)
                    dep_count = check_dependent_encrypted_data(primary_key=primary_key)
                    if dep_count > 0 and not force_drop:
                        raise RuntimeError(
                            f"Cannot drop old encryption keys: {dep_count} stored job(s) still depend on fallback keys after re-encryption. "
                            f"Ensure all records are decryptable or pass '--force-drop'."
                        )
                else:
                    # In dry run mode, record verified count without mutating DB or setting count to 0
                    pass
            elif not force_drop:
                raise RuntimeError(
                    f"Cannot drop old encryption keys: {dep_count} stored job(s) depend on fallback keys. "
                    f"Re-encrypt records first using 'python manage.py reencrypt_tokens' or pass '--reencrypt', "
                    f"or override with '--force-drop'."
                )
        new_values['FIELD_ENCRYPTION_KEY'] = primary_key
        if dep_count > 0 and reencrypt and dry_run:
            fernet_summary = (
                f"Pruned FIELD_ENCRYPTION_KEY (retained primary key; [DRY RUN] would re-encrypt {dep_count} "
                f"dependent job(s) and drop {len(old_keys) - 1} fallback key{'s' if len(old_keys) != 2 else ''})"
            )
        elif dep_count > 0 and force_drop:
            fernet_summary = (
                f"Pruned FIELD_ENCRYPTION_KEY (retained primary key, dropped old fallback keys; "
                f"WARNING: --force-drop applied, {dep_count} dependent job(s) will be unrecoverable)"
            )
        else:
            fernet_summary = "Pruned FIELD_ENCRYPTION_KEY (retained primary key, dropped old fallback keys)"
    else:
        # Generate fresh cryptographic credentials
        new_secret_key = secrets.token_urlsafe(50)
        new_api_key_secret = secrets.token_urlsafe(32)
        new_fernet_key = Fernet.generate_key().decode('utf-8')
        validate_fernet_key(new_fernet_key)

        # Compute rotated FIELD_ENCRYPTION_KEY value
        if keep_old_encryption_key and old_field_encryption_key:
            # Preserve old keys with newest primary key first
            combined_keys = [new_fernet_key] + [k for k in old_keys if k != new_fernet_key]
            final_encryption_val = ','.join(combined_keys)
            preserved_count = len(combined_keys) - 1
            fernet_summary = f"Rotated FIELD_ENCRYPTION_KEY (new primary, preserved {preserved_count} old key{'s' if preserved_count != 1 else ''})"
        else:
            if old_field_encryption_key:
                dep_count = check_dependent_encrypted_data(primary_key=new_fernet_key)
                if dep_count > 0:
                    if reencrypt:
                        if not dry_run:
                            reencrypt_stored_data(primary_key=new_fernet_key, fallback_keys=old_keys)
                            dep_count = check_dependent_encrypted_data(primary_key=new_fernet_key)
                            if dep_count > 0 and not force_drop:
                                raise RuntimeError(
                                    f"Cannot drop old encryption key: {dep_count} stored job(s) still depend on fallback keys after re-encryption. "
                                    f"Ensure all records are decryptable or pass '--force-drop'."
                                )
                        else:
                            # In dry run mode, record verified count without mutating DB or resetting to 0
                            pass
                    elif not force_drop:
                        raise RuntimeError(
                            f"Cannot drop old encryption key: {dep_count} stored job(s) depend on fallback keys. "
                            f"Re-encrypt records first using 'python manage.py reencrypt_tokens' or pass '--reencrypt', "
                            f"or override with '--force-drop'."
                        )
                final_encryption_val = new_fernet_key
                if dep_count > 0 and reencrypt and dry_run:
                    fernet_summary = (
                        f"Rotated FIELD_ENCRYPTION_KEY (new primary; [DRY RUN] would re-encrypt {dep_count} "
                        f"dependent job(s) before dropping old keys)"
                    )
                elif dep_count > 0 and force_drop:
                    fernet_summary = (
                        f"Rotated FIELD_ENCRYPTION_KEY (primary key replaced, old keys dropped; "
                        f"WARNING: --force-drop applied, {dep_count} dependent job(s) will be unrecoverable)"
                    )
                else:
                    fernet_summary = "Rotated FIELD_ENCRYPTION_KEY (primary key replaced, old keys dropped)"
            else:
                final_encryption_val = new_fernet_key
                fernet_summary = "Generated fresh FIELD_ENCRYPTION_KEY"

        new_values = {
            'SECRET_KEY': new_secret_key,
            'API_KEY_SECRET': new_api_key_secret,
            'FIELD_ENCRYPTION_KEY': final_encryption_val,
        }

    found_keys: set[str] = set()
    output_lines: list[str] = []

    for line in existing_lines:
        matched_key = None
        for key in new_values:
            if re.match(rf'^\s*{re.escape(key)}\s*=', line):
                matched_key = key
                break

        if matched_key:
            output_lines.append(f"{matched_key}={new_values[matched_key]}\n")
            found_keys.add(matched_key)
        else:
            output_lines.append(line)

    # Append any keys that were not present in the existing file
    missing_keys = [k for k in new_values if k not in found_keys]
    if missing_keys:
        if output_lines and not output_lines[-1].endswith('\n'):
            output_lines.append('\n')
        for key in missing_keys:
            output_lines.append(f"{key}={new_values[key]}\n")

    # Write securely directly to disk using atomic replacement (unless dry-run)
    if not dry_run:
        temp_file = target.with_name(f"{target.name}.tmp-{timestamp}-{secrets.token_hex(4)}")
        try:
            temp_file.write_text(''.join(output_lines), encoding='utf-8')
            try:
                temp_file.chmod(0o600)
            except OSError:
                pass
            temp_file.replace(target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
        except Exception:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass
            raise

    prefix = "[DRY RUN] " if dry_run else ""
    summary_text = fernet_summary if fernet_summary.startswith("[DRY RUN]") else f"{prefix}{fernet_summary}"
    result = {
        'backup': backup_msg,
        'FIELD_ENCRYPTION_KEY': summary_text,
    }
    if 'SECRET_KEY' in new_values:
        result['SECRET_KEY'] = f"{prefix}Rotated SECRET_KEY"
    if 'API_KEY_SECRET' in new_values:
        result['API_KEY_SECRET'] = f"{prefix}Rotated API_KEY_SECRET"
    if dry_run:
        result['dry_run'] = "[DRY RUN] Completed simulation without making persistent modifications."

    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for safe secrets rotation."""
    parser = argparse.ArgumentParser(
        description="Safely rotate local .env secrets (SECRET_KEY, API_KEY_SECRET, FIELD_ENCRYPTION_KEY)."
    )
    parser.add_argument(
        '--env-file',
        type=Path,
        default=Path('.env'),
        help="Path to .env file to update (default: .env in current directory; must be named '.env')."
    )
    parser.add_argument(
        '--drop-old-encryption-key',
        action='store_true',
        help="Drop previous encryption keys instead of keeping them in rotation."
    )
    parser.add_argument(
        '--keep-old-encryption-key',
        action='store_true',
        default=True,
        help="Keep previous encryption keys for backwards-compatible decryption (default: True)."
    )
    parser.add_argument(
        '--reencrypt',
        action='store_true',
        help="Re-encrypt existing database records with the primary key before dropping old keys."
    )
    parser.add_argument(
        '--force-drop',
        action='store_true',
        help="Force dropping old encryption keys even if existing database records depend on them."
    )
    parser.add_argument(
        '--prune-old-keys',
        action='store_true',
        help="Drop old fallback encryption keys while keeping current primary key without generating a new key."
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help="Simulate the secret rotation process without making any changes to disk or database."
    )

    args = parser.parse_args(argv)

    keep_old = not args.drop_old_encryption_key

    try:
        results = rotate_secrets(
            args.env_file,
            keep_old_encryption_key=keep_old,
            force_drop=args.force_drop,
            reencrypt=args.reencrypt,
            prune_old_keys_only=args.prune_old_keys,
            dry_run=args.dry_run,
        )
        for summary in results.values():
            print(summary)

        print("\n" + "=" * 80)
        print("MANUAL ACTION REQUIRED: External Provider Dashboards Checklist")
        print("=" * 80)
        print("[ ] 1. DATABASE_URL: Rotate PostgreSQL password in database console (e.g. Supabase, AWS RDS)")
        print("       and update DATABASE_URL in .env.")
        print("[ ] 2. External Provider Tokens: Rotate API access tokens and client secrets in CRM / third-party")
        print("       developer portals (e.g., HubSpot CRM, Salesforce).")
        print("[ ] 3. Webhooks & Signing Keys: Re-generate and configure webhook HMAC signing secrets in provider consoles.")
        print("[ ] 4. Restart backend application services to apply updated environment variables.")
        print("[ ] 5. Re-encryption: Once services are restarted, re-encrypt stored records with:")
        print("       python manage.py reencrypt_tokens")
        print("[ ] 6. Post-migration: Once verified, prune old fallback keys from rotation with:")
        print("       python scripts/rotate_secrets.py --prune-old-keys")
        print("=" * 80)
        return 0
    except (ValueError, RuntimeError, OSError) as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1


if __name__ == '__main__':
    sys.exit(main())
