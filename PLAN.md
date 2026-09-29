# CargoRewind build plan

CargoRewind rebuilds a Rust crate or workspace at a historical commit inside a
digest-pinned Docker image and verifies the fail-to-pass flip of a fix commit. The tool
is typed Python that drives git, cargo and Docker; cargo only ever runs inside
containers, so the host needs git and Docker but no Rust toolchain.

Work is tracked as checkboxes: a slice is ticked (`- [x]`) only when its commits are
green, pushed, and the README describes it with real output.

## Decisions

- Fresh repository, not a fork. A GitHub search (`gh search repos "rust commit rebuild
  docker fail-to-pass"`, `gh search repos "swe-bench rust"`) found no small, permissively
  licensed library to build on; the only hit was an evaluation-results repository, not
  reusable tooling.
- Baseline tooling: Python 3.12 via uv, src/ layout, ruff, mypy --strict on src/,
  pytest + pytest-cov with a 90% branch-coverage gate, pre-commit, Makefile, GitHub
  Actions (checks job plus a docker build and demo job). Actions are pinned to released
  tags (`astral-sh/setup-uv` publishes no floating major tag) and the runner to
  ubuntu-24.04.
- Host boundary: every git, docker and cargo call goes through one `Runner` protocol, so
  unit tests use a fake runner and recorded fixtures and never need Docker. Tests that
  need a Docker daemon and network carry the `docker` pytest marker; `make test`
  deselects them and the CI docker job runs them.
- Every generated Dockerfile and every `docker run` sets `CARGO_BUILD_JOBS=2` (8 GB
  development machine), uses a digest-pinned `rust:<version>-slim` base, a non-root
  user, and `LABEL project=cargorewind`, so cleanup can target only this project's
  images.
- Demo crate: rapidfuzz/strsim-rs (MIT, zero dependencies, compiles in seconds). Fix
  commit `605c81c9b9` "Fix Jaro and Jaro-Winkler when the length is one" (2019-12-13,
  parent `c4cdd9c35d`) changes one code hunk and adds two `#[test]` functions inside
  `mod tests` of the same `src/lib.rs`, so the patch split must work hunk by hunk, which
  is the hard case for Rust. Expected FAIL_TO_PASS: `tests::jaro_same_one_character`,
  `tests::jaro_winkler_same_one_character`. The newest stable release before the commit
  date is 1.39.0 (2019-11-07); `docker manifest inspect rust:1.39.0-slim` lists
  linux/amd64 and linux/arm64 images. The crate has no Cargo.lock at that commit, which
  exercises the no-lockfile path.
- Second e2e candidate (to verify in slice 6): strsim-rs `f6a759324b` "limit common
  prefix in jaro-winkler" (2023-12-31) edits `src/lib.rs` and `tests/lib.rs`, covering
  the integration-test (tests/) path of the split.
- The offline `make demo` replays a recorded live run: a git bundle of the two demo
  commits (strsim-rs LICENSE kept next to it) and the transcript of real `cargo test`
  output captured from Docker. A separate `make demo-live` performs the same run through
  Docker for real.
- The confidential-name guard is a local pre-push hook only (installed into
  `.git/hooks`); nothing about it is tracked in this repository.

### Decisions made while building the core (2026-09-30)

- Three runs instead of two: base (no patches), before (test patch), after (both).
  When the before run fails to compile, the base run decides whether an existing test
  is PASS_TO_PASS, so a compile error does not inflate FAIL_TO_PASS. Regressions
  (passed before, or passed at base and fail after) block verification.
- Patches are applied on the host with `git apply`, the result is checked blob by blob
  against the fix commit, and each stage's changed files are streamed into
  `docker run -i --network none` as a deterministic tar (`tar -xm` gives fresh mtimes
  so cargo rebuilds them). Old `rust:*-slim` images have no git or `patch`, and
  installing them from archived Debian releases is fragile.
- The patch split already works line by line (not only hunk by hunk): changes of the
  other side become context or are dropped, and hunk ranges are recomputed for the
  intermediate tree. The core scan blanks comments, nested block comments, strings,
  raw strings and char literals; slice 1 still owns out-of-line `mod tests;`,
  `cfg(all(test, ...))`, item-level `#[cfg(test)]` and Cargo.toml target paths.
- Toolchain precedence follows rustup: when both `rust-toolchain` and
  `rust-toolchain.toml` exist, the legacy file wins. The stable table (140 releases,
  1.0.0 to 1.98.1) is parsed from rust-lang/rust `RELEASES.md`; "before the commit
  date" means released on an earlier UTC day than the fix's committer date.
- The commit date used is the fix commit's committer date in UTC.
- Doctest names drop the `(line N)` suffix and get a per-item ordinal, because a fix
  that adds lines above a doctest would otherwise rename it and misreport it.
- Replay transcripts store the sha256 of the Dockerfile and of each stage's overlay;
  replay refuses to answer when they differ. `make demo` replays the transcript
  recorded from a live run on this Mac (`make record-demo` regenerates it).
- The demo bundle is the full strsim-rs history up to `605c81c9b9` (about 650 KB,
  mostly old rendered docs and fonts), so the pre-commit large-file limit is raised
  to 1024 KB. The pre-commit end-of-file fixer added a trailing newline to the copied
  strsim-rs LICENSE; the text is otherwise verbatim.
- The CLI image copies the static docker CLI from `docker:29.3.1-cli` (pinned by
  digest) and installs git from Debian, so it can drive a mounted Docker socket.
- CI's docker job runs the replayed demo, the demo inside the CLI image, and the live
  `docker`-marked e2e test (pull, build and three runs took 23 s on ubuntu-24.04).

## Core (deliverable)

- [x] Core: the smallest end-to-end rewind of one fix commit.

`cargorewind rewind <repo-url-or-path> --fix <sha> [--base <sha>] --out <dir>` does:

1. Git: clone or fetch into a work directory, resolve the base commit (default: the fix
   commit's first parent), read the commit date, and produce `git diff base fix`.
2. Minimal patch split: parse the unified diff into files and hunks. Files under
   `tests/` go to the test patch; everything else goes to the fix patch, except hunks of
   a source file that fall inside a `#[cfg(test)]` module (found by a brace-matching
   scan), which go to the test patch and are listed in the report.
3. Minimal toolchain choice: the channel from `rust-toolchain` or `rust-toolchain.toml`
   if present, otherwise the newest stable released before the commit date (tested
   table of stable release dates). The base image is `rust:<version>-slim@sha256:...`
   from an offline digest table that covers the demo and test versions.
4. Dockerfile: pinned base, non-root user, `LABEL project=cargorewind`,
   `CARGO_BUILD_JOBS=2`, the base checkout copied in, `cargo fetch --locked` when
   Cargo.lock exists (otherwise `cargo generate-lockfile`, not yet date-bounded), and a
   warm `cargo test --no-run`.
5. Run: build the image; run the tests with only the test patch applied (expect
   failures; a compile error counts every new test as failing), then with test and fix
   patches applied (expect passes). Parse libtest text output
   (`test <name> ... ok|FAILED|ignored`). FAIL_TO_PASS = failing before and passing
   after; PASS_TO_PASS = passing in both runs.
6. Export `<out>/task.json` (repo, base, fix, commit date, toolchain, image digest,
   FAIL_TO_PASS, PASS_TO_PASS), `Dockerfile`, `fix.patch`, `test.patch`.
7. `make demo` replays the recorded strsim-rs `605c81c9b9` run offline in under a
   minute; `make demo-live` runs it through Docker. Unit tests use the fake runner; one
   `docker`-marked e2e test runs the live path in the CI docker job. The CLI image gains
   git and the Docker CLI. README with real output and a delivery note.

## Slices

- [ ] 1. Rust-aware patch split and `#[cfg(test)]` report
- [ ] 2. Toolchain inference from toolchain files, MSRV, edition and a dated stable table
- [ ] 3. Dependency reproducibility: locked fetch, date-bounded lockfile, vendoring
- [ ] 4. Dockerfile generation with sanity probes and a recipe-hash build cache
- [ ] 5. Test execution by exact name, libtest text and JSON parsing, flaky detection
- [ ] 6. Task bundle export, `verify` command and batch recipes with two-crate e2e

### 1. Rust-aware patch split and `#[cfg(test)]` report

Classify every changed file by its Rust layout role: integration test (`tests/`),
source (`src/`), bench (`benches/`), example (`examples/`), build script (`build.rs`),
manifest, lockfile, and other (docs, CI). Honor workspace members from `[workspace]
members` globs and custom paths from `[lib]`, `[[test]]`, `[[bench]]` and
`[[example]]` in Cargo.toml. Replace the core's brace scan with a small Rust lexer that
skips line comments, nested block comments, strings, raw strings (`r#"..."#`), byte
strings, char literals and lifetimes, and that understands `#[cfg(test)] mod tests;`
pointing at an out-of-line file and `#[cfg(all(test, ...))]`. Split hunk by hunk,
re-emit patches that `git apply --check` accepts, and write `split.json` listing
shared-file hunks. New `cargorewind split` command. Tests: fixture diffs per role, lexer
edge cases, a round trip that applies both patches to a temporary git repository.

### 2. Toolchain inference from toolchain files, MSRV, edition and a dated stable table

Resolve the toolchain in order: `rust-toolchain.toml` (`[toolchain]` channel,
components, targets, profile), the legacy `rust-toolchain` file, then the newest stable
released before the commit date. Raise the result to at least `package.rust-version`
(including `rust-version.workspace = true` inherited from `[workspace.package]`) and
the edition minimum (2018 -> 1.31, 2021 -> 1.56, 2024 -> 1.85). Map dated channels
(`nightly-YYYY-MM-DD`) to a rustup install on a pinned stable image. Ship the complete
table of stable release dates from 1.0.0 with invariant tests (dates increase, six-week
cadence, point releases). Resolve `rust:<version>-slim` digests through the registry
HTTP API (optional, cached) with the offline table as fallback. New
`cargorewind toolchain <repo> <sha>` prints each decision and its reason. Tests: fixture
manifests and toolchain files, table invariants, fake registry responses.

### 3. Dependency reproducibility: locked fetch, date-bounded lockfile, vendoring

With a Cargo.lock: detect its format version (v1 to v4), check the chosen toolchain can
read it (v3 needs cargo 1.53+, v4 needs 1.78+), then `cargo fetch --locked`. Without a
lockfile: for every dependency requirement pick the newest semver-compatible, non-yanked
version published before the commit date from crates.io version metadata (recorded as
JSON fixtures for tests; live fetch optional and cached), generate a lockfile in the
container and pin it with `cargo update --precise`, iterating over transitive
dependencies until every entry is date-bounded; report any that cannot be. `--vendor`
runs `cargo vendor`, writes the `.cargo/config.toml` source replacement, and runs tests
with `--offline` under `docker run --network none`. Tests: requirement matching (caret,
tilde, wildcard, comparison, multiple bounds), yanked handling, lockfile version
parsing, the pin loop with a fake runner.

### 4. Dockerfile generation with sanity probes and a recipe-hash build cache

Render the Dockerfile deterministically from a typed `Recipe` (image digest, toolchain,
commit, lock strategy, vendoring, commands); the recipe hash is the sha256 of its
canonical JSON and becomes the image tag and a label. Sanity probes prove the fix is
absent at base and present after applying it: identifiers the fix adds (new `fn`,
`struct`, `enum`, `trait`, `const`, `macro_rules!` names parsed from added lines) must
not exist at base and must exist afterwards; the test patch applies cleanly at base;
the fix patch applies cleanly on top of it. Build cache: a local JSON index keyed by
recipe hash behind a file lock, reuse of existing images, `--rebuild`, and pruning of
only this project's dangling images through the label filter. Tests: golden
Dockerfiles, probe logic on fixture patches, cache hit, miss and lock contention with a
fake runner.

### 5. Test execution by exact name, libtest text and JSON parsing, flaky detection

Run selected tests by exact name per target (lib unit tests, each `tests/*.rs` binary,
doctests) with `cargo test --lib|--test <target> -- --exact <name>`. Harden the libtest
text parser: several binaries per run, `running N tests`, ok, FAILED, ignored and bench
lines, panic output interleaved with results, `test result:` summaries, doctest names
with line numbers, `--nocapture` noise. Add a JSON parser for
`-Z unstable-options --format json` used when the toolchain is nightly. Report explicit
statuses (passed, failed, ignored, compile error, timeout, missing). Rerun each
candidate N times (default 3) and mark a test flaky when its outcome changes; flaky
tests leave both lists with a recorded reason. Tests: recorded outputs from several Rust
versions as fixtures, parser tests on generated output, flaky scenarios with a fake
runner.

### 6. Task bundle export, `verify` command and batch recipes with two-crate e2e

Versioned `task.json` schema (typed dataclasses plus a committed JSON schema, validated
on write and read); bundle layout with `task.json`, `Dockerfile`, `fix.patch`,
`test.patch`, `fail_to_pass.txt`, `pass_to_pass.txt`, run logs, and the split and
toolchain reports. `cargorewind verify <bundle>` rebuilds from the bundle alone and
re-checks the flip. `cargorewind batch recipes.toml` runs several crates and commits,
deduplicates by (repository, fix commit), and prints a summary table. E2E under the
`docker` marker in CI on two real crates (strsim-rs `605c81c9b9` and a second verified
tests/ fix); README numbers regenerated from the batch summary.
