
all: build release deploy

build:
	docker build -t contribute.void42.internal/golden/gmr-api:$(shell git rev-parse --short HEAD) .

release:
	docker push contribute.void42.internal/golden/gmr-api:$(shell git rev-parse --short HEAD)

deploy:
	helm upgrade --install gmr ./deployment --set-string version=$(shell git rev-parse --short HEAD)
