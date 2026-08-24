.PHONY: install dev test lint fmt run check doctor docker-build docker-up docker-logs

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	pytest -q

lint:
	ruff check src tests

fmt:
	ruff check --fix src tests
	ruff format src tests

run:
	james run

check:
	james check

doctor:
	james doctor

docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-logs:
	docker compose logs -f
