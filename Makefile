# Entry points for working on the whole repository. Each component still pins and
# installs its own dependencies; these targets run the same commands CI does.
#
#   make setup      install every component's environment
#   make demo       run the full platform loop locally, no GPU or cluster needed
#   make check      every test and lint suite CI runs (except PostgreSQL)
#   make openapi    regenerate control-plane/openapi.json after an API change
#
# PostgreSQL suite, against a database whose contents can be discarded:
#   make test-postgres DATABASE_URL=postgresql+asyncpg://fabric_app:...@localhost/fabric_test

PYTHON_VERSION ?= 3.12
RUFF := uv run --no-project --with ruff==0.8.4 ruff
DATA_PLANE_TEST := uv run --python $(PYTHON_VERSION) --with-editable . \
	--with pytest==8.3.4 --with pytest-asyncio==0.24.0 pytest -q
TORCH_CPU := UV_INDEX=https://download.pytorch.org/whl/cpu UV_INDEX_STRATEGY=unsafe-best-match

# The settings the control plane needs to import, matching its test configuration.
OPENAPI_ENV := FABRIC_APP_ENV=test FABRIC_DATABASE_URL=sqlite+aiosqlite:///:memory: \
	FABRIC_AUTH0_ISSUER=https://fabric.jp.auth0.com/ \
	FABRIC_AUTH0_AUDIENCE=https://api.fabric.test/control \
	FABRIC_JWT_ISSUER=https://control.fabric.test \
	FABRIC_CREDENTIAL_PEPPER=test-pepper-value FABRIC_SYSTEM_ACCOUNT_SLUG=fabric-system

.PHONY: help setup setup-python demo check check-control-plane check-data-plane check-agent \
	check-runtime check-console test-postgres openapi

help:
	@sed -n '1,13p' Makefile

setup: setup-python
	cd v1 && npm ci

setup-python:
	cd control-plane && uv sync --python $(PYTHON_VERSION) --extra dev
	cd data-plane && uv venv --python $(PYTHON_VERSION) --allow-existing .venv \
		&& uv pip install --python .venv/bin/python -e .

# The end-to-end loop with a stub model host: a real control plane over HTTP, the
# compiled Go agent and collector, and the data plane serving from the file the agent
# wrote. Point FABRIC_E2E_UPSTREAM at a running OpenAI-compatible host to use a real model.
demo: setup-python
	bash agent/scripts/e2e.sh

check: check-control-plane check-data-plane check-agent check-runtime check-console

check-control-plane:
	cd control-plane && uv run --python $(PYTHON_VERSION) --extra dev pytest -q && $(RUFF) check .

check-data-plane:
	cd data-plane && $(DATA_PLANE_TEST) && $(RUFF) check .

check-agent:
	cd agent && test -z "$$(gofmt -l .)" && go vet ./... && go test ./...

check-runtime:
	cd runtime && $(TORCH_CPU) uv run --python $(PYTHON_VERSION) --with-editable . \
		--with pytest==8.3.4 pytest -q && $(RUFF) check .
	cd serving && $(TORCH_CPU) uv run --python $(PYTHON_VERSION) --no-project \
		--with torch==2.8.0 --with triton==3.4.0 --with-editable ../runtime \
		--with pytest==8.3.4 env PYTHONPATH=. pytest -q && $(RUFF) check .

check-console:
	cd v1 && npm run lint && npx next typegen && npx tsc --noEmit
	cd v1 && for test_file in tests/*.test.mjs; do node "$$test_file" || exit 1; done
	cd v1 && out=$$(mktemp -d) \
		&& npx tsc --target es2022 --module commonjs --moduleResolution node \
			--esModuleInterop --skipLibCheck --outDir "$$out" tests/*.test.ts \
		&& for test_file in "$$out"/tests/*.test.js; do node "$$test_file" || exit 1; done

test-postgres:
	@test -n "$(DATABASE_URL)" || { echo "set DATABASE_URL to a disposable PostgreSQL database"; exit 1; }
	cd control-plane && FABRIC_TEST_DATABASE_URL="$(DATABASE_URL)" \
		uv run --python $(PYTHON_VERSION) --extra dev pytest -q -rs

openapi:
	cd control-plane && $(OPENAPI_ENV) uv run --python $(PYTHON_VERSION) --extra dev python -c \
		"import json; from app.main import create_app; \
		print(json.dumps(create_app().openapi(), indent=2, sort_keys=True))" > openapi.json
