# GreenTree Data Extraction Service

Production-quality Django REST API backend built for the GreenTree / Springer Capital backend development practical assessment. The application implements an asynchronous-ready, resilient data extraction pipeline with deterministic testing, state machine transitions, strict deduplication, decoupled external client adapters, and complete OpenAPI/Swagger documentation.

---

## Table of Contents

- [Overview](#overview)
- [Technology Stack](#technology-stack)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Installation & Local Setup](#installation--local-setup)
- [Environment Variables](#environment-variables)
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
1. **Asynchronous Scan Initiation (`/api/v1/scan/start`)**: Accepts extraction requests with token hashing and active-job deduplication, returning `202 Accepted`.
2. **Status Polling & Progress Tracking (`/api/v1/scan/status/<job_id>`)**: Real-time lifecycle monitoring (`pending`, `in_progress`, `completed`, `failed`, `cancelled`).
3. **Paginated Data Retrieval (`/api/v1/scan/result/<job_id>`)**: Enforces state validation (rejects incomplete jobs with `409 Conflict`), returning paginated entity records.
4. **Controlled Cancellation (`/api/v1/scan/cancel/<job_id>`)**: Enforces strict state machine rules (prevents cancelling completed or failed jobs).
5. **Data Cleanup & Purge (`/api/v1/scan/remove/<job_id>`)**: Atomic cascade deletion of extraction jobs and records.
6. **Aggregated Analytics & Reporting (`/api/v1/jobs/statistics`, `/api/v1/jobs/analytics`)**: Single-query database aggregations for operational insights.
7. **Production Health Check (`/api/v1/health`)**: Liveness and readiness probe with database connection verification.

---

## Technology Stack

- **Language:** Python 3.12+ / Python 3.14 compatible
- **Web Framework:** Django 5.1+ / 6.0+
- **REST Framework:** Django REST Framework (DRF) 3.15+
- **Database:** PostgreSQL (with automatic zero-dependency SQLite fallback for instant local setup)
- **Configuration:** `python-decouple` & `dj-database-url` (strict environment variable separation)
- **API Documentation:** `drf-spectacular` (OpenAPI 3.0 schema generation + Swagger UI + ReDoc)
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
                            │
                            ▼
┌────────────────────────────────────────────────────────┐
│                   API Views / URLs                     │  apps/extraction/views.py
│  - HTTP Status Codes (200, 202, 204, 400, 404, 409)    │
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
│  - Deduplication & state transition validation         │
└─────────────┬───────────────────────────┬──────────────┘
              │                           │
              ▼                           ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│   External HTTP Client   │  │    Query Selectors       │  apps/extraction/selectors.py
│  apps/extraction/        │  │  - Single-query aggregates│
│  clients.py              │  │  - N+1 prevention        │
│  - Token masking in logs │  │  - Filtering & pagination │
│  - Timeouts & retries    │  └───────────┬──────────────┘
└──────────────────────────┘              │
                                          ▼
                              ┌──────────────────────────┐
                              │     Database Models      │  apps/extraction/models.py
                              │  - State machine rules   │
                              │  - Composite indexes     │
                              │  - Unique constraints    │
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
│       ├── migrations/
│       │   ├── __init__.py
│       │   └── 0001_initial.py
│       │
│       └── tests/              # Automated test suite (60+ tests)
│           ├── __init__.py
│           ├── conftest.py     # Seeded fixtures & mock data
│           ├── factories.py    # FactoryBoy model factories
│           ├── test_models.py  # Model constraints & state machines
│           ├── test_clients.py # External client & error mocks
│           ├── test_services.py# Service orchestration & deduplication
│           ├── test_api_scan.py# Scan workflow end-to-end tests
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
   # Copy template
   cp .env.example .env
   ```

---

## Environment Variables

All settings are configured using decoupled environment variables. See [.env.example](file:///.env.example):

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `SECRET_KEY` | *(django-insecure...)* | Django cryptographic secret key (change in production). |
| `DEBUG` | `False` | Boolean flag toggling Django debug mode. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,testserver` | Comma-separated list of allowed hostnames. |
| `DATABASE_URL` | `sqlite:///db.sqlite3` | Database connection URL (`postgresql://user:pass@host:5432/db` for Postgres). |
| `EXTERNAL_API_BASE_URL` | `https://api.thirdparty-crm.com/v1` | Base URL of third-party CRM provider. |
| `EXTERNAL_API_TIMEOUT` | `10` | Timeout in seconds for external API requests. |
| `LOG_LEVEL` | `INFO` | Logging threshold level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

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

Interactive OpenAPI 3.0 documentation is auto-generated via `drf-spectacular`:

- **Swagger UI:** [http://127.0.0.1:8000/api/docs/](http://127.0.0.1:8000/api/docs/)
- **ReDoc:** [http://127.0.0.1:8000/api/redoc/](http://127.0.0.1:8000/api/redoc/)
- **Raw OpenAPI Schema (YAML/JSON):** [http://127.0.0.1:8000/api/schema/](http://127.0.0.1:8000/api/schema/)

---

## API Endpoints Reference

| Method | Endpoint | Description | Expected Status Codes |
| :--- | :--- | :--- | :--- |
| `POST` | `/api/v1/scan/start` | Initiates new extraction job or returns existing active job | `202 Accepted`, `400`, `409` |
| `GET` | `/api/v1/scan/status/<job_id>` | Retrieves execution status, progress %, and metrics | `200 OK`, `404` |
| `GET` | `/api/v1/scan/result/<job_id>` | Fetches paginated extracted records for completed job | `200 OK`, `404`, `409` |
| `POST` | `/api/v1/scan/cancel/<job_id>` | Cancels a pending or in-progress extraction job | `200 OK`, `404`, `409` |
| `DELETE` | `/api/v1/scan/remove/<job_id>` | Deletes extraction job and all associated records | `204 No Content`, `404` |
| `GET` | `/api/v1/jobs/jobs` | Lists historical extraction jobs with filters & pagination | `200 OK` |
| `GET` | `/api/v1/jobs/statistics` | Returns aggregated metrics across all jobs | `200 OK` |
| `GET` | `/api/v1/jobs/analytics` | Provides analytics breakdown for charts & dashboard | `200 OK` |
| `GET` | `/api/v1/health` | Operational health check (verifies database connection) | `200 OK`, `503` |

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

### Test Categories Covered

1. **Seeded Data Tests:**
   - Pre-populated jobs in different states (`pending`, `in_progress`, `completed`, `failed`, `cancelled`).
   - Verifies status reporting, result schemas, and statistics calculation deterministically.
2. **Scan Workflow Integration Tests:**
   - End-to-end execution of steps 1 through 8 from the GreenTree test specification.
   - Header-based and body-based API token authentication.
   - Immediate verification of state changes upon cancellation and cascading deletion upon removal.
3. **Edge Case Tests:**
   - Missing, empty, whitespace-only, or short API tokens (`400 Bad Request`).
   - Malformed JSON payloads (`400 Bad Request`).
   - Non-existent UUIDs and SQL injection / non-standard ID attempts (`404 Not Found`).
   - Fetching results on incomplete or failed jobs (`409 Conflict` with `JOB_NOT_COMPLETED`).
   - Cancelling already completed, failed, or cancelled jobs (`409 Conflict`).
   - Concurrent requests for duplicate extraction with `strict_deduplication=True` (`409 Conflict`).
   - Extreme pagination bounds (oversized limit bounded to 100, out-of-range offset).
4. **Third-Party Client Unit Tests:**
   - Isolated testing of `ThirdPartyExtractionClient` with mocked network responses.
   - Verification of HTTP 401/403, 404, 429 (rate limiting), 500/503 server errors, network timeouts, and non-JSON malformed responses.
   - Assertions ensuring authorization tokens are masked in logs and never exposed in plaintext.
5. **N+1 Query Performance Tests:**
   - Query count assertions using `django_assert_num_queries` ensuring result listing and job listing maintain constant query counts regardless of dataset volume.

---

## Security & Compliance

- **No Secret Persistence:** The raw API token is never stored in plaintext in the database. Instead, a one-way deterministic SHA-256 hash (`token_hash`) is indexed for deduplication, and a masked representation (e.g. `pat***1234`) is kept for audit visibility.
- **Traceback Protection:** In production, unhandled 500 errors return a sanitized JSON message without exposing Python stack traces or framework internals.
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

1. **Token Deduplication:** Because third-party API tokens are sensitive, we hash tokens with SHA-256 upon arrival. When a new scan is started with identical parameters and an active job already exists (`pending` or `in_progress`), the API either returns the existing job ID (idempotent behavior) or responds with `409 Conflict` if `strict_deduplication=True` is provided.
2. **Pagination Standard:** `StandardLimitOffsetPagination` was chosen to align with the GreenTree test guidelines (`limit` and `offset` query parameters) while bounding requests to a safe upper limit of 100 records per page.
3. **Database Flexibility:** The application defaults to SQLite locally for zero-friction setup and evaluation, while seamlessly connecting to PostgreSQL when `DATABASE_URL` is set in production or GitHub Actions CI.
