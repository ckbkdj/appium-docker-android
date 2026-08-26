.PHONY: install test lint run mcp

install:
	python -m pip install -e '.[dev]'

test:
	python -m pytest -q

lint:
	python -m ruff check src tests

run:
	semantic-mobile-agent

mcp:
	semantic-mobile-mcp
