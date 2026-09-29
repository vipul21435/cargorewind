.PHONY: install lint format typecheck test cov demo docker-build docker-demo check clean

IMAGE ?= cargorewind:dev

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

demo:
	uv run cargorewind version
	uv run cargorewind doctor

docker-build:
	docker build -t $(IMAGE) .
	docker image prune -f --filter label=project=cargorewind --filter dangling=true

docker-demo: docker-build
	docker run --rm $(IMAGE) version

check: lint typecheck cov

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
