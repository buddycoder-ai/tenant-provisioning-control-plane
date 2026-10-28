.PHONY: up down reset test
up:
	docker compose up --build -d
down:
	docker compose down
reset:
	docker compose down -v
test:
	docker compose run --rm -e DATABASE_URL=sqlite+pysqlite:///:memory: api sh -c "pip install '.[dev]' && ruff check app tests && ruff format --check app tests && mypy app && bandit -q -r app -s B104,B311 && pytest"
