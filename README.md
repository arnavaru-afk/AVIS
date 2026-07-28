# AVIS

AVIS, the Adaptive Valuation Intelligence System, is a production-oriented research platform for Indian equities. It combines a governed security master, data ingestion and normalization pipelines, point-in-time market history, valuation engines, data quality gates, and authenticated APIs so analysts can move from raw exchange data to explainable intrinsic value outputs in one stack.

The platform is designed to support both engineering and research workflows. On the backend, AVIS manages reference data, ingestion orchestration, compliance-aware APIs, and valuation persistence. On the frontend, the Global Market Terminal provides analyst-facing modules for fundamentals, valuation, technical analysis, macro and CAPM, and AVIS operations monitoring.

## Quick Start

1. Start the stack:

```bash
docker compose up -d
```

2. Seed development reference data:

```bash
docker compose exec api python scripts/bootstrap.py
```

3. Generate a development JWT if you want to inspect or override the terminal token manually:

```bash
python scripts/dev_token.py
```

4. Open the terminal:

[http://localhost:8501](http://localhost:8501)

## Development Bootstrap

The compose stack can self-derive the test JWT public key and a matching terminal token from `tests/test_api.py`. If you want explicit local overrides, create an untracked `.env.dev` file using the template below:

```dotenv
DATABASE_URL=postgresql+psycopg://avis:avis_dev@localhost:5432/avis
AVIS_JWT_PUBLIC_KEY=<TEST_PUBLIC_KEY from tests/test_api.py>
AVIS_ENV=dev
LOG_LEVEL=INFO
AVIS_BASE_URL=http://localhost:8000
AVIS_JWT_TOKEN=<output of scripts/dev_token.py>
AVIS_ENABLED=true
AVIS_ANALYST_ID=9d2b8ea1-3af7-4d10-9c78-19ab4d119001
```

## Module Overview

1. Fundamental Deep Dive
2. Valuation Modeler
3. Technical Analysis
4. Macro & CAPM
5. AVIS Operations

## Architecture Overview

AVIS is split into a PostgreSQL-backed data core, an Alembic-managed schema layer, ingestion and normalization pipelines under `etl/`, domain logic under `avis/core/`, a FastAPI reporting and control plane under `apps/api/`, and a Streamlit analyst terminal under `apps/terminal/`. Docker Compose orchestrates the local development deployment so database, API, and terminal components boot with a consistent configuration.

## Tech Stack

| Layer | Technology |
| --- | --- |
| Database | PostgreSQL 16 |
| ORM / Migrations | SQLAlchemy 2, Alembic |
| API | FastAPI, Uvicorn |
| Frontend | Streamlit |
| Data / ETL | pandas, requests, custom connectors |
| Auth | RS256 JWT via PyJWT |
| Packaging | setuptools / pyproject.toml |
| Containers | Docker, Docker Compose |

## Specs

Detailed architecture, database, ingestion, normalization, valuation, and reporting specifications live under [`specs/`](specs/).
