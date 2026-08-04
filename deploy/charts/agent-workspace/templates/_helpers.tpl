{{- define "agent-workspace.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent-workspace.labels" -}}
app.kubernetes.io/name: {{ include "agent-workspace.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/component: agent-workspace
app.kubernetes.io/part-of: private-ai-platform-kit
app.kubernetes.io/managed-by: {{ .Release.Service }}
platform.ai/cost-center: {{ .Values.sandbox.costCenter | quote }}
platform.ai/environment: {{ .Values.sandbox.environment | quote }}
platform.ai/owner: {{ .Values.sandbox.owner | quote }}
platform.ai/sandbox-id: {{ .Values.sandbox.id | quote }}
platform.ai/tenant: {{ .Values.sandbox.tenant | quote }}
platform.ai/compliance-profile: {{ .Values.sandbox.complianceProfile | quote }}
platform.ai/data-classification: {{ .Values.sandbox.dataClassification | quote }}
{{- end -}}

{{- define "agent-workspace.serviceAccountName" -}}
{{- default "agent-runner" .Values.serviceAccount.name -}}
{{- end -}}

{{- /* The earliest expiresOn across the approved egress entries. One policy object holds
       several exceptions, and the policy stops being fully reviewed the moment the first
       of them lapses, so the soonest date is the one the cluster acts on. ISO dates sort
       lexically, which is why a plain string comparison is correct here. */ -}}
{{- define "agent-workspace.earliestEgressExpiry" -}}
{{- $earliest := "" -}}
{{- range .Values.networkPolicy.allowedEgressCidrs -}}
{{- $expiry := required (printf "agent-workspace: allowedEgressCidrs entry %s must set expiresOn (YYYY-MM-DD)" .cidr) .expiresOn | toString -}}
{{- if or (eq $earliest "") (lt $expiry $earliest) -}}
{{- $earliest = $expiry -}}
{{- end -}}
{{- end -}}
{{- $earliest -}}
{{- end -}}

{{- /* The catalog entries this policy's exceptions cite, so an operator reading the
       NetworkPolicy in the cluster can find the review that approved it. */ -}}
{{- define "agent-workspace.egressCatalogRefs" -}}
{{- $refs := list -}}
{{- range .Values.networkPolicy.allowedEgressCidrs -}}
{{- $refs = append $refs (.catalogRef | toString) -}}
{{- end -}}
{{- join "," $refs -}}
{{- end -}}
