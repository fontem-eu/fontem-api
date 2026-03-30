
all: build release deploy

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
