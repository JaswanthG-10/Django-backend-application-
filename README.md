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
2. **Background Execution Pipeline**: Automatically runs extractions in a background worker thread upon trigger, or via the `run_extraction_worker` polling management command.
3. **Multi-Page External Client with Retries**: Paginates through external endpoints, with automatic exponential backoff retries on HTTP 429 (Too Many Requests) and 5xx server errors, respecting `Retry-After` headers.
4. **API Key Authentication**: Protects all mutation and retrieval endpoints using constant-time comparison against `API_KEY_SECRET` (`X-API-KEY` or `Authorization: Api-Key <key>`).
5. **Status Polling & Progress Tracking (`/api/v1/scan/status/<job_id>`)**: Real-time lifecycle monitoring (`pending`, `in_progress`, `completed`, `failed`, `cancelled`) with sanitized error details that never leak internal tracebacks.
6. **Paginated Data Retrieval (`/api/v1/scan/result/<job_id>`)**: Enforces state validation (rejects incomplete jobs with `409 Conflict`), returning paginated entity records.
7. **Controlled Cancellation (`/api/v1/scan/cancel/<job_id>`)**: Enforces strict state machine rules (prevents cancelling completed or failed jobs).
8. **Data Cleanup & Purge (`/api/v1/scan/remove/<job_id>`)**: Atomic cascade deletion of extraction jobs and records.
9. **Aggregated Analytics & Reporting (`/api/v1/jobs/statistics`, `/api/v1/jobs/analytics`)**: Single-query database aggregations for operational insights.
10. **Production Health Check (`/api/v1/health`)**: Public liveness and readiness probe with database connection verification.

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
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | Comma-separated list of allowed hostnames. |
| `CSRF_TRUSTED_ORIGINS` | `http://localhost:8000,http://127.0.0.1:8000` | Comma-separated list of trusted origins for CSRF. |
| `API_KEY_SECRET` | *(Required in prod, dev fallback in dev)* | Service API key required for incoming API calls (fail-closed). |
| `EXTRACTION_ASYNC_EXECUTION` | `True` | Automatically dispatches background worker pool on scan start. |
| `EXTRACTION_MAX_WORKERS` | `4` | Maximum concurrent worker threads in the bounded extraction pool. |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | Database connection URL (`postgresql://user:pass@host:5432/db` for Postgres). |
| `EXTERNAL_API_BASE_URL` | `https://jsonplaceholder.typicode.com` | Base URL of third-party CRM provider or public data source. |
| `EXTERNAL_API_TIMEOUT` | `10` | Timeout in seconds for external API requests. |
| `EXTERNAL_API_DEFAULT_TOKEN` | `test-sample-service-token` | Default token for example data source. |
| `LOG_LEVEL` | `INFO` | Logging threshold level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

## API Authentication for Callers

All protected API endpoints require the caller to authenticate using the configured `API_KEY_SECRET`.

Authentication is strictly **fail-closed**: if `API_KEY_SECRET` is unset, empty, or unconfigured on the server, all requests to protected endpoints are rejected with `401 Unauthorized`.

Provide the API key via any of the following HTTP headers:
* `X-API-KEY: <your-api-key>`
* `Authorization: Api-Key <your-api-key>`
* `Authorization: Bearer <your-api-key>`

Unauthenticated requests receive `401 Unauthorized`. The operational health check (`/api/v1/health`) and OpenAPI schema views (`/api/docs/`) remain public for monitoring and documentation.

---

## Background Execution & Extraction Worker

When `/api/v1/scan/start` is called:
1. The endpoint validates input, hashes the token and filters, and persists the job in state `pending`.
2. It returns `202 Accepted` with the `job_id` immediately.
3. Because `EXTRACTION_ASYNC_EXECUTION=True`, a task is submitted to a **managed bounded ThreadPoolExecutor** (configurable via `EXTRACTION_MAX_WORKERS`, default: 4) after database commit (`transaction.on_commit`). This avoids unbounded thread spawning, ensures graceful process teardown, transitions the job to `in_progress`, paginates through records, and marks the job `completed`.
4. Subsequent calls to `/api/v1/scan/status/<job_id>` report progress and record count, and `/api/v1/scan/result/<job_id>` serves the extracted records.

### Public Demo Endpoint Token Protection
When `EXTERNAL_API_BASE_URL` points to public testing/sandbox endpoints (e.g., `jsonplaceholder.typicode.com`, `reqres.in`, `httpbin.org`), the HTTP client automatically omits the `Authorization` header to prevent leaking customer or service tokens across the public internet. For real CRM/custom endpoints, tokens are safely sent via `Authorization: Bearer <token>`.

### Standalone Polling Worker Command
Alternatively, extractions can be processed using the dedicated Django management command:
```bash
# Continuous polling loop (every 2 seconds)
python manage.py run_extraction_worker --poll-interval 2.0

# Run one pass and exit
python manage.py run_extraction_worker --once
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
| `GET` | `/api/v1/health` | Operational health check (verifies database connection) | `200 OK`, `503` (Public) |

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

### Test Categories Covered (76 Automated Tests)

1. **API Authentication & Fail-Closed Security Tests:**
   - Verifies missing API key returns `401 Unauthorized`.
   - Verifies invalid API key returns `401 Unauthorized`.
   - Verifies strict fail-closed rejection when `API_KEY_SECRET` is unset/unconfigured.
   - Verifies public health check remains accessible without credentials.
2. **Background Worker & ThreadPoolExecutor Tests:**
   - Tests end-to-end background extraction with bounded worker pool: triggering `/scan/start`, async execution, and retrieving completed records via `/scan/result/<job_id>`.
   - Tests bounded `ThreadPoolExecutor` lifecycle, max worker concurrency, and graceful shutdown.
   - Tests `manage.py run_extraction_worker --once` polling execution.
3. **Filter-Aware Deduplication Tests:**
   - Scans with different `filters` create distinct jobs.
   - Scans with identical `filters` (even with reordered JSON keys) reuse the existing active job.
4. **Error Sanitization Tests:**
   - Failed jobs store clean diagnostic codes in `error_details` without leaking internal Python exceptions, database passwords, or filesystem tracebacks.
5. **Third-Party Client Unit Tests:**
   - Multi-page pagination traversal across pages.
   - Exponential backoff retries on HTTP 429 (respecting `Retry-After`) and 5xx server errors.
   - Token masking in logs.
   - Token protection: verifying Authorization headers are omitted when calling public demo/mock endpoints.
6. **Scan Workflow Integration Tests:**
   - Steps 1 through 8 from GreenTree test guidelines (`start` → `status` → `result` → `cancel` → `remove` → verify 404).
7. **Edge Case Tests:**
   - Missing, empty, whitespace-only, or short API tokens (`400 Bad Request`).
   - Malformed JSON payloads (`400 Bad Request`).
   - Non-existent UUIDs and SQL injection / non-standard ID attempts (`404 Not Found`).
   - Accessing results on incomplete or failed jobs (`409 Conflict` with `JOB_NOT_COMPLETED`).
   - Cancelling already completed, failed, or cancelled jobs (`409 Conflict`).
   - Concurrent duplicate scans with `strict_deduplication=True` (`409 Conflict`).
   - Extreme pagination bounds (oversized limit bounded to 100, out-of-range offset).
8. **N+1 Query Performance Tests:**
   - Query count assertions using `django_assert_num_queries` ensuring result listing and job listing maintain constant query counts regardless of dataset volume.

---

## Security & Compliance

- **Caller Authentication:** Mandatory API Key (`X-API-KEY`) validated with constant-time comparison.
- **No Secret Persistence:** The raw third-party API token is never stored in plaintext in the database. A deterministic SHA-256 hash (`token_hash`) is indexed for deduplication, and a masked representation (e.g. `pat***1234`) is kept for audit visibility.
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
