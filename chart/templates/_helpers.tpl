{{/* Namespace guard: the release namespace is the namespace of every object. */}}
{{- define "calc.namespace" -}}
{{- if ne .Release.Namespace .Values.namespace.name -}}
{{- fail (printf "release namespace %q must be %q (use --namespace %s)" .Release.Namespace .Values.namespace.name .Values.namespace.name) -}}
{{- end -}}
{{- .Release.Namespace -}}
{{- end -}}

{{/* Labels. Call with a dict: root, name, component. */}}
{{- define "calc.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
app.kubernetes.io/part-of: calculator
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "calc.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{/* Image by digest only. */}}
{{- define "calc.image" -}}
{{- $repository := required "image.repository is required (set by the deployment)" .Values.image.repository -}}
{{- $digest := required "image.digest is required (set by the deployment)" .Values.image.digest -}}
{{- if not (regexMatch "^sha256:[0-9a-f]{64}$" $digest) -}}
{{- fail (printf "image.digest must look like sha256:<64 hex>, got %q" $digest) -}}
{{- end -}}
{{- if regexMatch "@|:[^/]*$" $repository -}}
{{- fail "image.repository must not contain a tag or a digest" -}}
{{- end -}}
{{- printf "%s@%s" $repository $digest -}}
{{- end -}}

{{/* Environment shared by every pod: plain values, none is a secret. */}}
{{- define "calc.envCommon" -}}
- name: AWS_REGION
  value: {{ .Values.region | quote }}
- name: ALERTS_TOPIC_ARN
  value: {{ required "app.alertsTopicArn is required" .Values.app.alertsTopicArn | quote }}
- name: APP_SECRET_ARN
  value: {{ required "app.appSecretArn is required" .Values.app.appSecretArn | quote }}
- name: DB_HOST
  value: {{ required "app.dbHost is required" .Values.app.dbHost | quote }}
- name: DB_PORT
  value: {{ .Values.app.dbPort | toString | quote }}
- name: DB_NAME
  value: {{ .Values.app.dbName | quote }}
- name: LOG_LEVEL
  value: {{ .Values.app.logLevel | quote }}
- name: SSL_ROOT_CERT
  value: {{ .Values.app.sslRootCert | quote }}
{{- end -}}

{{/* HTTP pods and the migration job also get the CORS origin. */}}
{{- define "calc.env" -}}
{{ include "calc.envCommon" . }}
- name: ALLOWED_ORIGIN
  value: {{ required "app.allowedOrigin is required" .Values.app.allowedOrigin | quote }}
{{- end -}}

{{/* The ingestion worker: common settings plus the queue, the bucket and the environment name. */}}
{{- define "calc.workerEnv" -}}
{{ include "calc.envCommon" . }}
- name: ENVIRONMENT
  value: {{ required "envType is required (dev or prod)" .Values.envType | quote }}
- name: INGEST_QUEUE_URL
  value: {{ required "ingest.queueUrl is required for the ingest worker" .Values.ingest.queueUrl | quote }}
- name: INGEST_BUCKET
  value: {{ required "ingest.bucketName is required for the ingest worker" .Values.ingest.bucketName | quote }}
{{- end -}}

{{- define "calc.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.securityContext.runAsUser }}
runAsGroup: {{ .Values.securityContext.runAsGroup }}
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "calc.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
privileged: false
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Resources of a profile (requests and limits are mandatory). Call with a dict: root, profile. */}}
{{- define "calc.resources" -}}
{{- $p := index .root.Values.resourceProfiles .profile | default dict -}}
{{- if not (and $p.requests $p.limits) -}}
{{- fail (printf "resource profile %q needs requests and limits" .profile) -}}
{{- end -}}
requests:
  cpu: {{ $p.requests.cpu | quote }}
  memory: {{ $p.requests.memory | quote }}
limits:
  cpu: {{ $p.limits.cpu | quote }}
  memory: {{ $p.limits.memory | quote }}
{{- end -}}

{{/*
Annotations of the load balancer. The listener-level ones are "Exclusive" in an IngressGroup (same value on
every Ingress of the group), so both Ingress objects get the same text from here.
*/}}
{{- define "calc.albAnnotations" -}}
{{- $v := .Values -}}
{{- /* Access logs are enabled only when a bucket is given (the log bucket exists once the storage stack is deployed) */ -}}
{{- $logs := $v.ingress.accessLogs.bucket -}}
{{- if and $v.ingress.wafRequired (not $v.ingress.wafAclArn) -}}
{{- fail "ingress.wafAclArn is required in this environment" -}}
{{- end -}}
alb.ingress.kubernetes.io/group.name: {{ $v.ingress.groupName | quote }}
alb.ingress.kubernetes.io/scheme: internet-facing
alb.ingress.kubernetes.io/target-type: ip
alb.ingress.kubernetes.io/backend-protocol: HTTPS
alb.ingress.kubernetes.io/healthcheck-protocol: HTTPS
alb.ingress.kubernetes.io/healthcheck-path: {{ $v.probes.path | quote }}
alb.ingress.kubernetes.io/listen-ports: '[{"HTTP":80},{"HTTPS":443}]'
alb.ingress.kubernetes.io/ssl-redirect: "443"
alb.ingress.kubernetes.io/ssl-policy: {{ $v.ingress.sslPolicy | quote }}
alb.ingress.kubernetes.io/certificate-arn: {{ required "ingress.certificateArn is required" $v.ingress.certificateArn | quote }}
alb.ingress.kubernetes.io/security-groups: {{ required "ingress.securityGroupId is required" $v.ingress.securityGroupId | quote }}
alb.ingress.kubernetes.io/manage-backend-security-group-rules: "false"
{{- if $logs }}
alb.ingress.kubernetes.io/load-balancer-attributes: {{ printf "access_logs.s3.enabled=true,access_logs.s3.bucket=%s,access_logs.s3.prefix=%s,routing.http.drop_invalid_header_fields.enabled=true" $logs $v.ingress.accessLogs.prefix | quote }}
{{- else }}
alb.ingress.kubernetes.io/load-balancer-attributes: "routing.http.drop_invalid_header_fields.enabled=true"
{{- end }}
alb.ingress.kubernetes.io/tags: {{ include "calc.tagsAnnotation" . | quote }}
{{- if $v.ingress.wafAclArn }}
alb.ingress.kubernetes.io/wafv2-acl-arn: {{ $v.ingress.wafAclArn | quote }}
{{- end }}
{{- end -}}

{{- define "calc.tagsAnnotation" -}}
{{- $t := .Values.tags -}}
{{- $env := required "envType is required (dev or prod)" .Values.envType -}}
{{- printf "team-owner=%s,project-name=%s,app-name=%s,repo-name=%s,env-type=%s" (index $t "team-owner") (index $t "project-name") (index $t "app-name") (index $t "repo-name") $env -}}
{{- end -}}
