# GreenTree Data Extraction Service

Production-quality Django REST API backend built for the GreenTree / Springer Capital backend development practical assessment. The application implements an asynchronous, resilient data extraction pipeline with deterministic testing, state machine transitions, filter-aware deduplication, decoupled external client adapters, API key authentication, and complete OpenAPI/Swagger documentation.

---

## Table of Contents

- [Overview](#overview)
- [Technology Stack](#technology-stack)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation & Local Setup](#installation--local-setup)
- [Environment Variables](#environment-variables)
- [API Authentication for Callers](#api-authentication-for-callers)
- [Background Execution & Extraction Worker](#background-execution--extraction-worker)
- [Database Setup & Migrations](#database-setup--migrations)
- [Running the Application](#running-the-application)
- [API Documentation (Swagger / ReDoc)](#api-documentation-swagger--redoc)
- [API Endpoints Reference](#api-endpoints-reference)
- [Testing Strategy & Execution](#testing-strategy--execution)
- [Security & Compliance](#security--compliance)
- [Deployment Guidelines](#deployment-guidelines)
- [Assumptions & Design Decisions](#assumptions--design-decisions)

---

## Overview

The **Data Extraction Service** facilitates ingestion and extraction of entity records (contacts, leads, CRM entries) from third-party API providers into structured, queryable relational data.

Key capabilities:
1. **Asynchronous Scan Initiation (`/api/v1/scan/start`)**: Accepts extraction requests with token hashing and filter-aware deduplication, immediately returning `202 Accepted` while dispatching background extraction.
2. **Field-Level Token Encryption & Transient Lifecycle**: Stores external API tokens encrypted at rest via AES-Fernet (`FIELD_ENCRYPTION_KEY`), strictly isolating caller credentials (`X-API-KEY`) from external CRM tokens. Encrypted tokens are decrypted in-memory only during extraction and automatically scrubbed upon reaching terminal states (`COMPLETED`, `CANCELLED`, `FAILED`).
3. **Background Execution Pipeline & Concurrency**: Automatically runs extractions via a managed bounded thread pool or dedicated multi-worker processes using atomic conditional database claims (`claim_next_pending_job`) to prevent duplicate processing.
4. **Stale Job Recovery**: Automatically detects and transitions abandoned or stalled worker jobs older than `EXTRACTION_JOB_TIMEOUT_MINUTES` to `FAILED` with diagnostic code `WORKER_ABANDONED_TIMEOUT`.
5. **Multi-Page External Client with Retries**: Paginates through external endpoints, with automatic exponential backoff retries on HTTP 429 (Too Many Requests) and 5xx server errors, respecting `Retry-After` headers.
6. **API Key Authentication**: Protects all mutation and retrieval endpoints using constant-time comparison against `API_KEY_SECRET` (`X-API-KEY` or `Authorization: Api-Key <key>`). Fails closed if unconfigured.
7. **Status Polling & Progress Tracking (`/api/v1/scan/status/<job_id>`)**: Real-time lifecycle monitoring (`pending`, `in_progress`, `completed`, `failed`, `cancelled`) with sanitized error details that never leak internal tracebacks.
8. **Paginated Data Retrieval (`/api/v1/scan/result/<job_id>`)**: Enforces state validation (rejects incomplete jobs with `409 Conflict`), returning paginated entity records.
9. **Controlled Cancellation (`/api/v1/scan/cancel/<job_id>`)**: Enforces strict state machine rules (prevents cancelling completed or failed jobs).
10. **Data Cleanup & Purge (`/api/v1/scan/remove/<job_id>`)**: Atomic cascade deletion of extraction jobs and records.
11. **Aggregated Analytics & Reporting (`/api/v1/jobs/statistics`, `/api/v1/jobs/analytics`)**: Single-query database aggregations for operational insights.
12. **Operational Health Probes (`/api/v1/health`, `/health/liveness`, `/health/readiness`)**: Consolidated and standalone Kubernetes-ready health, liveness, and readiness probes.

---

## Technology Stack

- **Language:** Python 3.12+ / Python 3.14 compatible
- **Web Framework:** Django 5.1+ / 6.0+
- **REST Framework:** Django REST Framework (DRF) 3.15+
- **Database:** PostgreSQL (with automatic zero-dependency SQLite fallback for instant local setup)
- **Configuration:** `python-decouple` & `dj-database-url` (strict environment variable separation)
- **API Documentation:** `drf-spectacular` (OpenAPI 3.0 schema generation + Swagger UI + ReDoc with ApiKeyAuth)
- **Test Automation:** `pytest`, `pytest-django`, `factory-boy`, `faker`
- **WSGI / Production Server:** `gunicorn`
- **CI / CD:** GitHub Actions with PostgreSQL matrix testing

---

## Architecture

The project adheres to a clean, decoupled **Layered Service Architecture**:

```
┌────────────────────────────────────────────────────────┐
│                      HTTP Clients                      │
└───────────────────────────┬────────────────────────────┘
                            │ (X-API-KEY Authentication)
                            ▼
┌────────────────────────────────────────────────────────┐
│                   API Views / URLs                     │  apps/extraction/views.py
│  - HTTP Status Codes (200, 202, 204, 400, 401, 404, 409│
│  - Swagger / OpenAPI AutoSchema annotations            │
└─────────────┬───────────────────────────┬──────────────┘
              │                           │
              ▼                           ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│   Serializers & DTOs     │  │   Centralized Handler    │  apps/common/exceptions.py
│  apps/extraction/        │  │  - Predictable error JSON │
│  serializers.py          │  │  - Never leaks tracebacks │
└─────────────┬────────────┘  └──────────────────────────┘
              │
              ▼
┌────────────────────────────────────────────────────────┐
│                 Business Service Layer                 │  apps/extraction/services.py
│  - ExtractionService (Job lifecycle & atomic workflows)│
│  - Filter-aware deduplication & state validation       │
│  - Asynchronous background worker dispatch             │
└─────────────┬───────────────────────────┬──────────────┘
              │                           │
              ▼                           ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│   External HTTP Client   │  │    Query Selectors       │  apps/extraction/selectors.py
│  apps/extraction/        │  │  - Single-query aggregates│
│  clients.py              │  │  - N+1 prevention        │
│  - Multi-page pagination │  │  - Filtering & pagination │
│  - 429/5xx backoff retry │  └───────────┬──────────────┘
│  - Token masking in logs │              │
└──────────────────────────┘              ▼
                              ┌──────────────────────────┐
                              │     Database Models      │  apps/extraction/models.py
                              │  - State machine rules   │
                              │  - Composite indexes     │
                              │  - Unique constraints    │
                              │  - dedup_hash (filters)  │
                              └──────────────────────────┘
```

---

## Project Structure

```
Django-backend-application-/
│
├── manage.py                   # Django administration utility
├── requirements.txt            # Production and testing dependencies
├── pytest.ini                  # Pytest configuration
├── .env.example                # Safe environment variable template
├── .gitignore                  # Git ignore rules
├── README.md                   # Technical documentation
│
├── config/                     # Core project configuration
│   ├── __init__.py
│   ├── settings.py             # Decoupled production settings
│   ├── urls.py                 # Root URL configuration + Swagger
│   ├── wsgi.py                 # WSGI entrypoint for Gunicorn
│   └── asgi.py                 # ASGI entrypoint
│
├── apps/
│   ├── __init__.py
│   ├── common/                 # Reusable cross-cutting concerns
│   │   ├── apps.py
│   │   ├── authentication.py   # ApiKeyAuthentication backend
│   │   ├── permissions.py      # HasServiceApiKey permission class
│   │   ├── exceptions.py       # Custom domain exceptions & handler
│   │   └── pagination.py       # Standard LimitOffset pagination
│   │
│   └── extraction/             # Data extraction domain
│       ├── apps.py
│       ├── admin.py            # Django Admin registration
│       ├── models.py           # ExtractionJob & ExtractedRecord models
│       ├── serializers.py      # Request/response serializers & validation
│       ├── views.py            # REST API endpoints
│       ├── urls.py             # Route definitions
│       ├── services.py         # Business orchestration & lifecycle
│       ├── selectors.py        # Optimized ORM queries & aggregations
│       ├── clients.py          # ThirdPartyExtractionClient adapter
│       │
│       ├── management/
│       │   └── commands/
│       │       └── run_extraction_worker.py # Standalone polling worker
│       │
│       ├── migrations/
│       │   ├── __init__.py
│       │   ├── 0001_initial.py
│       │   └── 0002_extractionjob_dedup_hash_and_more.py
│       │
│       └── tests/              # Automated test suite (72+ tests)
│           ├── __init__.py
│           ├── conftest.py     # Seeded fixtures & mock data
│           ├── factories.py    # FactoryBoy model factories
│           ├── test_models.py  # Model constraints & state machines
│           ├── test_clients.py # External client, pagination & retry mocks
│           ├── test_services.py# Service orchestration, deduplication & worker
│           ├── test_api_scan.py# Scan workflow, auth & async execution tests
│           ├── test_api_jobs.py# Jobs listing, filters, & statistics
│           ├── test_edge_cases.py# Edge cases from Section 7 of guidelines
│           └── test_performance.py# N+1 query count assertions
│
└── .github/
    └── workflows/
        └── tests.yml           # GitHub Actions CI matrix with PostgreSQL
```

---

## Requirements

- Python 3.12 or newer
- PostgreSQL (recommended for production) or SQLite (default for instant local development)
- Git

---

## Installation & Local Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/JaswanthG-10/Django-backend-application-.git
   cd Django-backend-application-
   ```

2. **Create and activate a virtual environment:**
   ```bash
   # On macOS/Linux:
   python3 -m venv .venv
   source .venv/bin/activate

   # On Windows (PowerShell):
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. **Install project dependencies:**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```

---

## Environment Variables

All settings are configured using decoupled environment variables. See [.env.example](file:///.env.example):

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `SECRET_KEY` | *(Required in prod, dev fallback in dev)* | Django cryptographic secret key. |
| `DEBUG` | `True` | Boolean flag toggling Django debug mode. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0,testserver` | Comma-separated list of allowed hostnames. |
| `CSRF_TRUSTED_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated list of trusted origins for CSRF. |
| `API_KEY_SECRET` | *(Required in prod, dev fallback in dev)* | Service API key required for incoming API calls (fail-closed). Shared dev keys prohibited in prod. |
| `FIELD_ENCRYPTION_KEY` | *(Derived from SECRET_KEY if dev)* | 32-byte urlsafe base64 Fernet key for encrypting tokens at rest. Generate via: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `EXTRACTION_ASYNC_EXECUTION` | `True` | Automatically dispatches background worker pool on scan start. |
| `EXTRACTION_MAX_WORKERS` | `4` | Maximum concurrent worker threads in the bounded extraction pool. |
| `EXTRACTION_JOB_TIMEOUT_MINUTES` | `15` | Minutes before an abandoned in-progress job is marked timed out / recovered. |
| `EXTRACTION_MAX_RETRIES` | `3` | Maximum automatic retries for interrupted or stale jobs. |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | Database connection URL (`postgresql://user:pass@host:5432/db` for Postgres / Supabase). |
| `EXTERNAL_API_BASE_URL` | `https://jsonplaceholder.typicode.com` | Base URL of third-party CRM provider or public data source. |
| `EXTERNAL_API_TIMEOUT` | `10` | Timeout in seconds for external API requests. |
| `EXTERNAL_API_DEFAULT_TOKEN` | `test-sample-service-token` | Default token for example data source. |
| `EXTERNAL_API_PAGINATION_TYPE` | `auto` | Pagination strategy: `auto`, `cursor` (e.g. HubSpot), `offset_limit`, or `page_number`. |
| `EXTERNAL_API_ENDPOINT_TEMPLATE` | `/posts` | Endpoint path or formatting template for provider requests. |
| `EXTERNAL_API_RECORDS_KEY` | `""` | Optional response dictionary key holding records list (e.g. `results` or `data`). |
| `API_REQUIRE_REQUEST_SIGNING` | `False` | Enforce cryptographic HMAC-SHA256 request signing on all protected endpoints. |
| `API_SIGNING_TOLERANCE_SECONDS` | `300` | Permitted drift in seconds for `X-Timestamp` validation window. |
| `LOG_LEVEL` | `INFO` | Logging threshold level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

## API Authentication & Request Signing

All protected API endpoints require callers to authenticate using the configured `API_KEY_SECRET`.

### 1. Fail-Closed Authentication
Authentication is strictly **fail-closed**: if `API_KEY_SECRET` is unset, empty, or unconfigured on the server, all requests to protected endpoints are rejected with `401 Unauthorized`. Known default development keys are explicitly prohibited from running in production (`DEBUG=False`).

Provide the API key via any of the following HTTP headers:
* `X-API-KEY: <your-api-key>`
* `Authorization: Api-Key <your-api-key>`
* `Authorization: Bearer <your-api-key>`

### 2. Cryptographic HMAC-SHA256 Request Signing
For zero-trust environments, cryptographic request signing can be enforced (`API_REQUIRE_REQUEST_SIGNING=True`) or verified whenever signatures are supplied:
* Header `X-Timestamp`: Current UTC UNIX timestamp (e.g., `1773584820`). Requests outside the tolerance window (`API_SIGNING_TOLERANCE_SECONDS`, default 300s) are rejected with `401`.
* Header `X-Signature`: HMAC-SHA256 signature generated with `API_KEY_SECRET` over the canonical string:
  ```
  <timestamp>.<HTTP_METHOD>.<PATH>.<SHA256_HEX(request_body)>
  ```

Unauthenticated requests receive `401 Unauthorized`. The operational health check (`/api/v1/health`) and OpenAPI schema views (`/api/docs/`) remain public for monitoring and documentation.

---

## Background Execution & Extraction Worker

When `/api/v1/scan/start` is called:
1. The endpoint validates input, hashes the token and filters, and persists the job in state `pending`.
2. It returns `202 Accepted` with the `job_id` immediately.
3. Because `EXTRACTION_ASYNC_EXECUTION=True`, a task is submitted to a **managed bounded ThreadPoolExecutor** (configurable via `EXTRACTION_MAX_WORKERS`, default: 4) after database commit (`transaction.on_commit`). This avoids unbounded thread spawning, ensures graceful process teardown, transitions the job to `in_progress`, paginates through records, and marks the job `completed`.
4. Subsequent calls to `/api/v1/scan/status/<job_id>` report progress, record count, and retry metrics, and `/api/v1/scan/result/<job_id>` serves the extracted records.

### Unified Atomic Job Claiming
Both execution paths (the inline ThreadPoolExecutor worker and the standalone background worker command) share the unified, atomic claiming implementation (`ExtractionService.claim_job()`). Jobs are claimed using atomic conditional updates (`status='pending' -> status='in_progress'`), guaranteeing that multiple concurrent threads or worker processes will never double-process or race on the same job.

### Automated Stale Job Recovery & Safe Retries
Stale or interrupted jobs stranded in `in_progress` past `EXTRACTION_JOB_TIMEOUT_MINUTES` are automatically recovered during job initiation and worker cycles:
- **Idempotent Retry Decision**: If an abandoned job has valid encrypted credentials and `retry_count < max_retries`, any partial extracted records are purged and the job is re-queued to `pending` with incremented `retry_count`.
- **Permanent Failure**: If max retries are exceeded or credentials are unavailable, the job transitions permanently to `failed` with code `MAX_RETRIES_EXCEEDED` or `WORKER_ABANDONED_TIMEOUT`.

### Public Demo Endpoint Token Protection
When `EXTERNAL_API_BASE_URL` points to public testing/sandbox endpoints (e.g., `jsonplaceholder.typicode.com`, `reqres.in`, `httpbin.org`), the HTTP client automatically omits the `Authorization` header to prevent leaking customer or service tokens across the public internet. For real CRM/custom endpoints, tokens are safely sent via `Authorization: Bearer <token>`.

### Standalone Polling Worker Command
In multi-container or distributed environments, background execution can be handed off entirely to dedicated worker processes (`EXTRACTION_ASYNC_EXECUTION=False` on web nodes):

```bash
# Continuous polling loop (every 2 seconds) with stale job recovery enabled
python manage.py run_extraction_worker --poll-interval 2.0 --recover-stale

# Run one pass and exit
python manage.py run_extraction_worker --once --recover-stale
```

---

## Database Setup & Migrations

Run database migrations to apply the initial schema:

```bash
python manage.py migrate
```

To create a superuser for accessing the Django Admin:
```bash
python manage.py createsuperuser
```

---

## Running the Application

Start the local development server:
```bash
python manage.py runserver 8000
```

The service is accessible at `http://127.0.0.1:8000/`.

---

## API Documentation (Swagger / ReDoc)

Interactive OpenAPI 3.0 documentation is auto-generated via `drf-spectacular` with `ApiKeyAuth` header support:

- **Swagger UI:** [http://127.0.0.1:8000/api/docs/](http://127.0.0.1:8000/api/docs/)
- **ReDoc:** [http://127.0.0.1:8000/api/redoc/](http://127.0.0.1:8000/api/redoc/)
- **Raw OpenAPI Schema (YAML/JSON):** [http://127.0.0.1:8000/api/schema/](http://127.0.0.1:8000/api/schema/)

---

## API Endpoints Reference

| Method | Endpoint | Description | Expected Status Codes |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/scan/start` | Initiates new extraction job and triggers background execution | `202 Accepted`, `400`, `401`, `409` |
| `GET` | `/api/v1/scan/status/<job_id>` | Retrieves execution status, progress %, and metrics | `200 OK`, `401`, `404` |
| `GET` | `/api/v1/scan/result/<job_id>` | Fetches paginated extracted records for completed job | `200 OK`, `401`, `404`, `409` |
| `POST` | `/api/v1/scan/cancel/<job_id>` | Cancels a pending or in-progress extraction job | `200 OK`, `401`, `404`, `409` |
| `DELETE` | `/api/v1/scan/remove/<job_id>` | Deletes extraction job and all associated records | `204 No Content`, `401`, `404` |
| `GET` | `/api/v1/jobs/jobs` | Lists historical extraction jobs with filters & pagination | `200 OK`, `401` |
| `GET` | `/api/v1/jobs/statistics` | Returns aggregated metrics across all jobs | `200 OK`, `401` |
| `GET` | `/api/v1/jobs/analytics` | Provides analytics breakdown for charts & dashboard | `200 OK`, `401` |
| `GET` | `/api/v1/health` | Consolidated operational health probe (status & subsystem checks) | `200 OK`, `503` (Public) |
| `GET` | `/api/v1/health/liveness` | Container liveness check (200 OK when process is running) | `200 OK` (Public) |
| `GET` | `/api/v1/health/readiness` | Container readiness check (200 OK / 503 Service Unavailable) | `200 OK`, `503` (Public) |

### Standard Error Envelope

All API errors return a uniform structure:
```json
{
  "error": {
    "code": "RESOURCE_NOT_FOUND",
    "message": "Job with ID '...' not found.",
    "details": {}
  }
}
```

---

## Testing Strategy & Execution

The test suite is built strictly against the **[GreenTree Test Guidelines V1.md](https://github.com/greentreegroup/policy/blob/main/TEST-GUIDELINES-V1.md)**.

### Running Pytest
```bash
pytest -v
```

### Test Categories Covered (89 Automated Tests)

1. **API Authentication & Fail-Closed Security Tests:**
   - Verifies missing API key returns `401 Unauthorized`.
   - Verifies invalid API key returns `401 Unauthorized`.
   - Verifies strict fail-closed rejection when `API_KEY_SECRET` is unset/unconfigured.
   - Verifies public health check remains accessible without credentials.
   - Verifies separation between service caller credentials (`X-API-KEY`) and external data provider credentials (`api_token` / `X-API-Token`).
2. **Field-Level Encryption & Token Scrubbing Lifecycle:**
   - External tokens are symmetrically encrypted at rest via AES-Fernet (`FIELD_ENCRYPTION_KEY`).
   - Verifies external tokens are never stored plaintext in the database or exposed via API serializers.
   - Verifies encrypted tokens are automatically purged upon reaching terminal states (`COMPLETED`, `CANCELLED`, `FAILED`).
   - Comprehensive token masking tests: tokens `<= 8` characters masked to `***`, longer tokens masked to `tok***1234`.
3. **Worker Concurrency & Reliability Tests:**
   - Multi-worker atomic job claiming (`claim_next_pending_job`) verified with concurrent racing transactions.
   - Automated stale job recovery: orphaned `in_progress` jobs older than timeout are transitioned to `failed`.
   - Mid-flight cancellation safety: jobs cancelled during network polling abort cleanly without overwriting state.
   - Accurate database record counting: `record_count` reflects actual rows committed in `ExtractedRecord`.
4. **Health & Diagnostic Probe Tests:**
   - Dedicated tests for `/api/v1/health`, `/api/v1/health/liveness`, and `/api/v1/health/readiness`.
   - Simulates database disconnection returning `503 Service Unavailable`.
5. **Background Worker & ThreadPoolExecutor Tests:**
   - Tests end-to-end background extraction with bounded worker pool: triggering `/scan/start`, async execution, and retrieving completed records via `/scan/result/<job_id>`.
   - Tests bounded `ThreadPoolExecutor` lifecycle, max worker concurrency, and graceful shutdown.
   - Tests `manage.py run_extraction_worker --once` polling execution.
6. **Filter-Aware Deduplication Tests:**
   - Scans with different `filters` create distinct jobs.
   - Scans with identical `filters` (even with reordered JSON keys) reuse the existing active job.
7. **Error Sanitization Tests:**
   - Failed jobs store clean diagnostic codes in `error_details` without leaking internal Python exceptions, database passwords, or filesystem tracebacks.
8. **Third-Party Client Unit Tests:**
   - Multi-page pagination traversal across pages.
   - Exponential backoff retries on HTTP 429 (respecting `Retry-After`) and 5xx server errors.
   - Token masking in logs.
   - Token protection: verifying Authorization headers are omitted when calling public demo/mock endpoints.
9. **Scan Workflow Integration Tests:**
   - Steps 1 through 8 from GreenTree test guidelines (`start` → `status` → `result` → `cancel` → `remove` → verify 404).
10. **Edge Case Tests:**
    - Missing, empty, whitespace-only, or short API tokens (`400 Bad Request`).
    - Malformed JSON payloads (`400 Bad Request`).
    - Non-existent UUIDs and SQL injection / non-standard ID attempts (`404 Not Found`).
    - Accessing results on incomplete or failed jobs (`409 Conflict` with `JOB_NOT_COMPLETED`).
    - Cancelling already completed, failed, or cancelled jobs (`409 Conflict`).
    - Concurrent duplicate scans with `strict_deduplication=True` (`409 Conflict`).
    - Extreme pagination bounds (oversized limit bounded to 100, out-of-range offset).
11. **N+1 Query Performance Tests:**
    - Query count assertions using `django_assert_num_queries` ensuring result listing and job listing maintain constant query counts regardless of dataset volume.

---

## Security & Compliance

- **Caller Authentication:** Mandatory API Key (`X-API-KEY` or `Authorization: Api-Key <key>`) validated with constant-time comparison (`secrets.compare_digest`). Fails closed if missing or unconfigured.
- **External Credential Isolation:** The caller's service key (`X-API-KEY`) is strictly isolated from third-party CRM tokens (`api_token` in request body or `X-API-Token` header).
- **Encrypted Token at Rest:** Third-party provider tokens are stored in the database exclusively as AES-Fernet encrypted ciphertext, decrypted in-memory only during active extraction, and purged immediately on terminal status.
- **No Plaintext Secret Persistence:** The raw API token is never stored in plaintext in the database or exposed via serialized API responses. A deterministic SHA-256 hash (`token_hash`) is indexed for deduplication, and a masked representation (`tok***1234` or `***`) is kept for audit visibility.
- **Traceback & Error Sanitization:** In production, unhandled errors and failed extraction jobs return sanitized JSON diagnostics without exposing Python stack traces, internal paths, or framework internals.
- **Input Sanitization:** URL parameters and payload fields undergo strict validation and UUID parsing before reaching the ORM.

---

## Deployment Guidelines

### Running with Gunicorn (Linux / Container)
```bash
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 4 --timeout 60
```

### Dockerfile (Production Ready)
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "4"]
```

---

## Assumptions & Design Decisions

1. **Filter-Aware Deduplication:** Deduplication computes a composite SHA-256 hash of `api_token + source_type + normalized_filters`. Different filters create distinct jobs, whereas identical filters prevent duplicate concurrent scans.
2. **Asynchronous Execution:** By default (`EXTRACTION_ASYNC_EXECUTION=True`), jobs are picked up immediately by a background worker thread upon trigger, allowing zero-dependency local and production execution without requiring Celery/Redis, while also providing a standalone `run_extraction_worker` management command for separated worker architectures.
3. **Pagination Standard:** `StandardLimitOffsetPagination` was chosen to align with the GreenTree test guidelines (`limit` and `offset` query parameters) while bounding requests to a safe upper limit of 100 records per page.
4. **Database Flexibility:** The application defaults to SQLite locally for zero-friction setup and evaluation, while seamlessly connecting to PostgreSQL when `DATABASE_URL` is set in production or GitHub Actions CI.
