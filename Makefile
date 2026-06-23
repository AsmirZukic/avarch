.PHONY: doctor scan test lint format typecheck check docker-build docker-test wrapper-init wrapper-doctor clean clean-cache clean-all clean-env clean-preview

DOCKER_USER := $(shell id -u):$(shell id -g)

doctor:
	uv run avarch doctor

scan:
	uv run avarch scan

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run pyright

check:
	uv run ruff check .
	uv run pyright
	uv run pytest

docker-build:
	docker build -t avarch:latest .

docker-test:
	scripts/test-container.sh avarch:latest

wrapper-init:
	./bin/avarch init

wrapper-doctor:
	./bin/avarch doctor

clean-preview:
	@echo "clean removes caches, build outputs, and local Avarch workspace/media artifacts."
	@echo "clean-cache removes only caches, pyc files, build outputs, logs, and tmp files."
	@echo "clean-all is an alias for clean."
	@echo "clean-env removes .venv and .uv-cache."

clean: clean-cache
	rm -rf .avarch avarch.toml profiles media

clean-cache:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
	rm -rf .pytest_cache .ruff_cache .mypy_cache .pyright htmlcov dist build
	rm -f coverage.xml
	find . -maxdepth 2 -type d -name "*.egg-info" -prune -exec rm -rf {} +
	find . -maxdepth 2 -type f \( -name "*.log" -o -name "*.tmp" -o -name "*.temp" \) -delete

clean-all: clean

clean-env:
	rm -rf .venv .uv-cache
