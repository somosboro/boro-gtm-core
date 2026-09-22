.PHONY: up down migrate import seed score test lint api clean

up:            ## Start Postgres and the API
	docker compose up --build -d

down:
	docker compose down

migrate:
	alembic upgrade head

import:
	python -m boro_gtm.cli market-intelligence import ./data/boro_market_intelligence_top50.json

seed:
	python -m boro_gtm.cli strategy seed

score:
	python -m boro_gtm.cli market-intelligence recalculate \
		--snapshot MI-2026-09-21-V1 --model market-attractiveness:1.0 \
		--mode reference_reproduction

test:
	pytest -q

lint:
	ruff check packages tests

api:
	uvicorn boro_gtm.api.main:app --reload --port 8000

clean:
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
