IMAGE ?= game-server
TAG   ?= dev
PORT  ?= 8000

.DEFAULT_GOAL := help
.PHONY: help install run dev lint format typecheck test coverage check db-up db-down db-migrate db-migrate-test db-info db-reset db-new-migration migration-guard eval-referee docker-build docker-run clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create/sync the virtualenv from uv.lock (incl. dev deps)
	uv sync

run: ## Run the server (settings from env / .env)
	uv run python -m game_server

dev: ## Run the server with auto-reload
	uv run uvicorn game_server.app:app --reload --port $(PORT)

lint: ## Lint and auto-fix with ruff
	uv run ruff check . --fix

format: ## Format with ruff
	uv run ruff format .

typecheck: ## Type-check with mypy
	uv run mypy .

test: db-migrate-test ## Run the test suite (migrates the test database first)
	uv run pytest

coverage: db-migrate-test ## Run tests with a coverage report
	uv run pytest --cov=src --cov-report=term-missing

check: lint format typecheck test ## Lint, format, type-check and test

DB_CONTAINER ?= game-server-db
DB_PORT       ?= 5432

db-up: ## Start a local PostgreSQL in Docker (game_server and game_server_test databases)
	docker run -d --name $(DB_CONTAINER) -p $(DB_PORT):5432 \
		-e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=game_server postgres:16
	@until docker exec $(DB_CONTAINER) pg_isready -h localhost -U postgres >/dev/null 2>&1; do sleep 1; done
	docker exec $(DB_CONTAINER) createdb -U postgres game_server_test

db-down: ## Stop and remove the local PostgreSQL (its data goes with it)
	docker rm -f $(DB_CONTAINER)

# Flyway: one exactly pinned image for every caller (local, PR CI, the deploy).
FLYWAY_IMAGE    ?= flyway/flyway:13.10.0
# Where Flyway runs: by default inside the `make db-up` container's network, so localhost is
# that PostgreSQL (on macOS and Linux alike). CI passes FLYWAY_NETWORK=host or its own network.
FLYWAY_NETWORK  ?= container:$(DB_CONTAINER)
FLYWAY_URL      ?= jdbc:postgresql://localhost:5432/game_server
FLYWAY_TEST_URL ?= jdbc:postgresql://localhost:5432/game_server_test
FLYWAY_USER     ?= postgres
FLYWAY_PASSWORD ?= postgres
export FLYWAY_USER FLYWAY_PASSWORD
# Credentials go in as `-e NAME`, never as values on a command line or in a log.
FLYWAY_RUN = docker run --rm --network $(FLYWAY_NETWORK) -e FLYWAY_URL -e FLYWAY_USER \
	-e FLYWAY_PASSWORD -v "$(CURDIR)/db:/flyway/project:ro" $(FLYWAY_IMAGE) \
	-configFiles=/flyway/project/flyway.toml
FLYWAY      = FLYWAY_URL='$(FLYWAY_URL)' $(FLYWAY_RUN)
FLYWAY_TEST = FLYWAY_URL='$(FLYWAY_TEST_URL)' $(FLYWAY_RUN)

db-migrate: ## Apply pending migrations (FLYWAY_URL: the local database by default)
	$(FLYWAY) migrate

db-migrate-test: ## Apply pending migrations to the test database (what the tests use)
	$(FLYWAY_TEST) migrate

db-info: ## Show applied and pending migrations (FLYWAY_URL)
	$(FLYWAY) info

# The only place clean is allowed, and always on the local container's databases, whatever
# FLYWAY_URL, FLYWAY_USER, FLYWAY_PASSWORD or FLYWAY_NETWORK say.
DB_RESET_FLYWAY = docker run --rm --network container:$(DB_CONTAINER) \
	-e FLYWAY_USER=postgres -e FLYWAY_PASSWORD=postgres \
	-v "$(CURDIR)/db:/flyway/project:ro" $(FLYWAY_IMAGE) -configFiles=/flyway/project/flyway.toml \
	-cleanDisabled=false

db-reset: ## DESTRUCTIVE, local only: drop everything in the local databases, then migrate
	$(DB_RESET_FLYWAY) -url=jdbc:postgresql://localhost:5432/game_server clean migrate
	$(DB_RESET_FLYWAY) -url=jdbc:postgresql://localhost:5432/game_server_test clean migrate

migration-guard: ## The CI guard-rails for migrations, against BASE (default origin/main)
	uv run python tools/check_migrations.py --base $(or $(BASE),origin/main)

db-new-migration: ## Start the next migration: NAME=add_something → db/migrations/V<next>__add_something.sql
	@echo "$(NAME)" | grep -Eq '^[a-z0-9]+(_[a-z0-9]+)*$$' || { echo "Usage: make db-new-migration NAME=lower_snake_case" >&2; exit 2; }
	@next=$$(( $$(ls db/migrations | sed -nE 's/^V([0-9]+)__.*\.sql$$/\1/p' | sort -n | tail -1) + 1 )); \
	file="db/migrations/V$${next}__$(NAME).sql"; \
	printf -- '-- V%s: %s.\n--\n-- Never edit this file once it'"'"'s on main: change the schema in a new migration.\n\n' "$$next" "$(NAME)" > "$$file"; \
	echo "Created $$file"

RUNS ?= 1

eval-referee: ## Run the referee eval: EVAL_DIR=... [MODEL=...] [RUNS=1] (real API calls; costs money)
	@test -n "$(EVAL_DIR)" || { echo "Usage: make eval-referee EVAL_DIR=path/to/eval [MODEL=...] [RUNS=1]"; exit 2; }
	uv run python -m game_server.evals.referee --eval-dir "$(EVAL_DIR)" --runs $(RUNS) $(if $(MODEL),--model $(MODEL))

docker-build: ## Build the Docker image ($(IMAGE):$(TAG))
	docker build -t $(IMAGE):$(TAG) .

docker-run: ## Run the Docker image, publishing $(PORT)
	docker run --rm -p $(PORT):8000 $(IMAGE):$(TAG)

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
