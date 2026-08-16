{{- define "fabric-control-plane.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fabric-control-plane.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "fabric-control-plane.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "fabric-control-plane.labels" -}}
app.kubernetes.io/name: {{ include "fabric-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: fabric
{{- with .Values.extraLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "fabric-control-plane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "fabric-control-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fabric-control-plane.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "fabric-control-plane.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "fabric-control-plane.secretName" -}}
{{- printf "%s-config" (include "fabric-control-plane.fullname" .) -}}
{{- end -}}

{{/* Secret and key that hold each sensitive value, so callers can bring their own. */}}
{{- define "fabric-control-plane.databaseSecret" -}}
{{- default (include "fabric-control-plane.secretName" .) .Values.database.existingSecret -}}
{{- end -}}

{{- define "fabric-control-plane.signingSecret" -}}
{{- default (include "fabric-control-plane.secretName" .) .Values.signingKey.existingSecret -}}
{{- end -}}

{{- define "fabric-control-plane.pepperSecret" -}}
{{- default (include "fabric-control-plane.secretName" .) .Values.credentialPepperExistingSecret -}}
{{- end -}}

{{/* Environment shared by the server and the migration job, so they cannot drift. */}}
{{- define "fabric-control-plane.env" -}}
- name: FABRIC_APP_ENV
  value: {{ .Values.appEnv | quote }}
- name: FABRIC_LOG_LEVEL
  value: {{ .Values.logLevel | quote }}
- name: FABRIC_JWT_ISSUER
  value: {{ .Values.jwt.issuer | quote }}
- name: FABRIC_AUTH0_ISSUER
  value: {{ .Values.auth0.issuer | quote }}
- name: FABRIC_AUTH0_AUDIENCE
  value: {{ .Values.auth0.audience | quote }}
- name: FABRIC_SYSTEM_ACCOUNT_SLUG
  value: {{ .Values.systemAccountSlug | quote }}
- name: FABRIC_JWT_PRIVATE_KEY_PATH
  value: /etc/fabric/signing/{{ .Values.signingKey.existingSecretKey }}
- name: FABRIC_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "fabric-control-plane.databaseSecret" . }}
      key: {{ .Values.database.existingSecretKey }}
- name: FABRIC_CREDENTIAL_PEPPER
  valueFrom:
    secretKeyRef:
      name: {{ include "fabric-control-plane.pepperSecret" . }}
      key: {{ .Values.credentialPepperExistingSecretKey }}
{{- end -}}

{{- define "fabric-control-plane.validate" -}}
{{- if not (or .Values.database.url .Values.database.existingSecret) -}}
{{- fail "database.url or database.existingSecret is required" -}}
{{- end -}}
{{- if not (or .Values.signingKey.value .Values.signingKey.existingSecret) -}}
{{- fail "signingKey.value or signingKey.existingSecret is required: tokens cannot be signed without a key" -}}
{{- end -}}
{{- if not (or .Values.credentialPepper .Values.credentialPepperExistingSecret) -}}
{{- fail "credentialPepper or credentialPepperExistingSecret is required: the control plane refuses to start on the default pepper outside local and test" -}}
{{- end -}}
{{- if not .Values.jwt.issuer -}}
{{- fail "jwt.issuer is required: it is the issuer stamps verify and the address they fetch JWKS from" -}}
{{- end -}}
{{- if not .Values.auth0.audience -}}
{{- fail "auth0.audience is required to validate human logins" -}}
{{- end -}}
{{- end -}}
