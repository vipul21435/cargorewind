# CargoRewind

[![CI](https://github.com/vipul21435/cargorewind/actions/workflows/ci.yml/badge.svg)](https://github.com/vipul21435/cargorewind/actions/workflows/ci.yml)

Rebuild a Rust crate or workspace at a historical commit inside a digest-pinned Docker
image and verify the fail-to-pass flip of a fix commit. CargoRewind is typed Python that
drives git, cargo and Docker; cargo runs only inside containers, so the host needs no
Rust toolchain.

## Status

Scaffold only. What exists today:

- A typed Typer CLI with `version` and `doctor` (checks that `git` and `docker` are on
  PATH and exits 1 when one is missing).
- Tooling: uv, ruff, mypy --strict, pytest with a 90% coverage gate, pre-commit, a
  Makefile, a CLI Docker image (digest-pinned `python:3.12-slim`, non-root user), and
  GitHub Actions with a checks job and a docker build and demo job.

The rewind pipeline itself (patch split, toolchain inference, Dockerfile generation,
fail-to-pass verification, task bundle export) is not built yet; the ordered plan is
in [PLAN.md](PLAN.md).

## Quickstart

```sh
git clone https://github.com/vipul21435/cargorewind && cd cargorewind
make install        # uv sync --frozen
make check          # ruff, mypy --strict, pytest with coverage gate
make demo           # cargorewind version && cargorewind doctor
make docker-demo    # build the CLI image and run it
```

## Roadmap

See [PLAN.md](PLAN.md): the core end-to-end rewind first, then six slices (Rust-aware
patch split, toolchain inference, dependency reproducibility, Dockerfile generation with
sanity probes and a build cache, test execution with flaky detection, task bundle
export with a verify command).

## License

MIT, see [LICENSE](LICENSE).
