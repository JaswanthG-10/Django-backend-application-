# Security Policy

## Supported Versions

Only the latest release of GreenTree Data Extraction Service is currently supported for security updates.

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

---

## Reporting a Vulnerability

If you discover a security vulnerability within this project, please send an email to the security team or open a confidential advisory. Please do not report security vulnerabilities through public GitHub issues.

Include the following in your report:
- Description of the issue and potential impact
- Steps to reproduce or proof-of-concept
- Any suggested mitigations or patches

---

## Environment Separation & Secrets Hygiene

- **Never commit `.env` or secrets**: All secrets must be injected through environment variables or secure vault stores in production. `.env` is explicitly ignored in `.gitignore`.
- **Placeholder values in examples**: `.env.example` provides development placeholders and defaults. Production deployments require non-default, production-grade secrets.
- **Fail-Fast Production Validation**: If `DEBUG=False`, the application strictly refuses to start if placeholder or development keys (`SECRET_KEY`, `API_KEY_SECRET`, `FIELD_ENCRYPTION_KEY`) are detected, or if SQLite is configured.

---

## Secret Rotation Procedures

### 1. Rotating `SECRET_KEY` (Django Session & Signing Secret)

The Django `SECRET_KEY` provides cryptographic signing for sessions, password reset tokens, and CSRF tokens.

1. Generate a new cryptographically secure key:
   ```bash
   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
   ```
2. Update `SECRET_KEY` in your production secret store / environment variables.
3. Restart the application instances.
4. *Impact*: Active user sessions and pending signature tokens will be invalidated; users will be required to re-authenticate.

---

### 2. Rotating `API_KEY_SECRET` (Ingestion & Extraction API Key)

The `API_KEY_SECRET` authenticates API clients via `X-API-KEY` or HMAC request signatures.

1. Generate a new high-entropy API key secret:
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. For zero-downtime rotation:
   - Provide the new API key secret to downstream clients.
   - Once all consumers have updated their credentials, update `API_KEY_SECRET` in the backend environment variables.
   - Restart the backend instances.
3. *Impact*: Any request using old credentials will receive `401 Unauthorized`.

---

### 3. Rotating `FIELD_ENCRYPTION_KEY` (Fernet Field Encryption)

The `FIELD_ENCRYPTION_KEY` is a 32-byte URL-safe base64-encoded key used by `cryptography.fernet.Fernet` to encrypt sensitive credentials stored in the database (e.g. `ExtractionJob.encrypted_token`).

Because existing database rows are encrypted with the active key, rotating this key requires a two-key re-encryption migration:

1. Generate a new 32-byte URL-safe base64 Fernet key:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. Temporarily configure both keys in the environment:
   - `FIELD_ENCRYPTION_KEY`: The new key.
   - `OLD_FIELD_ENCRYPTION_KEY`: The previous key.
3. Run a re-encryption management script or data migration across all jobs with `encrypted_token`:
   ```python
   from cryptography.fernet import Fernet, MultiFernet
   from apps.extraction.models import ExtractionJob

   f_new = Fernet(new_key)
   f_old = Fernet(old_key)

   for job in ExtractionJob.objects.exclude(encrypted_token=''):
       try:
           decrypted = f_old.decrypt(job.encrypted_token.encode()).decode()
           job.encrypted_token = f_new.encrypt(decrypted.encode()).decode()
           job.save(update_fields=['encrypted_token'])
       except Exception:
           pass
   ```
4. Verify all records decrypt cleanly under the new key.
5. Remove `OLD_FIELD_ENCRYPTION_KEY` from the environment.

---

### 4. Rotating Database Credentials (`DATABASE_URL`)

1. Provision new credentials in your PostgreSQL database (e.g. Supabase connection pooler or direct URI).
2. Verify connectivity with the new URI before deploying.
3. Update `DATABASE_URL` in the production environment.
4. Restart application processes.
5. Revoke the old database user credentials.
