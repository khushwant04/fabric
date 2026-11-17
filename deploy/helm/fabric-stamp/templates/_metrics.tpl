{{/*
Serving metrics for the console (docs/serving-metrics.md). On unless agent.reportMetrics is
false; compared as a string so `helm upgrade --reuse-values` from a chart without the value
keeps the default.
*/}}
{{- define "fabric-stamp.reportsMetrics" -}}
{{- if ne (toString .Values.agent.reportMetrics) "false" -}}
true
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.dcgmExporterName" -}}
{{ include "fabric-stamp.fullname" . }}-dcgm-exporter
{{- end -}}

{{- define "fabric-stamp.dcgmExporterEnabled" -}}
{{- $gpu := .Values.gpuMetrics | default dict -}}
{{- $exporter := $gpu.dcgmExporter | default dict -}}
{{- if and (include "fabric-stamp.reportsMetrics" .) $exporter.enabled (not (include "fabric-stamp.isTPU" .)) -}}
true
{{- end -}}
{{- end -}}

{{- define "fabric-stamp.dcgmExternalEnabled" -}}
{{- $gpu := .Values.gpuMetrics | default dict -}}
{{- $external := $gpu.external | default dict -}}
{{- if and (include "fabric-stamp.reportsMetrics" .) $external.enabled $external.namespace $external.service (not (include "fabric-stamp.isTPU" .)) -}}
true
{{- end -}}
{{- end -}}

{{/*
The DCGM exporter Services the agent reads, one namespace/name per line.
*/}}
{{- define "fabric-stamp.dcgmServices" -}}
{{- if include "fabric-stamp.dcgmExporterEnabled" . }}
{{ .Release.Namespace }}/{{ include "fabric-stamp.dcgmExporterName" . }}
{{- end }}
{{- if include "fabric-stamp.dcgmExternalEnabled" . }}
{{ .Values.gpuMetrics.external.namespace }}/{{ .Values.gpuMetrics.external.service }}
{{- end }}
{{- end -}}

{{/*
Whether serving metrics need the API server: to find model-host pods, or to read a DCGM
exporter's Endpoints. The gateway alone is read over localhost.
*/}}
{{- define "fabric-stamp.metricsUseKube" -}}
{{- if and (include "fabric-stamp.reportsMetrics" .) (or (include "fabric-stamp.managesModelHost" .) (include "fabric-stamp.dcgmServices" . | trim)) -}}
true
{{- end -}}
{{- end -}}
