.PHONY: install dev test build check contracts demo
install:
	uv sync --frozen
	uv run --frozen python scripts/fetch_vks_local_model.py
	npm ci
dev:
	bash scripts/dev.sh
test:
	uv run pytest -q
	npm test
build:
	npm run build
check: test build
contracts:
	uv run python scripts/freeze_v2_contracts.py
	uv run python scripts/freeze_v3_contracts.py
	uv run python scripts/build_contracts.py
demo:
	uv run python scripts/build_demo.py
