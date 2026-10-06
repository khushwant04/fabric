# Native Kubernetes inference endpoint

Install one stamp for a cluster or a disjoint GPU pool. Enrollment links that stamp to
the central account; registering a model does not enroll a cluster. The agent publishes
placed models as custom resources, and the operator creates model-host Deployments and
Services. Ready endpoints are published into the data-plane router.

An existing Kubernetes Ingress controller and cert-manager can expose the stamp without
Istio. Configure DNS to the ingress controller's public address and choose an existing
Issuer or ClusterIssuer:

```yaml
controlPlane:
  url: https://control.example.com
enrollment:
  existingSecret: fabric-enrollment
stamp:
  name: gpu-west
exposure:
  enabled: true
  baseDomain: inference.example.com
  className: nginx
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
    nginx.ingress.kubernetes.io/proxy-buffering: "off"
  tls:
    issuer:
      name: letsencrypt
      kind: ClusterIssuer
```

This produces `https://gpu-west.inference.example.com`. Set `exposure.host` for an exact
hostname, or `exposure.tls.secretName` to use an existing certificate Secret. Configure
HTTPS redirection and streaming timeouts using the annotations your ingress controller
supports. The native Ingress exposes `/v1` only; administrative listeners are private.
Native exposure and Istio exposure are mutually exclusive. Existing Istio installations
retain their Gateway and VirtualService configuration.

The agent reports the HTTPS base URL at enrollment and heartbeat and attaches it to
deployment status only after the operator observes an available model host. A declared
endpoint alone does not prove DNS or certificate issuance succeeded. `controlPlane.jwtIssuer`
defaults to the control-plane URL; the server's desired-state verification document remains
authoritative for issuer and signing keys.

Managed model hosts use a dedicated ServiceAccount without an API token. Private registry
and gated model credentials remain references resolved by Kubernetes:

```yaml
operator:
  managedModelHost:
    imagePullSecrets:
      - name: private-registry
    huggingFace:
      existingSecret: hugging-face
      tokenKey: token
```

Create those Secrets in the stamp namespace. The operator does not need read permissions
for their contents. NVIDIA GPU nodes need the host driver, container runtime support,
the device plugin, and GPU Feature Discovery labels for measured product/memory/compute
metadata. Cloud SKU inference is reported with its source, and unknown hardware remains
unknown. Nodes without `Ready=True`, or cordoned nodes, do not contribute placement capacity.
A single GPU k3s node can serve an initial model; a rollout preparing another model beside
it needs spare GPU capacity. The chart does not provision GPU nodes.
