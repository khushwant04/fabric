# Data zones and residency

Fabric can keep a deployment's requests inside a geographic boundary, and keep a client's prompts
from being processed outside one. The design and its trade-offs are in
[ADR 0017](context/adrs/0017-data-zone-residency-is-enforced-at-placement-and-at-the-gateway.md).
This page shows how to use the feature.

## Concepts

| Term | Meaning |
|---|---|
| **Region** | What a stamp reports about itself, such as `westeurope`, `eu-west-1`, or `europe-west4`. Set with the stamp chart's `stamp.region` value. |
| **Data zone** | A legal or contractual boundary made of regions: `eu`, `us`, `uk`, `ch`, `ca`, `latam`, `jp`, `kr`, `in`, `au`, `apac`, `me`, `af`. The control plane's catalogue maps regions to zones, and no region is in two zones. |
| **Residency** | A deployment's requirement: `global`, `data_zone`, or `regional`. |
| **Data boundary** | A stamp's region and zone, as the control plane reports them to the stamp. The gateway enforces residency against this. |

The full catalogue is public:

```bash
curl -fsS "$CONTROL_URL/v1/data-zones" | python -m json.tool
```

A region the catalogue does not know (an on-premises cluster called `hq-dc1`, for example) belongs
to no zone. Use the `regional` level to name it explicitly.

## Restrict a deployment

```bash
curl -fsS -X POST "$CONTROL_URL/v1/accounts/$ACCOUNT_ID/deployments" \
  -H "Authorization: Bearer $CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "name": "qwen-eu",
    "model_alias": "qwen3.5-4b-eu",
    "spec": {
      "runtime": {"release": "Qwen/Qwen3.5-4B"},
      "residency": {"level": "data_zone", "data_zone": "eu"}
    }
  }'
```

For a `regional` deployment, use `"residency": {"level": "regional", "regions": ["westeurope", "hq-dc1"]}`.
In the console, choose **Data residency** on the *Deploy a model* page.

Then place it as usual. Automatic placement only considers in-zone stamps. Naming an out-of-zone
stamp is refused with the reason:

```json
{"error": {"code": "stamp_cannot_fit_deployment", "details": {
  "reason": "data_residency_not_satisfied",
  "residency_reason": "stamp_outside_data_zone",
  "stamp_region": "eastus", "stamp_data_zone": "us"}}}
```

You cannot tighten residency so that it excludes a stamp already serving the deployment
(`residency_excludes_existing_placement`). Withdraw that placement first.

## Bind a client to a zone

Create an API key that can only mint EU tokens:

```bash
curl -fsS -X POST "$CONTROL_URL/v1/accounts/$ACCOUNT_ID/api-keys" \
  -H "Authorization: Bearer $CONTROL_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "eu-app", "scopes": ["inference:invoke"], "data_zone": "eu"}'
```

Every inference token minted from that key carries `"data_zone": "eu"`. Asking for another zone
fails with `data_zone_not_permitted`. An unbound key, or an Auth0/OIDC exchange, can narrow a
single token by adding `"data_zone": "eu"` to the `POST /v1/token` body. Only
`fabric-inference` tokens can be zone-bound.

To bind a single request without a bound token, send the routing extension:

```python
client.chat.completions.create(
    model="qwen3.5-4b-eu",
    messages=[{"role": "user", "content": "Hallo"}],
    extra_body={"routing": {"data_zone": "eu"}},
)
```

## Find an in-zone endpoint

The control plane does not proxy inference. Instead, ask it where to send requests:

```bash
curl -fsS "$CONTROL_URL/v1/accounts/$ACCOUNT_ID/inference-routes?model=qwen3.5-4b-eu&data_zone=eu" \
  -H "Authorization: Bearer $CONTROL_TOKEN"
```

```json
{"model": "qwen3.5-4b-eu", "data_zone": "eu", "region": null, "routes": [
  {"stamp_id": "…", "inference_url": "https://eu-1.inference.example", "region": "westeurope",
   "data_zone": "eu", "placement_status": "ready", "residency_satisfied": true, "healthy": true}
]}
```

Routes come back healthy first, then ready, then by stamp id. Read them at start-up or after a
refusal, not on every request. Add `include_ineligible=true` to also see placements whose stamp
has left the deployment's zone.

## What the gateway refuses

| Code | Status | When |
|---|---|---|
| `data_zone_mismatch` | 403 | The token's `data_zone` claim or the request's `routing.data_zone` differs from this stamp's zone, or this stamp has no boundary. `details.binding` says whether the token or the request set the zone. |
| `data_residency_not_satisfied` | 403 | The deployment's residency excludes this stamp, typically because the stamp reported a new region after placement. The model host is never contacted. |
| `invalid_routing` | 400 | `routing.data_zone` is not a zone name. |

`model=auto` silently skips deployments this stamp may not serve, and `/v1/models` does not list
them. Refusals are counted on `fabric_dp_data_zone_refusals_total{binding,reason}`.

## Upgrading

- Existing deployments have no `residency` key, which means `global`, and existing keys are
  unbound. Behaviour is unchanged until you opt in.
- Run migration `0005_api_key_data_zone` (`alembic upgrade head`; the Helm chart's pre-install
  hook does this).
- Upgrade the control plane before the stamps. Until a stamp's agent and data plane are upgraded,
  the stamp has no `data_boundary`. It keeps serving global deployments and unbound tokens, and
  refuses zone-restricted work, which is the fail-closed behaviour.
