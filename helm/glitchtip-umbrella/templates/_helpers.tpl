{{/*
Umbrella chart helpers
*/}}
{{- define "glitchtip-umbrella.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "glitchtip-umbrella.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s" .Release.Name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
