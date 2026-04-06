
SONAR_URL   ?= http://sonarqube.sonarqube.svc.cluster.local:9000
SONAR_TOKEN ?= $(shell cat /config/.sonarqube-token 2>/dev/null)
SCANNER     ?= /config/.local/sonar-scanner/bin/sonar-scanner
JAVA_HOME   ?= $(shell dirname $$(dirname $$(readlink -f $$(which java))))
export JAVA_HOME

PROJECT     := edgar-gmr-etl
SRC         := src
TESTS       := tests

all: build release deploy

# ── Quality ──────────────────────────────────────────────────
test:
	python3 -m pytest $(TESTS) \
		--cov=$(SRC) \
		--cov-report=xml:coverage.xml \
		--junitxml=test-results.xml \
		--cov-config=.coveragerc \
		-q
	python3 -m pylint $(SRC) $(TESTS) \
		--output-format=parseable \
		--reports=no \
		> pylint-report.txt 2>&1 || true
	@tail -1 pylint-report.txt

analyze: test
	$(SCANNER) \
		-Dsonar.projectKey=$(PROJECT) \
		-Dsonar.sources=$(SRC) \
		-Dsonar.tests=$(TESTS) \
		-Dsonar.language=py \
		-Dsonar.python.coverage.reportPaths=coverage.xml \
		-Dsonar.python.xunit.reportPath=test-results.xml \
		-Dsonar.python.pylint.reportPaths=pylint-report.txt \
		-Dsonar.host.url=$(SONAR_URL) \
		-Dsonar.token=$(SONAR_TOKEN) \
		-Dsonar.scm.provider=git \
		'-Dsonar.coverage.exclusions=src/etl/**/*'
	@echo "Dashboard: $(SONAR_URL)/dashboard?id=$(PROJECT)"

# ── Infra ────────────────────────────────────────────────────
neo4j:
	kubectl apply -f infra/neo4j/secret.yaml
	kubectl apply -f infra/neo4j/pvc.yaml
	kubectl apply -f infra/neo4j/deployment.yaml
	kubectl apply -f infra/neo4j/service.yaml
	@echo "Waiting for Neo4j to be ready..."
	kubectl -n gmr rollout status deployment/neo4j --timeout=120s
	@echo "Neo4j is ready."

build:
	docker pull python:3.12-slim
	docker build -t contribute.void42.internal/golden/gmr-api:$(shell git rev-parse --short HEAD) .

release:
	docker push contribute.void42.internal/golden/gmr-api:$(shell git rev-parse --short HEAD)

deploy:
	helm upgrade --install gmr ./deployment --set-string version=$(shell git rev-parse --short HEAD) --force-conflicts
	@echo "Deploying..."
	kubectl -n gmr rollout restart deployment gmr-api
	@echo "Waiting for deployment to become ready..."
	kubectl -n gmr rollout status deployment/gmr-api --timeout=300s
	@echo "Deployment is ready!"

.PHONY: all test analyze neo4j build release deploy

# ── Security & SBOM ─────────────────────────────────────────
audit:
	pip-audit -r requirements.txt --desc 2>&1 || true
	@echo ""
	@echo "=== Renovate Dependency Report ==="
	LOG_LEVEL=warn npx renovate --platform=local --dry-run 2>&1 | grep -E "dependency|update|→|->|current|new" | head -30 || true

sbom:
	cyclonedx-py requirements requirements.txt --of json -o sbom.json
	curl -s -X POST "$(DTRACK_URL)/api/v1/bom" \
		-H "X-Api-Key: $(DTRACK_KEY)" \
		-H "Content-Type: multipart/form-data" \
		-F "autoCreate=true" \
		-F "projectName=$(PROJECT)" \
		-F "projectVersion=main" \
		-F "bom=@sbom.json" > /dev/null
	@echo "SBOM uploaded to Dependency-Track"

.PHONY: audit sbom
