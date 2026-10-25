.PHONY: setup verify run migrate migrate-docs rollback-docs

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

setup:
	@mkdir -p artifacts evidence docs db/migrations db/seed src tests
	@test -f .env.example
	@python3 -m venv .venv 2>/dev/null || true
	@. .venv/bin/activate && pip install -q -r requirements.txt
	@echo "CDRL M01 preparada."

migrate:
	@bash scripts/apply_migrations.sh

migrate-docs:
	@$(PYTHON) scripts/migrate_m07.py --aplicar

rollback-docs:
	@$(PYTHON) scripts/migrate_m07.py --rollback

verify:
	@bash scripts/verify_base.sh

run:
	@docker compose up --build
