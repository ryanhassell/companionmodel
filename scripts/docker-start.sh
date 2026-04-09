#!/usr/bin/env bash
set -euo pipefail

echo "[startup] Applying database migrations..."
uv run alembic upgrade head

echo "[startup] Seeding default prompts/persona..."
uv run python -m scripts.seed_defaults

if [[ -n "${ADMIN_BOOTSTRAP_USERNAME:-}" && -n "${ADMIN_BOOTSTRAP_PASSWORD:-}" ]]; then
  echo "[startup] Bootstrapping admin user from environment..."
  if ! uv run python -m scripts.bootstrap_admin \
      --username "${ADMIN_BOOTSTRAP_USERNAME}" \
      --password "${ADMIN_BOOTSTRAP_PASSWORD}"; then
    echo "[startup] Admin bootstrap skipped (likely already exists)."
  fi
else
  echo "[startup] ADMIN_BOOTSTRAP_USERNAME/PASSWORD not set; skipping admin bootstrap."
fi

echo "[startup] Starting API server..."
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
