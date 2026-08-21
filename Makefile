.PHONY: test lint format format-check typecheck check docker-build docker-test clean-cache clean-env

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright
	uv run pytest

docker-build:
	docker build -t avarch:latest .

docker-test:
	scripts/test-container.sh avarch:latest

clean-cache:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete
	rm -rf .pytest_cache .ruff_cache dist build
	find . -maxdepth 2 -type d -name "*.egg-info" -prune -exec rm -rf {} +

clean-env:
	rm -rf .venv .uv-cache
