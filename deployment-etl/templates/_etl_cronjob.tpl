{{/*
Helm helper that emits a CronJob with:
  - the Neo4j connection env from neo4j-credentials
  - a /bin/sh trap-EXIT wrapper that pushes status to Uptime Kuma
    (matches the fontem-stats-sync-daily pattern: default-down,
    flip to up only on rc=0; pod death + active-deadline still emit
    a down ping via the EXIT trap)
  - an optional Kuma push URL from the etl-kuma-push-urls Secret,
    keyed by the job's `name`. Missing key → push is skipped silently
    so the ETL still runs even if the monitor hasn't been provisioned
    yet.

Usage:
  {{- include "edgar-gmr-etl.cronjob" (dict
        "name"     "etl-eu-sanctions"
        "schedule" "0 7 * * *"
        "module"   "src.etl.load_eu_sanctions"
        "args"     (list)
        "deadlineSeconds" 3600
        "cpuRequest" "250m" "cpuLimit" "1"
        "memRequest" "512Mi" "memLimit" "2Gi"
        "extraEnv" (list)
        "Values"    .Values
        "Release"   .Release) -}}
*/}}
{{- define "edgar-gmr-etl.cronjob" -}}
apiVersion: batch/v1
kind: CronJob
metadata:
  name: {{ .name }}
  namespace: {{ .Release.Namespace }}
  labels:
    app: {{ .name }}
    component: etl
spec:
  # CronJobs run on schedule unless .Values.cronjobsSuspended is true.
  # Default false so dev/staging/dast keep running; prod sets it to
  # true during the empty-stores phase before the wipe-and-replay
  # cutover. (Inverted from cronjobsEnabled so the Go-template
  # `default` zero-value trap doesn't bite — `default true false`
  # returns true.)
  suspend: {{ .Values.cronjobsSuspended | default false }}
  schedule: {{ .schedule | quote }}
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      activeDeadlineSeconds: {{ .deadlineSeconds | default 3600 }}
      backoffLimit: 1
      template:
        metadata:
          labels:
            app: {{ .name }}
            component: etl
          annotations:
            linkerd.io/inject: disabled
        spec:
          restartPolicy: Never
          imagePullSecrets:
            - name: regcred
          containers:
            - name: etl
              image: contribute.void42.internal/fontem/fontem-api:{{ .Values.version }}
              imagePullPolicy: Always
              env:
                - name: NEO4J_URI
                  valueFrom:
                    secretKeyRef:
                      name: neo4j-credentials
                      key: NEO4J_URI
                - name: NEO4J_USER
                  valueFrom:
                    secretKeyRef:
                      name: neo4j-credentials
                      key: NEO4J_USER
                - name: NEO4J_PASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: neo4j-credentials
                      key: NEO4J_PASSWORD
                # Event log target (Phase B onward — see
                # fontem-ontology/MIGRATION.md). Producers emit
                # into events.entity_events instead of writing
                # downstream stores directly.
                - name: EVENTS_DATABASE_URL
                  valueFrom:
                    secretKeyRef:
                      name: fontem-postgres-credentials
                      key: EVENTS_DATABASE_URL
                # CRONJOB_NAME + IMAGE_TAG drive the events.etl_run
                # row that fontem_events.RunLog writes around each
                # invocation. Replaces the previous Uptime-Kuma push
                # trap with proper structured data for the data-
                # quality dashboard.
                - name: CRONJOB_NAME
                  value: {{ .name | quote }}
                - name: IMAGE_TAG
                  value: {{ .Values.version | quote }}
                {{- range .extraEnv }}
                - {{ toYaml . | nindent 18 | trim }}
                {{- end }}
              {{- if or .needsEdgarData .needsEsefData .needsFirdsCache }}
              # edgar-data / esef-data PVCs are owned by the main fontem-api
              # chart (RO mount; API workers write). firds-cache is owned by
              # this ETL chart (RW mount; the FIRDS cronjob writes downloaded
              # DLTINS zips and reuses them on the next run — see
              # gitops/infra/shared.yaml for the PV/PVC definition).
              volumeMounts:
                {{- if .needsEdgarData }}
                - name: edgar-data
                  mountPath: /edgar-data
                  readOnly: true
                {{- end }}
                {{- if .needsEsefData }}
                - name: esef-data
                  mountPath: /esef-data
                  readOnly: true
                {{- end }}
                {{- if .needsFirdsCache }}
                - name: firds-cache
                  mountPath: /var/cache/firds
                {{- end }}
              {{- end }}
              resources:
                requests:
                  cpu: {{ .cpuRequest | default "250m" }}
                  memory: {{ .memRequest | default "512Mi" }}
                limits:
                  cpu: {{ .cpuLimit | default "1" }}
                  memory: {{ .memLimit | default "2Gi" }}
              # `_run_wrapper` invokes the loader's `main()` inside a
              # `fontem_events.RunLog` context, so every run lands a
              # row in `events.etl_run` (status='running' on entry,
              # 'success'|'failed' on clean exit). SIGKILL / OOM /
              # activeDeadlineSeconds leave the row at 'running' so
              # the data-quality dashboard can flag the crash without
              # scraping pod logs. Replaces the previous Uptime-Kuma
              # shell trap — same purpose, structured data instead of
              # a status ping with a stringly-typed summary.
              command:
                - python
                - -m
                - src.etl._run_wrapper
                - {{ .module | quote }}
                {{- range .args }}
                - {{ . | quote }}
                {{- end }}
          {{- if or .needsEdgarData .needsEsefData .needsFirdsCache }}
          volumes:
            {{- if .needsEdgarData }}
            - name: edgar-data
              persistentVolumeClaim:
                claimName: edgar-data
            {{- end }}
            {{- if .needsEsefData }}
            - name: esef-data
              persistentVolumeClaim:
                claimName: esef-data
            {{- end }}
            {{- if .needsFirdsCache }}
            - name: firds-cache
              persistentVolumeClaim:
                claimName: firds-cache
            {{- end }}
          {{- end }}
{{- end -}}
