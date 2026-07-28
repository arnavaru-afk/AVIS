#!/bin/sh
set -eu

if [ -z "${AVIS_JWT_PUBLIC_KEY:-}" ]; then
  export AVIS_JWT_PUBLIC_KEY="$(python -c 'from scripts.dev_token import load_test_public_key; print(load_test_public_key())')"
fi

alembic upgrade head
exec uvicorn apps.api.main:app --host 0.0.0.0 --port 8000
