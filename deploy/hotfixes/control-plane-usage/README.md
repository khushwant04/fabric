# Control-plane account usage route repair

The cluster's deployed control-plane image contains account response caching and
the `0004_account_cache_version` migration that are absent from the current checkout
and published Git history. Replacing it with the checkout's image fails the
migration hook with an unknown revision and would remove its cache behavior.

This build inherits the deployed image by immutable digest and changes only
`app/api/v1/deployments.py`. Existing handlers, durable cache invalidation,
dependencies, schema migrations, and the process configuration remain inherited.
The added static `GET /v1/accounts/{account_id}/deployments/usage` route precedes the
UUID route and sums usage with an explicit authenticated-account predicate.
Collector history for removed deployments is included. The route retains the
existing `deployments:read` authorization dependency and does not cache usage.

Provenance captured on 2026-10-06 from the running control-plane pod:

- Base image: `acrfabricinference.azurecr.io/fabric/control-plane@sha256:4b1ee65a6c3317a955c8e061d4c4f40b1575c5b328f6fffb8ef9b0edf7ee07c8`
- Original route SHA-256: `e1bf592e6f66ae71cc6e7e43e3c8cc7cd481ee47da423d4f6ec31df0b1d70032`
- Installed package path: `/opt/venv/lib/python3.12/site-packages/app`
- Live migration: `0004_account_cache_version` follows
  `0003_account_oidc_providers` and adds `accounts.cache_version` as
  `INTEGER NOT NULL DEFAULT 1`.

Build from this directory:

```bash
docker build -t fabric/control-plane:account-usage .
```

Validation used the real FastAPI router and authorization dependencies with an
isolated SQLite usage table. It verified the aggregate path returns 200, sums
only the token account, includes removed-deployment history, and refuses both
cross-account access and missing read scope with 403. An AST comparison verified
all existing deployed handlers remained unchanged. Python compilation and Ruff
passed. No production database was modified during validation.

The deployed cache source and migration history were recovered into the regular
control-plane source alongside the account usage fix. The recovery includes
`core/cache.py`, the account generation field and migration, cache configuration
and lifespan management, and the cached read/invalidation paths in the API routes.
Existing deployed handler ASTs were retained. Cache isolation, outage recovery,
durable generation updates, rollback behavior and the control-plane API suite
must pass before replacing this repair with a regular image build.

## Cluster rollout and Helm reconciliation

The repair image is
`acrfabricinference.azurecr.io/fabric/control-plane@sha256:1e26092e06813d9ebe0dfcceb148ce59694abf95465ca2de5e85d55d9f5430bc`.
Change only the control-plane Deployment's image, then wait for readiness:

```bash
kubectl --context aks-inference-cin-01 --namespace fabric-control set image \
  deployment/cp-fabric-control-plane \
  control-plane=acrfabricinference.azurecr.io/fabric/control-plane@sha256:1e26092e06813d9ebe0dfcceb148ce59694abf95465ca2de5e85d55d9f5430bc
kubectl --context aks-inference-cin-01 --namespace fabric-control rollout status \
  deployment/cp-fabric-control-plane --timeout=300s
```

The live two-replica Deployment uses a rolling update with one surge pod and zero
unavailable pods. This action preserves cache configuration, Secret references,
the Valkey Deployment and Service, and its ingress NetworkPolicy. If the repair
does not become ready, set the same image field back to the recorded base digest.
No schema migration is needed for a route-only repair.

Helm revision 9 was the last deployed release. Revision 10 failed its migration
hook; revision 11 failed rollback because its manifest disagreed about the cache
NetworkPolicy. A direct image rollout therefore leaves Helm metadata behind the
live Deployment. Avoid another upgrade with a chart missing the cache resources,
and avoid reusing failed revision 11's values.

The repository chart's `cache.yaml` and cache environment helper were restored
byte-for-byte from deployed Helm revision 9. Cache defaults were added so clean
installs remain optional. For a later Helm reconciliation:

1. Recover revision 9's release configuration into a protected local file. Values
   can include credentials; do not commit or print them.
2. Render the restored chart with those exact values, the repair image digest,
   and migrations disabled for this schema-preserving repair.
3. Compare every rendered resource with the live release and use a server dry run.
   Confirm cache resources, selectors, Secret references and public routing are
   retained before applying the reviewed Helm upgrade.
4. Use the recovered runtime cache module, ORM field and migration history now in
   the repository when returning to normal image builds, and complete their checks.
