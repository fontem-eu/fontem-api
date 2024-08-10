
all: build start attach

build:
	docker compose build

start:
	docker compose down -t 0
	docker compose up -d

attach:
	docker exec -it edgar-gmr-etl-edgar-gmr-etl-1 bash

