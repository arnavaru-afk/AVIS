#!/bin/sh
set -eu

: "${AVIS_ANALYST_ID:=9d2b8ea1-3af7-4d10-9c78-19ab4d119001}"
export AVIS_ANALYST_ID

if [ -z "${AVIS_JWT_TOKEN:-}" ]; then
  export AVIS_JWT_TOKEN="$(python scripts/dev_token.py)"
fi

exec streamlit run apps/terminal/app.py --server.port=8501 --server.address=0.0.0.0
