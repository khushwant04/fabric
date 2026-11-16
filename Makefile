# Entry points for working on the whole repository. Each component still pins and
# installs its own dependencies; these targets run the same commands CI does.
#
#   make setup      install every component's environment
#   make demo       run the full platform loop locally, no GPU or cluster needed
#   make demo-agent the same platform, then a multi-step agent run with a budget, a trace
#                   and the tool-call guard (docs/agent-runs.md); a stub host unless
#                   FABRIC_E2E_UPSTREAM points at a real one
#   make demo-agent-loop  one request runs a whole agent on the gateway (POST /v1/agent/runs)
#   make demo-cascade     model "cascade": small tier first, escalate on cheap checks
#   make demo-openai-agents  the OpenAI Agents SDK on Fabric: handoff, tools, run budget,
#                   tool guard and trace (docs/openai-agents-sdk.md)
#   make demo-edge  the edge gateway (ADR 0021) in front of two local stamps: routing by
#                   model, failover, and serving through a control-plane outage
#   make check      every test and lint suite CI runs (except PostgreSQL)
#   make openapi    regenerate control-plane/openapi.json after an API change
#   make bench-routing  compare routing strategies through the real gateway (see
#                       data-plane/benchmarks/routing/README.md); BENCH_ARGS passes options
#
# PostgreSQL suite, against a database whose contents can be discarded:
#   make test-postgres DATABASE_URL=postgresql+asyncpg://fabric_app:...@localhost/fabric_test

PYTHON_VERSION ?= 3.12
RUFF := uv run --no-project --with ruff==0.8.4 ruff
DATA_PLANE_TEST := uv run --python $(PYTHON_VERSION) --with-editable . \
	--with pytest==8.3.4 --with pytest-asyncio==0.24.0 \
	--with openai-agents==0.24.0 --with openai==3.16.2 pytest -q
TORCH_CPU := UV_INDEX=https://download.pytorch.org/whl/cpu UV_INDEX_STRATEGY=unsafe-best-match

# The settings the control plane needs to import, matching its test configuration.
OPENAPI_ENV := FABRIC_APP_ENV=test FABRIC_DATABASE_URL=sqlite+aiosqlite:///:memory: \
	FABRIC_AUTH0_ISSUER=https://fabric.jp.auth0.com/ \
	FABRIC_AUTH0_AUDIENCE=https://api.fabric.test/control \
	FABRIC_JWT_ISSUER=https://control.fabric.test \
	FABRIC_CREDENTIAL_PEPPER=test-pepper-value FABRIC_SYSTEM_ACCOUNT_SLUG=fabric-system

.PHONY: help setup setup-python demo demo-agent demo-agent-loop demo-cascade demo-openai-agents demo-edge check check-control-plane check-data-plane check-agent \
	check-runtime check-console test-postgres openapi bench-routing

help:
	@sed -n '1,22p' Makefile

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

demo-agent: setup-python
	uv sync --quiet --project examples/python --python $(PYTHON_VERSION)
	FABRIC_E2E_AGENT_DEMO=1 PYTHON_VERSION=$(PYTHON_VERSION) bash agent/scripts/e2e.sh

# Control plane, two stamps (Go agents + HTTPS gateways with stub model hosts, different
# names and regions) and the edge; see docs/edge-gateway.md.
demo-edge: setup-python
	bash agent/scripts/edge-demo.sh

# Server-side tool loop (docs/agent-tool-loop.md) and model cascade (docs/model-cascade.md),
# on the same local platform and stub host machinery as demo-agent.
demo-agent-loop: setup-python
	uv sync --quiet --project examples/python --python $(PYTHON_VERSION)
	FABRIC_E2E_AGENT_DEMO=loop PYTHON_VERSION=$(PYTHON_VERSION) bash agent/scripts/e2e.sh

demo-cascade: setup-python
	uv sync --quiet --project examples/python --python $(PYTHON_VERSION)
	FABRIC_E2E_AGENT_DEMO=cascade PYTHON_VERSION=$(PYTHON_VERSION) bash agent/scripts/e2e.sh

# The OpenAI Agents SDK (examples/python/scripts/openai_agents_demo.py) against the same local
# platform, with the stub host agent/scripts/openai_agents_demo_host.py unless
# FABRIC_E2E_UPSTREAM points at a real one.
demo-openai-agents: setup-python
	uv sync --quiet --project examples/python --python $(PYTHON_VERSION)
	FABRIC_E2E_AGENT_DEMO=openai-agents PYTHON_VERSION=$(PYTHON_VERSION) bash agent/scripts/e2e.sh

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
		&& npx tsc -p tests/tsconfig.json --outDir "$$out" \
		&& for test_file in "$$out"/tests/*.test.js; do node "$$test_file" || exit 1; done

test-postgres:
	@test -n "$(DATABASE_URL)" || { echo "set DATABASE_URL to a disposable PostgreSQL database"; exit 1; }
	cd control-plane && FABRIC_TEST_DATABASE_URL="$(DATABASE_URL)" \
		uv run --python $(PYTHON_VERSION) --extra dev pytest -q -rs

openapi:
	cd control-plane && $(OPENAPI_ENV) uv run --python $(PYTHON_VERSION) --extra dev python -c \
		"import json; from app.main import create_app; \
		print(json.dumps(create_app().openapi(), indent=2, sort_keys=True))" > openapi.json

BENCH_ARGS ?=
bench-routing:
	cd data-plane && uv run --python $(PYTHON_VERSION) --with-editable . \
		--with matplotlib==3.9.2 python -m benchmarks.routing $(BENCH_ARGS)
