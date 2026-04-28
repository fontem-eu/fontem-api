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
              image: contribute.void42.internal/golden/gmr-api:{{ .Values.version }}
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
                - name: KUMA_PUSH_URL
                  valueFrom:
                    secretKeyRef:
                      name: etl-kuma-push-urls
                      key: {{ .name }}
                      optional: true
                {{- range .extraEnv }}
                - {{ toYaml . | nindent 18 | trim }}
                {{- end }}
              resources:
                requests:
                  cpu: {{ .cpuRequest | default "250m" }}
                  memory: {{ .memRequest | default "512Mi" }}
                limits:
                  cpu: {{ .cpuLimit | default "1" }}
                  memory: {{ .memLimit | default "2Gi" }}
              command: ["/bin/sh", "-c"]
              args:
                - |
                  # Default-down: if the pod is killed (OOM, deadline,
                  # node eviction) the trap emits a down ping before
                  # exit. Status only flips to "up" on rc=0.
                  #
                  # We push via python urllib rather than curl — the
                  # gmr-api Docker image is python:3.12-slim with no
                  # curl. Python is guaranteed present (it's how we run
                  # the loader anyway).
                  KUMA_STATUS=down
                  KUMA_SUMMARY=running
                  push_kuma() {
                    [ -z "$KUMA_PUSH_URL" ] && return 0
                    python3 -c "
                  import os, sys, urllib.parse, urllib.request
                  url = os.environ['KUMA_PUSH_URL']
                  qs = urllib.parse.urlencode({
                      'status': os.environ.get('KUMA_STATUS', 'down'),
                      'msg':    os.environ.get('KUMA_SUMMARY', 'unknown'),
                      'ping':   '',
                  })
                  full = url + ('&' if '?' in url else '?') + qs
                  try:
                      urllib.request.urlopen(full, timeout=10).read()
                  except Exception as e:  # noqa: BLE001
                      sys.stderr.write(f'kuma push failed: {e}\n')
                  " || true
                  }
                  trap push_kuma EXIT
                  set +e
                  out=$(python -m {{ .module }}{{- range .args }} {{ . | quote }}{{- end }} 2>&1); rc=$?
                  echo "$out"
                  # Pull the loader's "Done: ..." summary line if present
                  # so the Kuma history shows what actually happened
                  # (e.g. "1585 entities, 0 resolver matches, 1585 no_match").
                  KUMA_SUMMARY=$(echo "$out" | grep -E "^Done:|^\[sweep\] DONE" | tail -1 \
                    | sed 's/[^A-Za-z0-9 .,=-]/_/g' | tr ' ' '+' | head -c 200)
                  KUMA_SUMMARY=${KUMA_SUMMARY:-rc=$rc}
                  [ "$rc" -eq 0 ] && KUMA_STATUS=up
                  export KUMA_STATUS KUMA_SUMMARY
                  exit $rc
{{- end -}}
