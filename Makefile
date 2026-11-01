PYTHON ?= python3.11
SUITES ?= legit,policy_edges,chaos
RUNS   ?= 3
VENV   ?= .venv
BIN    := $(VENV)/bin

.PHONY: help venv install lint fmt test test-docker up down logs migrate check-secrets clean \
	eval-naive eval-guarded summarise-naive summarise-guarded report fetch-results approvals

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

eval-naive:  ## run the naive agent over SUITES x RUNS (resumable; needs an LLM key + Postgres)
	$(BIN)/python -m tollgate.eval run --mode naive --suites $(SUITES) --runs $(RUNS)

eval-guarded:  ## run the guarded agent over SUITES x RUNS (resumable; needs an LLM key + Postgres)
	$(BIN)/python -m tollgate.eval run --mode guarded --suites $(SUITES) --runs $(RUNS)

summarise-naive:  ## rebuild results/eval_naive.json from per-scenario results
	$(BIN)/python -m tollgate.eval summarise --mode naive --suites $(SUITES) --runs $(RUNS)

summarise-guarded:  ## rebuild results/eval_guarded.json from per-scenario results
	$(BIN)/python -m tollgate.eval summarise --mode guarded --suites $(SUITES) --runs $(RUNS)

approvals:  ## list refunds waiting for a human (approve/reject: python -m tollgate.approvals)
	$(BIN)/python -m tollgate.approvals list

report:  ## regenerate README results tables from results/*.json
	$(BIN)/python -m tollgate.eval report

fetch-results:  ## download an eval workflow artifact into results/ and cache/ (RUN_ID=...)
	rm -rf .eval-download
	gh run download $(RUN_ID) -D .eval-download
	cp -R .eval-download/*/results/. results/
	mkdir -p cache/llm && cp -R .eval-download/*/cache/llm/. cache/llm/
	rm -rf .eval-download

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .hypothesis
