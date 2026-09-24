IMAGE ?= game-server
TAG   ?= dev
PORT  ?= 8000

.DEFAULT_GOAL := help
.PHONY: help install run dev lint format typecheck test coverage check docker-build docker-run clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

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

test: ## Run the test suite
	uv run pytest

coverage: ## Run tests with a coverage report
	uv run pytest --cov=src --cov-report=term-missing

check: lint format typecheck test ## Lint, format, type-check and test

docker-build: ## Build the Docker image ($(IMAGE):$(TAG))
	docker build -t $(IMAGE):$(TAG) .

docker-run: ## Run the Docker image, publishing $(PORT)
	docker run --rm -p $(PORT):8000 $(IMAGE):$(TAG)

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist build
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
