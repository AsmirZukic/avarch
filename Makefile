.PHONY: doctor scan test lint format typecheck check docker-build docker-init docker-doctor

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
	docker build -t avarch .

docker-init:
	docker run --rm --user "$(DOCKER_USER)" -v "$(PWD):/work" avarch init --config /work/avarch.toml

docker-doctor:
	docker run --rm --user "$(DOCKER_USER)" -v "$(PWD):/work" avarch doctor --config /work/avarch.toml
