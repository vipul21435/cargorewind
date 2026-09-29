.PHONY: install lint format typecheck test cov demo split-demo toolchain-demo lock-demo lock-demo-live record-lock-demo demo-live record-demo e2e docker-build docker-demo docker-prune check clean

IMAGE ?= cargorewind:dev
DEMO_ARGS = examples/strsim/strsim-rs.bundle --fix 605c81c9b9
LOCK_ARGS = examples/which-rs/which-rs.bundle e776ff0 --index-dir examples/which-rs/index

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

# Offline: split the strsim-rs fix into test.patch and fix.patch and write split.json.
split-demo:
	uv run cargorewind split $(DEMO_ARGS) --out out/split

# Offline: print every toolchain decision for the strsim-rs fix and write toolchain.json.
toolchain-demo:
	uv run cargorewind toolchain $(word 1,$(DEMO_ARGS)) 605c81c9b9 --json out/toolchain/toolchain.json

# Offline: replays the recorded pin loop that bounds which-rs e776ff0 (no Cargo.lock) by date.
lock-demo:
	uv run cargorewind lock $(LOCK_ARGS) --out out/lock-demo --replay examples/which-rs/lock-transcript.json

# Live: the same pin loop with real cargo in a rust:1.73.0-slim container.
lock-demo-live:
	uv run cargorewind lock $(LOCK_ARGS) --out out/lock-demo-live
	$(MAKE) docker-prune

# Re-record the committed lock transcript from a live run.
record-lock-demo:
	uv run cargorewind lock $(LOCK_ARGS) --out out/lock-demo-live --record examples/which-rs/lock-transcript.json
	$(MAKE) docker-prune

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
