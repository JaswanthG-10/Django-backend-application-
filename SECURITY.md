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

## Secrets That Must Be Rotated Before Production Use

> **CRITICAL SECURITY NOTICE**: Any secrets committed to this repository's earlier git history are compromised and must not be used in any production environment.

Before deploying this service to production, you must generate and configure brand-new, unique secrets for each of the following:

1. **`SECRET_KEY`**: Django cryptographic signing secret.
2. **`API_KEY_SECRET`**: Ingestion and scan initiation API key / HMAC signing secret.
3. **`FIELD_ENCRYPTION_KEY`**: 32-byte URL-safe base64 Fernet symmetric key for at-rest token encryption.
4. **`EXTERNAL_API_DEFAULT_TOKEN`**: Third-party provider access token.
5. **Database Password (`DATABASE_URL`)**: PostgreSQL database connection credentials.

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

### 3. Rotating `FIELD_ENCRYPTION_KEY` (MultiFernet Zero-Downtime Rotation)

The `FIELD_ENCRYPTION_KEY` encrypts sensitive external tokens stored in `ExtractionJob.encrypted_token`. The system natively supports zero-downtime key rotation using `cryptography.fernet.MultiFernet` with comma-separated keys.

1. Generate a new 32-byte URL-safe base64 Fernet key:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
2. Set `FIELD_ENCRYPTION_KEY` in your production environment to a comma-separated list with the **new key first** and the **old key second**:
   ```env
   FIELD_ENCRYPTION_KEY=<new_primary_key>,<old_fallback_key>
   ```
   - **Encryption** always uses the first (primary) key.
   - **Decryption** automatically attempts keys in order, successfully decrypting existing database tokens encrypted under the previous key.
   - When a job token is accessed and saved or re-encrypted, it is written using the primary key.
3. Once all legacy encrypted rows have been re-encrypted or the corresponding jobs have reached terminal status (at which point tokens are automatically purged), remove `<old_fallback_key>` from `FIELD_ENCRYPTION_KEY`.

---

### 4. Rotating Database Credentials (`DATABASE_URL`)

1. Provision new credentials in your PostgreSQL database (e.g. Supabase connection pooler or direct URI).
2. Verify connectivity with the new URI before deploying.
3. Update `DATABASE_URL` in the production environment.
4. Restart application processes.
5. Revoke the old database user credentials.

---

## SSRF Hardening & Network Egress Controls

The extraction client enforces strict network egress controls and anti-SSRF protections to defend internal network infrastructure:

### 1. Host Allowlisting (`EXTERNAL_API_ALLOWED_HOSTS`)
- **Default (`[]`)**: When empty, the client permits connections only to the hostname specified in `EXTERNAL_API_BASE_URL`.
- **Configured list**: Set `EXTERNAL_API_ALLOWED_HOSTS` to a comma-separated list of permitted hostnames (e.g. `api.hubapi.com,crm-provider.com`). Any pagination next-URL or HTTP redirect target outside this allowlist is immediately rejected.

### 2. Loopback Access Controls (`EXTERNAL_API_ALLOW_LOOPBACK`)
- **Default (`False`)**: Loopback destinations (`127.0.0.1`, `localhost`, `[::1]`) are blocked.
- **Local testing only**: `EXTERNAL_API_ALLOW_LOOPBACK=True` is permitted **only** when `DEBUG=True`.
- **Production enforcement**: In production (`DEBUG=False`), setting `EXTERNAL_API_ALLOW_LOOPBACK=True` causes Django startup to fail immediately with `ImproperlyConfigured`.

### 3. DNS Rebinding Defense (Anti-TOCTOU)
- Target hostnames are resolved and validated once.
- The validated IP is pinned to the connection via `PinnedIPAdapter`, preventing subsequent DNS rebinding attacks while preserving TLS SNI and certificate verification.
- All resolved IP addresses are inspected: if any resolved address belongs to private, loopback, link-local, CGNAT (`100.64.0.0/10`), IPv6 ULA (`fc00::/7`), or cloud metadata ranges, the request fails closed.

### 4. Redirect Following
- HTTP redirects are followed manually up to a maximum of 3 hops (`allow_redirects=False`).
- Each redirect target undergoes full SSRF validation, cycle detection, and `Authorization` header stripping on cross-host transitions.
