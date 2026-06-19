.PHONY: bootstrap setup doctor scan test lint format typecheck check

bootstrap:
	./scripts/bootstrap

setup: bootstrap

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
