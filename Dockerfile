# CargoRewind CLI image. Every base is pinned by multi-arch index digest.
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
# Static docker CLI binary, so the CLI can drive a mounted Docker socket.
FROM docker:29.3.1-cli@sha256:18f5ab0fab739ea822819b342357947dfba235cdef438cce345ebc0c143c5b34 AS dockercli

FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
LABEL project=cargorewind \
      org.opencontainers.image.source=https://github.com/vipul21435/cargorewind \
      org.opencontainers.image.licenses=MIT

# git drives the checkout, diff and patch split on the host side of the pipeline.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
# Dependencies first so source edits do not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable
# Bundled demo data: strsim-rs history (MIT, license kept) and a recorded Docker run.
COPY examples ./examples

RUN useradd --create-home --uid 10001 rewind
USER rewind
WORKDIR /home/rewind

ENTRYPOINT ["cargorewind"]
CMD ["--help"]
