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
3. Re-encrypt all existing database records using the Django management command:
   ```bash
   # Check if any database tokens require fallback keys:
   python manage.py reencrypt_tokens --verify-only

   # Re-encrypt all stored tokens under the new primary key:
   python manage.py reencrypt_tokens
   ```
4. Once verification confirms 0 records depend on old keys, prune the old fallback key:
   ```bash
   python scripts/rotate_secrets.py --prune-old-keys
   ```

---

### 4. Rotating Database Credentials (`DATABASE_URL`)

1. Provision new credentials in your PostgreSQL database (e.g. Supabase connection pooler or direct URI).
2. Verify connectivity with the new URI before deploying.
3. Update `DATABASE_URL` in the production environment.
4. Restart application processes.
5. Revoke the old database user credentials.

---

## Automated Local Rotation Script (`scripts/rotate_secrets.py`)

An automated helper script is provided to safely rotate local environment credentials without manual string generation or accidental credential leakage:

```bash
# Standard rotation: generates new SECRET_KEY, API_KEY_SECRET, and FIELD_ENCRYPTION_KEY,
# preserving the previous FIELD_ENCRYPTION_KEY as fallback (<NEW>,<OLD>):
python scripts/rotate_secrets.py

# Step 2: Restart backend services so the new primary key is active.

# Step 3: Re-encrypt existing stored database records under the new primary key:
python manage.py reencrypt_tokens

# Step 4: Safely prune old fallback keys (refuses to run if any records still depend on old keys):
python scripts/rotate_secrets.py --prune-old-keys
```

### Script Security Features & Safeguards:
- **Strict File Safety**: The script requires the target file to be named `.env` and checks `git ls-files --error-unmatch .env` to verify the file is NOT tracked by git.
- **Automated Backup**: Generates a timestamped `.env.bak-YYYYMMDDTHHMMSSZ` file before altering any lines.
- **Zero-Leakage Output**: Secret values are generated in memory and written directly to disk. Only setting names and operational summaries are printed to terminal output.
- **Data Protection Safeguard**: Refuses to drop old encryption keys if any stored database records still depend on fallback keys (preventing accidental data loss). Use `--reencrypt` to automatically re-encrypt records before dropping, or `--force-drop` to bypass.
- **Preserves Configuration**: Preserves existing file comments, spacing, and unrelated environment variables.

---

## Recommended Secret Rotation Order

When rotating secrets in production, follow this sequenced workflow to avoid service downtime or lost data:

```
1. External Providers   ->  2. Local / Stored Config  ->  3. Service Restart
   (DB Password, CRM           (Run rotate_secrets.py,       (Restart web & worker
    API Tokens, Webhooks)       update DATABASE_URL)          processes)
         |                             |                           |
         v                             v                           v
4. Verification         ->  5. Data Re-encryption     ->  6. Prune Fallback Keys
   (Health checks 200,         (python manage.py             (python rotate_secrets.py
    Verify API keys)            reencrypt_tokens)             --prune-old-keys)
```

1. **Rotate External Services First**: Provision new database credentials in your database console and generate new third-party API tokens/webhook secrets in upstream developer portals.
2. **Update Environment**: Run `python scripts/rotate_secrets.py` (or inject new secrets via your production vault/CI store) and update external URLs/tokens.
3. **Restart Application Services**: Restart all web and worker processes to reload configuration.
4. **Verify Health and Operations**: Validate that `GET /api/v1/health` returns 200 and test endpoints with the newly issued credentials.
5. **Re-encrypt Stored Data**: Re-encrypt legacy database fields using the new primary key (`python manage.py reencrypt_tokens`).
6. **Prune Legacy Fallback Keys**: Once all historical records have been re-encrypted and verified (`python manage.py reencrypt_tokens --verify-only`), run `python scripts/rotate_secrets.py --prune-old-keys` to complete the rotation cycle.

---

## External Provider Dashboards Checklist

For manual secret rotation across external provider consoles, track completion with this checklist:

- [ ] **Database Provider (e.g., Supabase / AWS RDS / Neon)**:
  - Generate new database password in provider dashboard.
  - Update `DATABASE_URL` with new credentials.
  - Verify database pooler connectivity.
  - Revoke previous user role credentials.
- [ ] **External CRM / Data Providers (e.g., HubSpot CRM / Salesforce)**:
  - Create new API access token or OAuth client credentials in developer portal.
  - Update `EXTERNAL_API_DEFAULT_TOKEN` in environment configuration.
  - Revoke obsolete API keys in provider console.
- [ ] **Third-Party Webhooks & Request Signing**:
  - Rotate HMAC webhook signing secrets in provider settings.
  - Update application verification secrets.
- [ ] **Application Restart & Deployment**:
  - Deploy updated configuration to all container/server instances.
  - Confirm logs report clean startup and healthy database connections.

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
