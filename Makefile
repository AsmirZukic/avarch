.PHONY: doctor scan test lint format typecheck check docker-build wrapper-init wrapper-doctor

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

wrapper-init:
	./bin/avarch init

wrapper-doctor:
	./bin/avarch doctor
