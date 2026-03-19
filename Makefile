PYTHON ?= python3
UV ?= uv

.PHONY: sync run lint test migrate upgrade seed bootstrap fmt

sync:
	$(UV) sync --dev

run:
	$(UV) run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

lint:
	$(UV) run ruff check .

test:
	$(UV) run pytest

migrate:
	$(UV) run alembic revision --autogenerate -m "$(m)"

upgrade:
	$(UV) run alembic upgrade head

seed:
	$(UV) run python -m scripts.seed_defaults

bootstrap:
	$(UV) run python -m scripts.bootstrap_admin
