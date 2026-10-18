"""Comprehensive test suite for secret rotation, SHA-256 blocklists, and zero-leakage policies."""
from __future__ import annotations

import hashlib
import re
import secrets
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command

from apps.common.encryption import decrypt_secret, encrypt_secret, get_fernet, reset_fernet
from apps.extraction.models import ExtractionJob
from apps.extraction.services import ExtractionService
from config.settings import (
    _PROHIBITED_API_KEY_HASHES,
    _PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES,
    _PROHIBITED_SECRET_KEY_HASHES,
    _is_prohibited_hash,
)
from scripts.rotate_secrets import (
    check_dependent_encrypted_data,
    rotate_secrets,
)
from scripts.rotate_secrets import (
    main as rotate_secrets_main,
)


class TestProhibitedSecretHashes:
    def test_blocklists_contain_only_64_char_hex_hashes(self):
        """Verifies that blocklists contain strictly 64-character lowercase hex digests and no plaintext secrets."""
        all_blocklists = [
            ('SECRET_KEY', _PROHIBITED_SECRET_KEY_HASHES),
            ('API_KEY_SECRET', _PROHIBITED_API_KEY_HASHES),
            ('FIELD_ENCRYPTION_KEY', _PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES),
        ]
        hex_pattern = re.compile(r'^[0-9a-f]{64}$')

        for name, blocklist in all_blocklists:
            assert len(blocklist) > 0, f"{name} blocklist must not be empty"
            for digest in blocklist:
                assert isinstance(digest, str), f"Digest {digest!r} in {name} is not a string"
                assert len(digest) == 64, f"Digest {digest} in {name} does not have length 64"
                assert hex_pattern.match(digest), f"Digest {digest} in {name} is not a valid 64-char lowercase hex string"

    def test_settings_validation_rejects_matching_canary_hash_and_accepts_fresh_random(self):
        """Tests that _is_prohibited_hash detects matched SHA-256 hashes and accepts unblocked values."""
        canary_secret = "test-canary-secret-value-for-unit-testing-only-12345"
        canary_hash = hashlib.sha256(canary_secret.strip().encode('utf-8')).hexdigest()
        test_blocklist = {canary_hash}

        # Canary matches exactly
        assert _is_prohibited_hash(canary_secret, test_blocklist) is True
        # Whitespace-tolerant matching
        assert _is_prohibited_hash(f"  {canary_secret}  \n", test_blocklist) is True

        # Non-matching strings return False
        assert _is_prohibited_hash("different-unblocked-secret-value", test_blocklist) is False
        # Empty or None values fail closed (treated as prohibited)
        assert _is_prohibited_hash("", test_blocklist) is True
        assert _is_prohibited_hash(None, test_blocklist) is True

        # Fresh random values must never match the actual settings blocklists
        fresh_secret = secrets.token_urlsafe(50)
        assert len(fresh_secret) >= 32
        assert not _is_prohibited_hash(fresh_secret, _PROHIBITED_SECRET_KEY_HASHES)

        fresh_api_key = secrets.token_urlsafe(32)
        assert len(fresh_api_key) >= 32
        assert not _is_prohibited_hash(fresh_api_key, _PROHIBITED_API_KEY_HASHES)

        fresh_fernet = Fernet.generate_key().decode('utf-8')
        assert not _is_prohibited_hash(fresh_fernet, _PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES)


class TestFieldEncryptionKeyValidationAndRotation:
    def test_single_valid_fernet_key_passes(self, settings):
        """A single valid Fernet key instantiates correctly without error."""
        valid_key = Fernet.generate_key().decode('utf-8')
        settings.FIELD_ENCRYPTION_KEY = valid_key
        reset_fernet()
        cipher = get_fernet()
        assert cipher is not None

    def test_comma_separated_key_pair_passes(self, settings):
        """A comma-separated pair of valid Fernet keys instantiates MultiFernet cipher."""
        key1 = Fernet.generate_key().decode('utf-8')
        key2 = Fernet.generate_key().decode('utf-8')
        settings.FIELD_ENCRYPTION_KEY = f"{key1},{key2}"
        reset_fernet()
        cipher = get_fernet()
        assert cipher is not None

    def test_invalid_string_fails_with_helpful_message(self, settings):
        """An invalid key string raises ImproperlyConfigured with generation instructions and no leaked key."""
        settings.FIELD_ENCRYPTION_KEY = "invalid-corrupted-key-string-999"
        reset_fernet()
        with pytest.raises(ImproperlyConfigured) as exc_info:
            get_fernet()
        err_msg = str(exc_info.value)
        assert "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" in err_msg
        assert "invalid-corrupted-key-string-999" not in err_msg
        reset_fernet()

    def test_zero_downtime_rotating_decryption(self, settings):
        """Encrypts data with an old key, updates key list to <new>,<old>, and verifies seamless decryption."""
        old_key = Fernet.generate_key().decode('utf-8')
        new_key = Fernet.generate_key().decode('utf-8')

        # 1. Encrypt with old key
        settings.FIELD_ENCRYPTION_KEY = old_key
        reset_fernet()
        raw_secret_token = "secret-hubspot-api-token-xyz-12345"
        ciphertext_under_old_key = encrypt_secret(raw_secret_token)
        assert ciphertext_under_old_key != raw_secret_token

        # 2. Configure rotation with new key primary and old key secondary
        settings.FIELD_ENCRYPTION_KEY = f"{new_key},{old_key}"
        reset_fernet()

        # 3. Transparently decrypt token encrypted under old key
        decrypted_token = decrypt_secret(ciphertext_under_old_key)
        assert decrypted_token == raw_secret_token

        # 4. Encrypting a new secret now uses the primary (new) key
        new_ciphertext = encrypt_secret("another-secret-token")
        assert decrypt_secret(new_ciphertext) == "another-secret-token"

        # 5. Verify the old key alone CANNOT decrypt the newly encrypted token
        settings.FIELD_ENCRYPTION_KEY = old_key
        reset_fernet()
        with pytest.raises(ValueError, match="Failed to decrypt stored credential"):
            decrypt_secret(new_ciphertext)

        # Cleanup
        reset_fernet()


class TestRotateSecretsScript:
    def test_rotate_secrets_against_temporary_env(self, tmp_path, monkeypatch):
        """Tests that rotate_secrets updates values, creates backup, and preserves other settings."""
        env_file = tmp_path / '.env'
        initial_fernet = Fernet.generate_key().decode('utf-8')
        env_file.write_text(
            f"# Test environment file\n"
            f"DEBUG=True\n"
            f"SECRET_KEY=initial-secret-key-3891823719283719283719283\n"
            f"API_KEY_SECRET=initial-api-key-91823719283719283719283\n"
            f"FIELD_ENCRYPTION_KEY={initial_fernet}\n"
            f"DATABASE_URL=postgresql://localhost:5432/testdb\n",
            encoding='utf-8',
        )

        # Ensure git tracking check considers this temporary file untracked
        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)

        out_buffer = StringIO()
        monkeypatch.setattr(sys, 'stdout', out_buffer)

        exit_code = rotate_secrets_main(['--env-file', str(env_file)])
        assert exit_code == 0

        stdout_text = out_buffer.getvalue()
        # Verify no secret values are printed
        assert "initial-secret-key" not in stdout_text
        assert "initial-api-key" not in stdout_text
        assert initial_fernet not in stdout_text
        assert "Rotated SECRET_KEY" in stdout_text
        assert "Rotated API_KEY_SECRET" in stdout_text
        assert "Rotated FIELD_ENCRYPTION_KEY" in stdout_text

        # Verify backup was created
        backups = list(tmp_path.glob('.env.bak-*'))
        assert len(backups) == 1
        assert "initial-secret-key" in backups[0].read_text(encoding='utf-8')

        # Verify updated .env
        updated_content = env_file.read_text(encoding='utf-8')
        assert "DEBUG=True" in updated_content
        assert "DATABASE_URL=postgresql://localhost:5432/testdb" in updated_content
        assert "initial-secret-key" not in updated_content
        assert "initial-api-key" not in updated_content

        # Verify FIELD_ENCRYPTION_KEY preserved old key
        fernet_line = next(line for line in updated_content.splitlines() if line.startswith('FIELD_ENCRYPTION_KEY='))
        keys = fernet_line.split('=', 1)[1].split(',')
        assert len(keys) == 2
        assert keys[1] == initial_fernet

    def test_rotate_secrets_drop_old_encryption_key_flag(self, tmp_path, monkeypatch):
        """Tests that --drop-old-encryption-key replaces with only the new key."""
        env_file = tmp_path / '.env'
        initial_fernet = Fernet.generate_key().decode('utf-8')
        env_file.write_text(
            f"FIELD_ENCRYPTION_KEY={initial_fernet}\n",
            encoding='utf-8',
        )

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)
        monkeypatch.setattr('scripts.rotate_secrets.check_dependent_encrypted_data', lambda primary_key=None: 0)

        exit_code = rotate_secrets_main(['--env-file', str(env_file), '--drop-old-encryption-key'])
        assert exit_code == 0

        updated_content = env_file.read_text(encoding='utf-8')
        fernet_line = next(line for line in updated_content.splitlines() if line.startswith('FIELD_ENCRYPTION_KEY='))
        keys = fernet_line.split('=', 1)[1].split(',')
        assert len(keys) == 1
        assert keys[0] != initial_fernet

    def test_refuses_to_run_on_non_env_file(self, tmp_path):
        """Refuses to run if target file is not named '.env'."""
        bad_file = tmp_path / 'custom.env'
        bad_file.write_text("FOO=BAR\n", encoding='utf-8')
        with pytest.raises(ValueError, match="Target file must be named exactly '.env'"):
            rotate_secrets(bad_file)

    def test_refuses_to_run_on_git_tracked_file(self, tmp_path, monkeypatch):
        """Refuses to run if target file is tracked in git repository."""
        env_file = tmp_path / '.env'
        env_file.write_text("FOO=BAR\n", encoding='utf-8')
        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: True)
        with pytest.raises(RuntimeError, match="tracked in git repository"):
            rotate_secrets(env_file)


class TestRepositorySanitization:
    def test_no_tracked_files_contain_blocked_hashes_or_plaintext_leaked_keys(self):
        """Scans all git-tracked files (excluding test suites and docs) for prohibited hashes or raw secrets."""
        repo_root = Path(__file__).resolve().parent.parent.parent.parent
        git_files_output = subprocess.check_output(
            ['git', 'ls-files'],
            cwd=str(repo_root),
            text=True,
        )
        tracked_files = [line.strip() for line in git_files_output.splitlines() if line.strip()]

        # Filter out tests, git configs, migrations, or documentation
        scannable_files = [
            f for f in tracked_files
            if not f.startswith(('apps/extraction/tests/', '.github/', 'docs/'))
            and not f.endswith(('.md', '.rst', '.png', '.jpg', '.lock', '.gitignore'))
            and f != 'config/settings.py'  # settings.py contains the SHA-256 blocklists themselves
        ]

        all_blocked_hashes = (
            _PROHIBITED_SECRET_KEY_HASHES
            | _PROHIBITED_API_KEY_HASHES
            | _PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES
        )

        for rel_path in scannable_files:
            file_path = repo_root / rel_path
            if not file_path.is_file():
                continue
            content = file_path.read_text(encoding='utf-8', errors='ignore')

            # Check if any line in python files assigns a hardcoded secret whose hash matches a blocked hash
            assignments = re.findall(r'(?:SECRET_KEY|API_KEY_SECRET|FIELD_ENCRYPTION_KEY)\s*=\s*["\']([^"\']+)["\']', content)
            for raw_val in assignments:
                h = hashlib.sha256(raw_val.strip().encode('utf-8')).hexdigest()
                assert h not in all_blocked_hashes, (
                    f"File {rel_path} contains a credential value matching prohibited hash {h}"
                )


@pytest.mark.django_db
class TestKeyRotationSafeguardsAndReencryption:
    def test_count_jobs_dependent_on_old_keys(self, settings):
        """Identifies jobs whose encrypted token cannot be decrypted with the primary key alone."""
        old_key = Fernet.generate_key().decode('utf-8')
        new_key = Fernet.generate_key().decode('utf-8')
        settings.FIELD_ENCRYPTION_KEY = f"{new_key},{old_key}"
        reset_fernet()

        fernet_old = Fernet(old_key.encode('utf-8'))
        fernet_new = Fernet(new_key.encode('utf-8'))

        # Job 1: encrypted with old key
        ExtractionJob.objects.create(
            encrypted_token=fernet_old.encrypt(b"token-under-old-key").decode('utf-8'),
            token_hash=ExtractionJob.hash_token("token-under-old-key"),
            dedup_hash=ExtractionJob.compute_dedup_hash("token-under-old-key"),
        )
        # Job 2: encrypted with new key
        ExtractionJob.objects.create(
            encrypted_token=fernet_new.encrypt(b"token-under-new-key").decode('utf-8'),
            token_hash=ExtractionJob.hash_token("token-under-new-key"),
            dedup_hash=ExtractionJob.compute_dedup_hash("token-under-new-key"),
        )
        # Job 3: empty token
        ExtractionJob.objects.create(
            encrypted_token="",
            token_hash="hash3",
            dedup_hash="dedup3",
        )

        # Under new_key alone, exactly 1 job fails decryption
        assert ExtractionService.count_jobs_dependent_on_old_keys(primary_key=new_key) == 1
        # Under old_key alone, exactly 1 job fails decryption
        assert ExtractionService.count_jobs_dependent_on_old_keys(primary_key=old_key) == 1

        reset_fernet()

    def test_reencrypt_all_jobs_migrates_tokens_to_primary_key(self, settings):
        """Re-encrypts all jobs using MultiFernet fallback, making them decryptable with primary key alone."""
        old_key = Fernet.generate_key().decode('utf-8')
        new_key = Fernet.generate_key().decode('utf-8')
        settings.FIELD_ENCRYPTION_KEY = f"{new_key},{old_key}"
        reset_fernet()

        fernet_old = Fernet(old_key.encode('utf-8'))

        job = ExtractionJob.objects.create(
            encrypted_token=fernet_old.encrypt(b"my-api-token").decode('utf-8'),
            token_hash=ExtractionJob.hash_token("my-api-token"),
            dedup_hash=ExtractionJob.compute_dedup_hash("my-api-token"),
        )

        assert ExtractionService.count_jobs_dependent_on_old_keys(primary_key=new_key) == 1

        # Perform re-encryption with primary key
        count = ExtractionService.reencrypt_all_jobs(primary_key=new_key)
        assert count == 1

        # Now zero jobs depend on old keys
        assert ExtractionService.count_jobs_dependent_on_old_keys(primary_key=new_key) == 0

        # Verify plaintext token is preserved exactly
        job.refresh_from_db()
        assert job.get_decrypted_token() == "my-api-token"

        reset_fernet()

    def test_reencrypt_tokens_management_command(self, settings):
        """Verifies reencrypt_tokens management command in verify-only and active execution modes."""
        old_key = Fernet.generate_key().decode('utf-8')
        new_key = Fernet.generate_key().decode('utf-8')
        settings.FIELD_ENCRYPTION_KEY = f"{new_key},{old_key}"
        reset_fernet()

        fernet_old = Fernet(old_key.encode('utf-8'))

        ExtractionJob.objects.create(
            encrypted_token=fernet_old.encrypt(b"cmd-token").decode('utf-8'),
            token_hash=ExtractionJob.hash_token("cmd-token"),
            dedup_hash=ExtractionJob.compute_dedup_hash("cmd-token"),
        )

        out = StringIO()
        # verify-only exits with error when records depend on old keys
        with pytest.raises(SystemExit) as exc_info:
            call_command('reencrypt_tokens', '--verify-only', stdout=out)
        assert exc_info.value.code == 1

        # Active re-encryption succeeds
        out = StringIO()
        call_command('reencrypt_tokens', stdout=out)
        assert "Successfully re-encrypted and verified 1 job token(s)" in out.getvalue()

        # verify-only now succeeds cleanly
        out = StringIO()
        call_command('reencrypt_tokens', '--verify-only', stdout=out)
        assert "Safe to drop old keys" in out.getvalue()

        reset_fernet()

    def test_rotate_secrets_refuses_to_drop_key_when_records_depend_on_it(self, tmp_path, monkeypatch):
        """rotate_secrets refuses to drop old key if database records still depend on fallback keys."""
        env_file = tmp_path / '.env'
        initial_fernet = Fernet.generate_key().decode('utf-8')
        env_file.write_text(f"FIELD_ENCRYPTION_KEY={initial_fernet}\n", encoding='utf-8')

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)
        # Mock check_dependent_encrypted_data returning 3 dependent records
        monkeypatch.setattr('scripts.rotate_secrets.check_dependent_encrypted_data', lambda primary_key=None: 3)

        with pytest.raises(RuntimeError, match=r"Cannot drop old encryption key: 3 stored job\(s\) depend"):
            rotate_secrets(env_file, keep_old_encryption_key=False)

    def test_rotate_secrets_force_drop_bypasses_safeguard(self, tmp_path, monkeypatch):
        """--force-drop flag allows dropping old keys despite dependent database records."""
        env_file = tmp_path / '.env'
        initial_fernet = Fernet.generate_key().decode('utf-8')
        env_file.write_text(f"FIELD_ENCRYPTION_KEY={initial_fernet}\n", encoding='utf-8')

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)
        monkeypatch.setattr('scripts.rotate_secrets.check_dependent_encrypted_data', lambda primary_key=None: 5)

        result = rotate_secrets(env_file, keep_old_encryption_key=False, force_drop=True)
        assert "old keys dropped" in result['FIELD_ENCRYPTION_KEY']

    def test_rotate_secrets_reencrypt_flag_reencrypts_before_dropping(self, tmp_path, monkeypatch):
        """--reencrypt flag re-encrypts dependent data before dropping old keys."""
        env_file = tmp_path / '.env'
        initial_fernet = Fernet.generate_key().decode('utf-8')
        env_file.write_text(f"FIELD_ENCRYPTION_KEY={initial_fernet}\n", encoding='utf-8')

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)

        reencrypt_called = []
        checks = [2, 0]  # Initially 2 dependent, then 0 after re-encryption

        def mock_check(primary_key=None):
            return checks.pop(0) if checks else 0

        def mock_reencrypt(primary_key=None):
            reencrypt_called.append(True)
            return 2

        monkeypatch.setattr('scripts.rotate_secrets.check_dependent_encrypted_data', mock_check)
        monkeypatch.setattr('scripts.rotate_secrets.reencrypt_stored_data', mock_reencrypt)

        result = rotate_secrets(env_file, keep_old_encryption_key=False, reencrypt=True)
        assert reencrypt_called == [True]
        assert "old keys dropped" in result['FIELD_ENCRYPTION_KEY']

    def test_rotate_secrets_prune_old_keys(self, tmp_path, monkeypatch):
        """--prune-old-keys retains primary key and drops fallback keys without generating a new key."""
        env_file = tmp_path / '.env'
        key1 = Fernet.generate_key().decode('utf-8')
        key2 = Fernet.generate_key().decode('utf-8')
        env_file.write_text(f"FIELD_ENCRYPTION_KEY={key1},{key2}\n", encoding='utf-8')

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)
        monkeypatch.setattr('scripts.rotate_secrets.check_dependent_encrypted_data', lambda primary_key=None: 0)

        result = rotate_secrets(env_file, prune_old_keys_only=True)
        assert "retained primary key" in result['FIELD_ENCRYPTION_KEY']

        updated_content = env_file.read_text(encoding='utf-8')
        assert f"FIELD_ENCRYPTION_KEY={key1}\n" in updated_content
        assert key2 not in updated_content

    def test_check_dependent_encrypted_data_zero_records(self, db):
        """check_dependent_encrypted_data returns 0 when no stored jobs depend on old keys."""
        valid_key = Fernet.generate_key().decode('utf-8')
        count = check_dependent_encrypted_data(primary_key=valid_key)
        assert count == 0

    def test_check_dependent_encrypted_data_existing_records(self, db):
        """check_dependent_encrypted_data accurately counts jobs encrypted with old keys."""
        old_key = Fernet.generate_key().decode('utf-8')
        new_key = Fernet.generate_key().decode('utf-8')
        f_old = Fernet(old_key.encode('utf-8'))

        ExtractionJob.objects.create(
            encrypted_token=f_old.encrypt(b"token-dep-1").decode('utf-8'),
            token_hash=ExtractionJob.hash_token("token-dep-1"),
            dedup_hash=ExtractionJob.compute_dedup_hash("token-dep-1"),
        )
        ExtractionJob.objects.create(
            encrypted_token=f_old.encrypt(b"token-dep-2").decode('utf-8'),
            token_hash=ExtractionJob.hash_token("token-dep-2"),
            dedup_hash=ExtractionJob.compute_dedup_hash("token-dep-2"),
        )

        count = check_dependent_encrypted_data(primary_key=new_key)
        assert count == 2

    def test_check_dependent_encrypted_data_fails_closed_on_db_failure(self, monkeypatch):
        """check_dependent_encrypted_data raises RuntimeError on database failure (fails closed, never returns 0)."""
        def mock_failing_query(*args, **kwargs):
            raise RuntimeError("Database connection refused: [Errno 111] Connection refused")

        monkeypatch.setattr(ExtractionService, 'count_jobs_dependent_on_old_keys', mock_failing_query)

        valid_key = Fernet.generate_key().decode('utf-8')
        with pytest.raises(RuntimeError) as exc_info:
            check_dependent_encrypted_data(primary_key=valid_key)
        assert "Failing closed" in str(exc_info.value)
        assert "Database connection refused" in str(exc_info.value)

    def test_check_dependent_encrypted_data_fails_closed_on_missing_key(self, settings):
        """check_dependent_encrypted_data raises RuntimeError when primary_key is missing or invalid."""
        settings.FIELD_ENCRYPTION_KEY = None
        with pytest.raises(RuntimeError) as exc_info:
            check_dependent_encrypted_data(primary_key="")
        assert "Failing closed" in str(exc_info.value)

    def test_count_jobs_dependent_handles_malformed_ciphertext(self, db):
        """Malformed ciphertext in encrypted_token fails decryption and is counted as dependent."""
        key = Fernet.generate_key().decode('utf-8')
        ExtractionJob.objects.create(
            encrypted_token="malformed_non_fernet_garbage_ciphertext",
            token_hash=ExtractionJob.hash_token("malformed"),
            dedup_hash=ExtractionJob.compute_dedup_hash("malformed"),
        )

        count = ExtractionService.count_jobs_dependent_on_old_keys(primary_key=key)
        assert count == 1

    def test_rotate_secrets_dry_run_makes_no_modifications(self, tmp_path, monkeypatch):
        """--dry-run simulates the secret rotation without creating backup files or modifying target .env."""
        env_file = tmp_path / '.env'
        original_content = "SECRET_KEY=original-secret-key-12345678901234567890\nFIELD_ENCRYPTION_KEY=old-key\n"
        env_file.write_text(original_content, encoding='utf-8')

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)

        result = rotate_secrets(env_file, dry_run=True)
        assert "[DRY RUN]" in result['FIELD_ENCRYPTION_KEY']
        assert "[DRY RUN]" in result['backup']
        assert "dry_run" in result

        # Verify .env was not modified
        assert env_file.read_text(encoding='utf-8') == original_content

        # Verify no backup file was created
        backup_files = list(tmp_path.glob('.env.bak-*'))
        assert len(backup_files) == 0

    def test_rotate_secrets_interrupted_rotation_preserves_backup(self, tmp_path, monkeypatch):
        """If rotation is interrupted before completion, the timestamped backup remains intact for rollback."""
        env_file = tmp_path / '.env'
        original_content = "SECRET_KEY=original-secret-key-12345678901234567890\nFIELD_ENCRYPTION_KEY=old-key\n"
        env_file.write_text(original_content, encoding='utf-8')

        monkeypatch.setattr('scripts.rotate_secrets.is_tracked_by_git', lambda p: False)

        # Simulate interruption during output lines preparation
        def mock_error_during_processing(*args, **kwargs):
            raise OSError("Disk write failed: unexpected interruption")

        # Monkeypatch Path.write_text on the target to simulate error after backup creation
        original_write_text = Path.write_text

        def failing_write_text(self, data, *args, **kwargs):
            if self.name == '.env':
                raise OSError("Disk write failed: disk full or power failure")
            return original_write_text(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, 'write_text', failing_write_text)

        with pytest.raises(OSError, match="Disk write failed"):
            rotate_secrets(env_file, dry_run=False)

        # Backup file was already written before target modification and is intact!
        backup_files = list(tmp_path.glob('.env.bak-*'))
        assert len(backup_files) == 1
        assert backup_files[0].read_text(encoding='utf-8') == original_content

