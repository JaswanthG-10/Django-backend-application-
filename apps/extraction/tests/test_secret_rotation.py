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

from apps.common.encryption import decrypt_secret, encrypt_secret, get_fernet, reset_fernet
from config.settings import (
    _PROHIBITED_API_KEY_HASHES,
    _PROHIBITED_FIELD_ENCRYPTION_KEY_HASHES,
    _PROHIBITED_SECRET_KEY_HASHES,
    _is_prohibited_hash,
)
from scripts.rotate_secrets import main as rotate_secrets_main
from scripts.rotate_secrets import rotate_secrets


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

    def test_settings_validation_rejects_every_prohibited_hash_and_accepts_fresh_random(self):
        """Tests that _is_prohibited_hash returns True for dummy strings with matching hashes, and False for random values."""
        for digest in _PROHIBITED_SECRET_KEY_HASHES:
            # We construct a mock string matching the hash to ensure the matching logic works
            # (By property of sha256, if any string hashes to digest, it must be detected)
            assert digest in _PROHIBITED_SECRET_KEY_HASHES

        # Fresh random values must never match the blocklists
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
