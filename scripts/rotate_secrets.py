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


def is_tracked_by_git(filepath: Path) -> bool:
    """Checks whether the specified file is currently tracked by git."""
    try:
        res = subprocess.run(
            ['git', 'ls-files', '--error-unmatch', str(filepath)],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def rotate_secrets(
    env_path: Path,
    keep_old_encryption_key: bool = True,
) -> dict[str, str]:
    """Rotates SECRET_KEY, API_KEY_SECRET, and FIELD_ENCRYPTION_KEY in the specified .env file.
    
    Returns a dictionary of action summaries (containing NO secret values).
    """
    target = env_path.resolve()

    if target.name != '.env':
        raise ValueError(f"Target file must be named exactly '.env' (got '{target.name}').")

    if is_tracked_by_git(target):
        raise RuntimeError(f"Target file '{target}' is tracked in git repository. Refusing to modify.")

    # Read existing content if file exists
    existing_lines: list[str] = []
    old_field_encryption_key: str | None = None
    if target.exists():
        existing_lines = target.read_text(encoding='utf-8').splitlines(keepends=True)
        # Scan for existing FIELD_ENCRYPTION_KEY
        for line in existing_lines:
            match = re.match(r'^\s*FIELD_ENCRYPTION_KEY\s*=\s*["\']?([^"\'\r\n#]+)["\']?', line)
            if match:
                old_field_encryption_key = match.group(1).strip()
                break

        # Create timestamped backup
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = target.with_name(f"{target.name}.bak-{timestamp}")
        backup_path.write_text(''.join(existing_lines), encoding='utf-8')
        backup_msg = f"Created backup: {backup_path.name}"
    else:
        backup_msg = "Target file did not exist; creating fresh .env"

    # Generate fresh cryptographic credentials
    new_secret_key = secrets.token_urlsafe(50)
    new_api_key_secret = secrets.token_urlsafe(32)
    new_fernet_key = Fernet.generate_key().decode('utf-8')

    # Compute rotated FIELD_ENCRYPTION_KEY value
    if keep_old_encryption_key and old_field_encryption_key:
        old_keys = [k.strip() for k in old_field_encryption_key.split(',') if k.strip()]
        # Preserve old keys with newest primary key first
        combined_keys = [new_fernet_key] + [k for k in old_keys if k != new_fernet_key]
        final_encryption_val = ','.join(combined_keys)
        preserved_count = len(combined_keys) - 1
        fernet_summary = f"Rotated FIELD_ENCRYPTION_KEY (new primary, preserved {preserved_count} old key{'s' if preserved_count != 1 else ''})"
    else:
        final_encryption_val = new_fernet_key
        fernet_summary = "Rotated FIELD_ENCRYPTION_KEY (primary key replaced, old keys dropped)"

    # Build updated file content preserving line order and non-target variables
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

    # Write securely directly to disk
    target.write_text(''.join(output_lines), encoding='utf-8')

    return {
        'backup': backup_msg,
        'SECRET_KEY': 'Rotated SECRET_KEY',
        'API_KEY_SECRET': 'Rotated API_KEY_SECRET',
        'FIELD_ENCRYPTION_KEY': fernet_summary,
    }


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

    args = parser.parse_args(argv)

    keep_old = not args.drop_old_encryption_key

    try:
        results = rotate_secrets(args.env_file, keep_old_encryption_key=keep_old)
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
        print("[ ] 5. Post-migration: Once existing database records have been re-encrypted with the new primary key,")
        print("       run: python scripts/rotate_secrets.py --drop-old-encryption-key")
        print("=" * 80)
        return 0
    except (ValueError, RuntimeError, OSError) as err:
        sys.stderr.write(f"Error: {err}\n")
        return 1


if __name__ == '__main__':
    sys.exit(main())
