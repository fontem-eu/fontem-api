IMAGE := contribute.void42.internal/golden/edgar-gmr-etl:latest

.PHONY: test lint gate mutation build deploy

test:
	python3 -m pytest

lint:
	python3 -m pylint src tests

gate: test lint

mutation:
	python3 -m mutmut run --paths-to-mutate=src/analysis/,src/etl/

build:
	docker build -t edgar-gmr-etl:latest .

deploy: build
	docker tag edgar-gmr-etl:latest $(IMAGE)
	docker push $(IMAGE)
	kubectl set image deployment/gmr-api -n gmr gmr-api=$(IMAGE)
	kubectl rollout status deployment/gmr-api -n gmr --timeout=60s
