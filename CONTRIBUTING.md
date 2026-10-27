# Contributing to Fabric

Fabric is several independent components in one repository. Each pins its own
dependencies and has its own checks; CI runs all of them on every pull request.

## Before opening a pull request

Run the checks for every component you touched. `make check` runs them all.

| Component | Checks |
|---|---|
| `control-plane/` | `uv run --extra dev pytest -q` and `uv run --with ruff==0.8.4 ruff check .` |
| `control-plane/` on PostgreSQL | `FABRIC_TEST_DATABASE_URL=postgresql+asyncpg://fabric_app:…@localhost/fabric_test uv run --extra dev pytest -q`, with the role from `scripts/create-app-role.sql` |
| `data-plane/` | `uv run --with-editable . --with pytest==8.3.4 --with pytest-asyncio==0.24.0 pytest -q`, plus ruff |
| `agent/` | `gofmt -l .` (must print nothing), `go vet ./...`, `go test ./...` |
| `runtime/`, `serving/` | see the `runtime` job in [`ci.yaml`](.github/workflows/ci.yaml); CPU wheels are enough |
| `v1/` | `npm run lint`, `npx next typegen && npx tsc --noEmit`, and the files in `tests/` |
| `deploy/helm/` | `helm lint` and the `deploy/scripts/test-*` chart checks |

Row-level security is a no-op on SQLite. A change to tenancy, policies or migrations is
only verified once the PostgreSQL run passes.

If you change the control-plane API, regenerate `control-plane/openapi.json` as described
in [the control-plane guide](docs/context/control-plane-service.md). A test fails while
the committed document differs from what the app serves. Don't edit it by hand.

## Conventions

- **Never skip CI.** Commits marked `[skip ci]` let five broken commits reach the branch
  unnoticed once. A failing check is fixed, not bypassed. Don't skip, disable or loosen
  a test to get green.
- **Code is the source of truth.** Documentation describes what is implemented, and
  planned behaviour is labelled as planned. If your change makes a statement in `docs/`
  untrue, fix the statement in the same pull request.
- **Durable decisions get an ADR** in [`docs/context/adrs/`](docs/context/adrs/README.md).
- **Benchmark claims cite an artifact** produced by the runtime harness on named
  hardware. A number without one stays out of the docs.
- **Secrets never enter values files, logs or command history.** Use Kubernetes Secrets
  and the existing `existingSecret` chart options.
- Match the surrounding code: its comment density, naming and error handling.

## Commit messages

Use a short imperative summary line (`Add …`, `Fix …`, `Make …`), then a body that says
*why*. Conventional prefixes (`feat:`, `fix:`, `docs:`) are fine but not required.

## Reporting security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md).
