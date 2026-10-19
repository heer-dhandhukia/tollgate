PYTHON ?= python3.11
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: help venv install lint fmt test test-docker up down logs migrate check-secrets clean

help:  ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

venv:  ## create the virtualenv
	$(PYTHON) -m venv $(VENV)

install: venv  ## install pinned runtime + dev dependencies
	$(BIN)/pip install -r requirements-dev.txt -e .

lint:  ## ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:  ## auto-fix lint and format
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

test:  ## run tests (needs Postgres at TEST_PG_ADMIN_URL, e.g. `make up`)
	$(BIN)/pytest -q

test-docker:  ## run tests inside docker compose
	docker compose run --rm --build test

up:  ## start postgres, migrations, mockpay and the api
	docker compose up -d --build

down:  ## stop everything
	docker compose down

logs:  ## follow service logs
	docker compose logs -f

migrate:  ## apply database migrations locally
	$(BIN)/alembic upgrade head

check-secrets:  ## refuse staged .env files or API-key-looking strings
	./scripts/check_staged_secrets.sh

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .hypothesis
