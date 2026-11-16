{{- define "fabric-stamp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fabric-stamp.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "fabric-stamp.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.labels" -}}
app.kubernetes.io/name: {{ include "fabric-stamp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: fabric
{{- with .Values.extraLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "fabric-stamp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "fabric-stamp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fabric-stamp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "fabric-stamp.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.stampName" -}}
{{- default .Release.Name .Values.stamp.name -}}
{{- end -}}

{{- define "fabric-stamp.jwtIssuer" -}}
{{- default (trimSuffix "/" .Values.controlPlane.url) .Values.controlPlane.jwtIssuer -}}
{{- end -}}

{{- define "fabric-stamp.exposureHost" -}}
{{- if .Values.exposure.host -}}
{{- .Values.exposure.host -}}
{{- else if .Values.exposure.baseDomain -}}
{{- printf "%s.%s" (include "fabric-stamp.stampName" .) .Values.exposure.baseDomain -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.inferenceUrl" -}}
{{- if .Values.exposure.enabled -}}
{{- printf "https://%s" (include "fabric-stamp.exposureHost" .) -}}
{{- else if .Values.stamp.inferenceUrl -}}
{{- .Values.stamp.inferenceUrl -}}
{{- else if and .Values.istio.enabled (eq (len .Values.istio.hosts) 1) -}}
{{- printf "https://%s" (first .Values.istio.hosts) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.exposureSecret" -}}
{{- default (printf "%s-inference-tls" (include "fabric-stamp.fullname" .)) .Values.exposure.tls.secretName -}}
{{- end -}}

{{- define "fabric-stamp.modelHostServiceAccount" -}}
{{- default (printf "%s-model-host" (include "fabric-stamp.fullname" .)) .Values.operator.managedModelHost.serviceAccount.name -}}
{{- end -}}

{{/* The JWKS URL follows the control-plane URL unless overridden. */}}
{{- define "fabric-stamp.jwksUrl" -}}
{{- if .Values.controlPlane.jwksUrl -}}
{{- .Values.controlPlane.jwksUrl -}}
{{- else -}}
{{- printf "%s/.well-known/jwks.json" (trimSuffix "/" .Values.controlPlane.url) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.enrollmentSecretName" -}}
{{- if .Values.enrollment.existingSecret -}}
{{- .Values.enrollment.existingSecret -}}
{{- else -}}
{{- printf "%s-enrollment" (include "fabric-stamp.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.fileSearchClaimName" -}}
{{- if .Values.dataPlane.fileSearch.persistence.existingClaim -}}
{{- .Values.dataPlane.fileSearch.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-file-search" (include "fabric-stamp.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.usageSpoolClaimName" -}}
{{- if .Values.usageSpool.existingClaim -}}
{{- .Values.usageSpool.existingClaim -}}
{{- else -}}
{{- printf "%s-usage-spool" (include "fabric-stamp.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.limitSecretName" -}}
{{- if .Values.dataPlane.limits.shared.existingSecret -}}
{{- .Values.dataPlane.limits.shared.existingSecret -}}
{{- else -}}
{{- printf "%s-limit-coordinator" (include "fabric-stamp.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Render an exact manifest digest when supplied; tags remain for local development. */}}
{{- define "fabric-stamp.agentImage" -}}
{{- if .Values.image.agent.digest -}}
{{- printf "%s@%s" .Values.image.agent.repository .Values.image.agent.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.agent.repository (.Values.image.agent.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.dataPlaneImage" -}}
{{- if .Values.image.dataPlane.digest -}}
{{- printf "%s@%s" .Values.image.dataPlane.repository .Values.image.dataPlane.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.dataPlane.repository (.Values.image.dataPlane.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{/* Fail early on values that would otherwise produce a pod that cannot work. */}}
{{- define "fabric-stamp.validate" -}}
{{- include "fabric-stamp.validateHostedTools" . -}}
{{- if and (include "fabric-stamp.managesModelHost" .) (not .Values.operator.managedModelHost.serviceAccount.create) (not .Values.operator.managedModelHost.serviceAccount.name) -}}
{{- fail "managed model hosts need a dedicated serviceAccount.name when serviceAccount.create is false" -}}
{{- end -}}
{{- if not .Values.controlPlane.url -}}
{{- fail "controlPlane.url is required: the agent has nowhere to enrol" -}}
{{- end -}}
{{- if and .Values.exposure.enabled .Values.istio.enabled -}}
{{- fail "exposure.enabled and istio.enabled are mutually exclusive" -}}
{{- end -}}
{{- if .Values.exposure.enabled -}}
{{- $host := include "fabric-stamp.exposureHost" . -}}
{{- if not (regexMatch "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$" $host) -}}
{{- fail "exposure.host or stamp.name plus exposure.baseDomain must produce a DNS hostname" -}}
{{- end -}}
{{- if and (not .Values.exposure.tls.secretName) (not .Values.exposure.tls.issuer.name) -}}
{{- fail "exposure requires an existing TLS secretName or cert-manager issuer.name" -}}
{{- end -}}
{{- if not (has .Values.exposure.tls.issuer.kind (list "Issuer" "ClusterIssuer")) -}}
{{- fail "exposure.tls.issuer.kind must be Issuer or ClusterIssuer" -}}
{{- end -}}
{{- end -}}
{{- if and (not .Values.modelHost.url) (not (include "fabric-stamp.managesModelHost" .)) -}}
{{- fail "modelHost.url is required: the data plane has nothing to proxy to, and no operator is managing a host" -}}
{{- end -}}
{{- if and (not .Values.enrollment.token) (not .Values.enrollment.existingSecret) -}}
{{- fail "enrollment.token or enrollment.existingSecret is required for the first install" -}}
{{- end -}}
{{- if and .Values.dataPlane.limits.shared.enabled .Values.dataPlane.limits.shared.existingSecret .Values.dataPlane.limits.shared.token -}}
{{- fail "dataPlane.limits.shared.existingSecret and token are mutually exclusive" -}}
{{- end -}}
{{- if and .Values.dataPlane.limits.shared.enabled (not .Values.dataPlane.limits.shared.existingSecretKey) -}}
{{- fail "dataPlane.limits.shared.existingSecretKey is required" -}}
{{- end -}}
{{- if and .Values.dataPlane.limits.shared.enabled (ge (float64 .Values.dataPlane.limits.shared.renewSeconds) (divf (float64 .Values.dataPlane.limits.shared.leaseSeconds) 2.0)) -}}
{{- fail "dataPlane.limits.shared.renewSeconds must be less than half leaseSeconds" -}}
{{- end -}}
{{- if and .Values.stamp.measureCapacity (not .Values.serviceAccount.create) (not .Values.serviceAccount.name) -}}
{{- fail "stamp.measureCapacity needs a dedicated ServiceAccount: without serviceAccount.create or serviceAccount.name the cluster-wide node and pod read would be bound to the namespace's default account, which every other pod there can mount. Set serviceAccount.name, or stamp.measureCapacity=false to report stamp.gpus instead." -}}
{{- end -}}
{{- end -}}

{{/*
Whether the operator creates and owns the model host itself. When it does, the upstream
in the rendered configuration is a placeholder: the operator's Service is named after a
deployment ID that does not exist at install time, and the operator overrides the
upstream once a deployment is actually placed here.
*/}}
{{- define "fabric-stamp.managesModelHost" -}}
{{- if and .Values.operator.enabled .Values.operator.managedModelHost.image -}}
true
{{- end -}}
{{- end -}}

{{/*
Whether the agent pushes model-host pod status, events and log tails to the control plane
(docs/model-host-logs.md). Only meaningful when the operator runs the hosts. Compared as a
string so `helm upgrade --reuse-values` from a chart without the value keeps it on, while
an explicit false still turns it off.
*/}}
{{- define "fabric-stamp.reportsHosts" -}}
{{- if and (include "fabric-stamp.managesModelHost" .) (ne (toString .Values.agent.reportHosts) "false") -}}
true
{{- end -}}
{{- end -}}

{{/*
The upstream the data plane starts with. A host that cannot be reached fails closed with
a gateway error, which is the right behaviour before a deployment is placed: answering
from the wrong upstream would be worse than not answering.
*/}}
{{- define "fabric-stamp.modelHostUrl" -}}
{{- if .Values.modelHost.url -}}
{{- .Values.modelHost.url -}}
{{- else -}}
http://model-host-not-yet-placed.invalid
{{- end -}}
{{- end -}}

{{/* Hosted tools (docs/hosted-tools.md): the self-hosted code-interpreter sandbox. */}}
{{- define "fabric-stamp.sandboxEnabled" -}}
{{- if and .Values.hostedTools.codeInterpreter.enabled (eq .Values.hostedTools.codeInterpreter.backend "sandbox") -}}
true
{{- end -}}
{{- end -}}

{{/*
Sandbox pods carry their own app.kubernetes.io/name so that no stamp selector (the
StatefulSet, the inference Service, the stamp NetworkPolicy) ever matches them.
*/}}
{{- define "fabric-stamp.sandboxSelectorLabels" -}}
app.kubernetes.io/name: {{ include "fabric-stamp.name" . }}-sandbox
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: sandbox
{{- end -}}

{{- define "fabric-stamp.sandboxName" -}}
{{- printf "%s-sandbox" (include "fabric-stamp.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fabric-stamp.sandboxSecretName" -}}
{{- default (include "fabric-stamp.sandboxName" .) .Values.sandbox.token.existingSecret -}}
{{- end -}}

{{- define "fabric-stamp.sandboxImage" -}}
{{- if .Values.sandbox.image.digest -}}
{{- printf "%s@%s" .Values.sandbox.image.repository .Values.sandbox.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.sandbox.image.repository (.Values.sandbox.image.tag | default .Chart.AppVersion) -}}
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.validateHostedTools" -}}
{{- $h := .Values.hostedTools -}}
{{- if not (has $h.codeInterpreter.backend (list "sandbox" "e2b")) -}}
{{- fail "hostedTools.codeInterpreter.backend must be sandbox or e2b" -}}
{{- end -}}
{{- if and $h.codeInterpreter.enabled (eq $h.codeInterpreter.backend "e2b") (not $h.codeInterpreter.e2b.existingSecret) -}}
{{- fail "hostedTools.codeInterpreter.backend=e2b needs hostedTools.codeInterpreter.e2b.existingSecret holding the API key (never a plain value)" -}}
{{- end -}}
{{- if not (has $h.web.backend (list "direct" "searxng" "firecrawl")) -}}
{{- fail "hostedTools.web.backend must be direct, searxng or firecrawl" -}}
{{- end -}}
{{- if and $h.webFetch.enabled (not $h.webFetch.allowedHosts) -}}
{{- fail "hostedTools.webFetch.enabled needs webFetch.allowedHosts (a comma-separated list, or * for any public host)" -}}
{{- end -}}
{{- if and (or $h.webFetch.enabled $h.webSearch.enabled) (eq $h.web.backend "firecrawl") (not $h.web.firecrawl.url) -}}
{{- fail "hostedTools.web.backend=firecrawl needs hostedTools.web.firecrawl.url (Fabric does not deploy Firecrawl)" -}}
{{- end -}}
{{- if and $h.webSearch.enabled (ne $h.web.backend "firecrawl") (not $h.web.searchUrl) -}}
{{- fail "hostedTools.webSearch.enabled needs hostedTools.web.searchUrl (SearXNG base URL or an API template)" -}}
{{- end -}}
{{- end -}}
