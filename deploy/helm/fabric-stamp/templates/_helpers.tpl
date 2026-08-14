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

{{/* Fail early on values that would otherwise produce a pod that cannot work. */}}
{{- define "fabric-stamp.validate" -}}
{{- if not .Values.controlPlane.url -}}
{{- fail "controlPlane.url is required: the agent has nowhere to enrol" -}}
{{- end -}}
{{- if not .Values.controlPlane.jwtIssuer -}}
{{- fail "controlPlane.jwtIssuer is required: the data plane cannot verify tokens without it" -}}
{{- end -}}
{{- if not .Values.modelHost.url -}}
{{- fail "modelHost.url is required: the data plane has nothing to proxy to" -}}
{{- end -}}
{{- if and (not .Values.enrollment.token) (not .Values.enrollment.existingSecret) -}}
{{- fail "enrollment.token or enrollment.existingSecret is required for the first install" -}}
{{- end -}}
{{- end -}}
