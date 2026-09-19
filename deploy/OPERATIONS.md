# Operations

## Automated Azure deployment

`deploy/scripts/deploy-azure.sh` deploys images already built in ACR. It accepts only
`sha256:` manifest digests and enforces this sequence:

1. apply the FabricModelDeployment CRD and wait for `Established`;
2. deploy the control plane and run its migration hook;
3. deploy the shared agent/operator image while retaining the current data plane;
4. prove every stored deployment carries HTTPS issuer and JWKS fields;
5. deploy the data plane and require `/admin/verification` to report `source=sync` with
   no rejected update.

The script uses existing Helm release values and never reconstructs the spent enrollment
token. Helm `--atomic --wait` rolls a failed release back.

The manually dispatched `.github/workflows/deploy-azure.yaml` builds one image per
component with the selected commit SHA, resolves the resulting ACR manifest digests, and
calls this script. The GitHub `production` environment should require approval and define:

- OIDC secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`;
- variables: `AZURE_ACR_NAME`, `AZURE_RESOURCE_GROUP`, `AZURE_AKS_CLUSTER`;
- optional release variables: `FABRIC_CONTROL_PLANE_RELEASE`,
  `FABRIC_CONTROL_PLANE_NAMESPACE`, `FABRIC_STAMP_RELEASE`, `FABRIC_STAMP_NAMESPACE`.

The federated identity needs only ACR build rights, AKS credential access, and Kubernetes
permissions for these two releases/namespaces and the Fabric CRD. Do not use a client
secret or grant subscription Owner.

## Failure drills

`deploy/scripts/failure-drills.sh` runs one bounded drill at a time:

```bash
# Read-only posture check.
deploy/scripts/failure-drills.sh verification

# Mint a short-lived inference token first. The trap restores CP replicas on every exit.
INFERENCE_TOKEN="$TOKEN" deploy/scripts/failure-drills.sh cp-outage

# Recreate the stamp pod and verify durable state plus synchronized verification.
INFERENCE_TOKEN="$TOKEN" deploy/scripts/failure-drills.sh agent-restart

# Staging-safe only. Renews short-lived control tokens, changes max_num_seqs,
# and restores the exact old spec.
FABRIC_API_KEY="$FABRIC_API_KEY" INFERENCE_TOKEN="$TOKEN" \
ACCOUNT_ID="$ACCOUNT" DEPLOYMENT_ID="$DEPLOYMENT" ALLOW_MODEL_ROLLOUT=yes \
  deploy/scripts/failure-drills.sh model-rollout
```

Run mutation drills during a maintenance window. `model-rollout` needs one deployment's
worth of spare GPU capacity; without that explicit acknowledgement it refuses to start.
Tokens are passed in memory through environment variables and are never printed or
written by the scripts.
