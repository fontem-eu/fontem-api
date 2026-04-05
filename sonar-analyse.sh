#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# sonar-analyse.sh — Run tests, lint, and upload analysis to SonarQube
#
# Usage:  ./sonar-analyse.sh
# Env:    SONAR_TOKEN  (or /config/.sonarqube-token)
#         SONAR_URL    (default: http://sonarqube.sonarqube.svc.cluster.local:9000)
#         SCANNER_HOME (default: /config/.local/sonar-scanner)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_KEY="edgar-gmr-etl"
SRC_DIR="src"
TEST_DIR="tests"

SONAR_URL="${SONAR_URL:-http://sonarqube.sonarqube.svc.cluster.local:9000}"
SONAR_TOKEN="${SONAR_TOKEN:-$(cat /config/.sonarqube-token 2>/dev/null || echo '')}"
SCANNER="${SCANNER_HOME:-/config/.local/sonar-scanner}/bin/sonar-scanner"
export JAVA_HOME="${JAVA_HOME:-$(dirname $(dirname $(readlink -f $(which java))))}"

if [ -z "$SONAR_TOKEN" ]; then
  echo "ERROR: Set SONAR_TOKEN or create /config/.sonarqube-token" >&2; exit 1
fi

cd "$(dirname "$0")"

echo "=== [$PROJECT_KEY] Step 1: Tests + coverage ==="
python3 -m pytest "$TEST_DIR" \
  --cov="$SRC_DIR" \
  --cov-report=xml:coverage.xml \
  --junitxml=test-results.xml \
  -q

echo ""
echo "=== [$PROJECT_KEY] Step 2: Pylint ==="
python3 -m pylint "$SRC_DIR" "$TEST_DIR" \
  --output-format=parseable \
  --reports=no \
  > pylint-report.txt 2>&1 || true
tail -1 pylint-report.txt

echo ""
echo "=== [$PROJECT_KEY] Step 3: SonarQube analysis ==="
"$SCANNER" \
  -Dsonar.projectKey="$PROJECT_KEY" \
  -Dsonar.projectName="$PROJECT_KEY" \
  -Dsonar.sources="$SRC_DIR" \
  -Dsonar.tests="$TEST_DIR" \
  -Dsonar.language=py \
  -Dsonar.python.coverage.reportPaths=coverage.xml \
  -Dsonar.python.xunit.reportPath=test-results.xml \
  -Dsonar.python.pylint.reportPaths=pylint-report.txt \
  -Dsonar.host.url="$SONAR_URL" \
  -Dsonar.token="$SONAR_TOKEN" \
  -Dsonar.scm.provider=git

echo ""
echo "=== Done: $SONAR_URL/dashboard?id=$PROJECT_KEY ==="
