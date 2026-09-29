.PHONY: install lint format typecheck test cov demo demo-live record-demo e2e docker-build docker-demo docker-prune check clean

IMAGE ?= cargorewind:dev
DEMO_ARGS = examples/strsim/strsim-rs.bundle --fix 605c81c9b9

install:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy

test:
	uv run pytest -m "not docker"

cov:
	uv run pytest -m "not docker" --cov --cov-report=term-missing --cov-report=xml

# Offline: replays the recorded Docker runs of the strsim-rs fix (no Docker needed).
demo:
	uv run cargorewind rewind $(DEMO_ARGS) --out out/demo --replay examples/strsim/transcript.json

# Live: builds the rust:1.39.0-slim environment and runs the three test stages in Docker.
demo-live:
	uv run cargorewind rewind $(DEMO_ARGS) --out out/demo-live --record out/demo-live/transcript.json
	$(MAKE) docker-prune

# Re-record the committed transcript from a live run.
record-demo:
	uv run cargorewind rewind $(DEMO_ARGS) --out out/demo-live --record examples/strsim/transcript.json
	$(MAKE) docker-prune

e2e:
	uv run pytest -m docker -v
	$(MAKE) docker-prune

docker-build:
	docker build -t $(IMAGE) .
	$(MAKE) docker-prune

docker-demo: docker-build
	docker run --rm $(IMAGE) version
	docker run --rm $(IMAGE) rewind /app/$(word 1,$(DEMO_ARGS)) --fix 605c81c9b9 \
		--out /tmp/demo --replay /app/examples/strsim/transcript.json

# Remove only this project's dangling images.
docker-prune:
	docker image prune -f --filter label=project=cargorewind --filter dangling=true

check: lint typecheck cov

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov out .cargorewind
