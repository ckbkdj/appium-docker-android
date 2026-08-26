.PHONY: install test lint run docker

install:
	python -m pip install -e '.[all,dev]'

test:
	pytest -q

lint:
	ruff check src tests

run:
	mobile-agent-api

docker:
	docker compose up -d --build
