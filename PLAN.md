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

### Decisions made while building slice 1 (2026-09-30)

- The lexer is hand-written (no tree-sitter or other native dependency). The scanner
  needs attributes, `mod` items and balanced delimiters, never full syntax trees, and
  every literal form that can hide a brace is covered by edge-case tests.
- "Test-only" means the cfg predicate implies `test` or `doctest`: `all(...)` when any
  argument does, `any(...)` when every argument does, `not(...)` never. Several `cfg`
  attributes on one item are a conjunction.
- An attributed item ends at a top-level `;` or at the `}` that closes its body. A
  field, variant, match arm or statement that does not start with an item keyword
  also ends at a top-level comma, and at a closing `}` unless an operator follows.
- Module files are resolved with rustc's rules: `name.rs` or `name/mod.rs` next to a
  mod-rs file (crate roots, `mod.rs`), under `stem/` for other files, with inline
  module names as directories, and `#[path]` files treated as mod-rs files (as rustc
  does). The walk only descends into directories that lead to a changed file.
- A file reached by several targets reports the most "production" role (source, build
  script, bench, example, test); it is test code as a whole only if every way it is
  compiled is test-only. Files in the directory of a custom target root (for example
  data next to `[[test]] path = "checks/it.rs"`) share that target's role.
- Benches, examples, build scripts, manifests, lockfiles and other files go to
  `fix.patch`; test-only lines inside Rust files of any role still go to `test.patch`.
  A new source file stays whole in `fix.patch`, because its `mod` line is fix code.
- `shared_hunks` now lists every hunk of a file that lands in both patches, including
  hunks with only fix lines, so `split.json` shows the whole file. `split.json` lists
  only the packages that contain changed files, plus the total count.
- `rewind` runs the same `git apply --check` proof as `split`, writes `split.json` into
  the bundle and stops before Docker when a check fails. `task.json` keeps schema 1
  and gains `split.report`.
- Not done in this slice (listed under Known issues in the README): treating `#[test]`
  functions outside `cfg(test)` as test code, splitting `[dev-dependencies]` manifest
  hunks into `test.patch`, and macro-generated modules.

### Decisions made while building slice 2 (2026-09-30)

- Precedence when both toolchain files exist stays rustup's: the legacy
  `rust-toolchain` wins (rustup reads it and warns). The slice text lists
  `rust-toolchain.toml` first; that order names the sources, while the tie-break follows
  what developers actually ran. `rust-toolchain.toml` must be TOML; the legacy file is
  a bare channel only when it has one non-empty line without `=` or `[`.
- Channels: exact versions (`1.56` becomes the newest `1.56.x`), `stable-YYYY-MM-DD`
  (release of that day), dated `nightly-`/`beta-YYYY-MM-DD`, undated `nightly`/`beta`
  (the channel of the day before the commit, the newest one surely published before
  it) and an ignored host-triple suffix. Custom `path` toolchains are rejected.
  Component and target names are validated against `[A-Za-z0-9_.-]`, because they
  end up in a Dockerfile `RUN` line.
- Floors come from the root package and the members of the root workspace (members
  globs minus `exclude`); unlisted packages elsewhere in the tree are ignored. Floors:
  `rust-version` (own or inherited), edition minimums (package and per-target), and
  1.64 for any `key.workspace = true` in `[package]` or dependency tables. A stable
  result below the highest floor is raised to the lowest release that meets it, at
  its newest patch published before the commit (else its first patch). A pinned file
  is raised too, because cargo refuses to build below `rust-version`; the decision
  names the package that forced it.
- Dated channels are never raised (that would change the channel); a warning is
  recorded when the channel's estimated version (stable minor of that day + 2 for
  nightly, + 1 for beta) is below a floor. They are installed with
  `rustup toolchain install <channel> --profile <p>` on a fixed host image,
  `CHANNEL_HOST_VERSION = 1.98.1` (newest in the digest table, newest rustup), so
  the recipe does not depend on network lookups. Verified once in Docker with
  `nightly-2020-01-01` + rustfmt: rustc 1.42.0-nightly, running as the non-root user
  under `--network none`.
- `ENV RUSTUP_TOOLCHAIN=<chosen>` is set whenever the checkout has a toolchain file
  (or a channel is installed). Otherwise rustup in the container follows the file,
  which for `stable` means today's stable and for a raised pin means the too-old
  version. It is not set when there is no file, so the strsim-rs Dockerfile and the
  recorded replay transcript stay byte-identical.
- On stable images the file's components and targets are added with
  `rustup component add` / `rustup target add`; its `profile` is recorded but not
  applied (the slim images use the minimal profile).
- Registry lookup is opt-in (`--registry`): cache first, then Docker Hub (anonymous
  token + HEAD on the manifest, `Docker-Content-Digest`), then the offline table as
  fallback when the registry fails. The cache is one JSON file written atomically
  (`$CARGOREWIND_CACHE_DIR`, `$XDG_CACHE_HOME/cargorewind` or `~/.cache/cargorewind`);
  entries never expire. Recorded registry responses in `tests/fixtures/registry/` drop
  the rate-limit and client-address headers and replace the token.
- `toolchain.json` (schema 1) is written by both `cargorewind toolchain --json` and
  `rewind`; `task.json` gains `toolchain.decisions`, `image_source` and
  `toolchain_report` and keeps schema 1 (additive fields).
- `cargorewind toolchain <repo> <sha>` takes the fix commit like the other commands:
  files are read at its base (first parent unless `--base`), the date rule uses its
  committer date.

### Decisions made while fixing the slice 1 and 2 review findings (2026-09-30)

- A package under a `tests` directory of an enclosing package or workspace that no
  enclosing workspace lists is a fixture crate: its files, `Cargo.toml` and
  `Cargo.lock` included, are test data of that package (`test.patch`). A fixture with
  its own `[workspace]` table is still a fixture; a listed member stays a package.
- Path conventions: a `tests` directory anywhere below the package root (also
  `src/**/tests/`) means test data, checked before the `src/`, `benches/` and
  `examples/` rules.
- Item ends: block-like statements (`if`, `for`, `while`, `loop`, `match`, labeled
  blocks, `unsafe {`) end at their brace unless `else` follows; `let`, `static`,
  `const X`, `use` and `type` end at their semicolon; items with a body skip braces
  inside `<...>` (tracked only in the header, with `->` not closing a generic); a
  match arm whose body after `=>` is block-like ends at that body (plus its comma).
- A plain `mod name;` whose file starts with `#![cfg(test)]` is a test-only region of
  the declaring file (reported as `module-decl`), so the declaration and the file land
  in the same patch. A test-only module file declared only by a new or deleted source
  file stays in `fix.patch` with it, so `test.patch` never holds an orphan file or
  leaves a dangling `mod` line.
- The projection recomputes "No newline at end of file" markers per image: a base line
  without a newline that gains lines after it in the intermediate tree is emitted as a
  removed and an added line. A seeded random-edit test (10 seeds; the old projection
  failed seeds 2 and 4) guards it.
- `parse_diff` splits on `\n` only.
- The work checkout is guarded by an `flock` (`.repo.lock` next to it), held by the
  clone or fetch, `check_split` and `build_overlays`; the lock is reentrant per `Git`
  object. `build_overlays` compares the patched tree with the fix commit again before
  capturing overlays. Each rewind builds from its own temporary context directory.

### Decisions made while building slice 3 (2026-09-30)

- crates.io metadata comes from the sparse index (`https://index.crates.io`), one
  request per crate: it now carries `pubtime` for every version (backfilled for old
  ones), plus `yanked`, `rust_version` and dependencies. The crates.io web API is not
  used (rate limits, one request per version for dependencies).
- The cutoff is the fix commit's committer time (the same commit time the toolchain
  rule uses); an entry is late when `pubtime >= cutoff` or unknown. Candidates are
  non-yanked (today's flag; cargo refuses yanked versions anyway), not pre-releases
  unless a requirement names one, published before the cutoff, and matching every
  requirement on the entry: the member manifests for workspace members, the index
  entry of each dependent's locked version (normal and build dependencies) otherwise.
- Pin rounds: only late entries without late dependents are pinned in a round (falling
  back to the rest when none of those can move), all in one session script, each with
  its own exit status. A refused pin excludes that version for that entry and the next
  round tries the next older one. At most 30 rounds; what is still late is reported
  with a reason and `lock` exits 2.
- `cargo update -p name:version --precise v`: the `name:version` spec form works on
  every cargo (the `@` form only works on newer ones).
- The pin loop runs cargo, not a Python resolver: cargo writes the lockfile in its own
  format and resolution, and old cargo versions only need to understand their own
  lockfiles. It runs in one long-lived container (`docker run -d ... sleep infinity`,
  then `docker exec` per round), so an old cargo clones the git registry index once.
- The Dockerfile gains a `toolchain` stage only for the date-bounded strategy: the stage
  is built with `--target toolchain`, the loop runs in it, the lockfile is written into
  the build context and the final stage copies it and runs `cargo fetch --locked`.
  Committed lockfiles and crates without crates.io dependencies keep one stage (the
  strsim-rs Dockerfile changed only in its comment; its transcript was re-recorded).
- Lockfile formats: v1 when there is no `version` key and no per-package checksum (this
  also covers lockfiles without registry packages, which every cargo reads), v2 with
  per-package checksums, v3 and v4 from the `version` key; other values are errors.
  The read floors follow the release notes: v2 1.41, v3 1.53 (as the slice text asks),
  v4 1.78. They join the toolchain floors, so an unreadable committed lockfile raises a
  stable toolchain (decision step `lockfile`, source `Cargo.lock`).
- `--vendor`: `cargo vendor --locked ~/vendor > ~/.cargo/config.toml` in the image
  (`config` below cargo 1.39, refused below 1.37), `ENV CARGO_NET_OFFLINE=true`, and
  `--offline` on the warm build and every stage run. The configuration lives in the
  container user's home (an ancestor of the checkout), so the checkout and the patches
  are untouched.
- Images are tagged `cargorewind/<repo>:<base12>-<sha256 of Dockerfile and lockfile>`
  and toolchain stages `cargorewind/toolchain-stage:<sha256 of the Dockerfile>`, so
  runs with different lockfiles never share a tag. Transcripts gain `build:<target>`
  entries and `steps` keyed by session step name with a script digest (schema stays 1;
  old transcripts replay unchanged).
- Second demo crate: harryfei/which-rs (MIT) at `e776ff0` (2023-10-17, rust 1.73.0,
  no Cargo.lock). The undated lockfile locks `home 0.5.12`, whose manifest cargo 1.73
  cannot parse (edition 2024); the bounded one pins 16 of 41 entries in 4 rounds and
  builds. Its history is bundled with its license; the index files the live run read
  are committed, trimmed to the fields the pin loop reads (`record.py --minimal`,
  428 KB), so `make lock-demo` replays offline. The commit changes no test, so it
  demonstrates the environment, not a flip; the verified flip with dependencies stays
  in slice 6. `rust:1.73.0-slim` joined the offline digest table (the digest the
  registry returned for the live run).
- `lock` takes the fix commit positionally like `toolchain`; `rewind` gains `--vendor`
  and `--index-dir`. `task.json` keeps schema 1 and gains `lock_report` and
  `vendored`; `lockfile` is now `committed`, `generated` or `date-bounded`.

### Decisions made while fixing the slice 2 and 3 review findings (2026-09-30)

- Every pattern that validates text bound for a Dockerfile line (toolchain channel,
  exact version, component and target names, image digests) is applied with
  `fullmatch`: `$` also accepts a final newline, which let a toolchain file start a new
  Dockerfile instruction.
- A host triple must start with a letter (every architecture does), so an unpadded
  date such as `nightly-2020-1-01` is an unsupported channel instead of an undated
  nightly with an ignored host. Impossible dates are `ToolchainError`s.
- `GitTree` resolves symlinks inside the tree only for readers that stand in for
  rustup and cargo (toolchain inference and lock planning); the split keeps reading
  links as the diff shows them. A link that leaves the tree, dangles or loops reads as
  a missing file.
- `RUSTUP_TOOLCHAIN` is also set when a patch adds or changes a toolchain file, and the
  toolchain decisions gain a `pin` step that says so. Pinning always would have
  changed the strsim-rs Dockerfile for no behavioral gain.
- A digest cache that cannot be written (read-only directory, a file in its place) is
  reported in the image reason; the registry answer is kept.

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

- [x] 1. Rust-aware patch split and `#[cfg(test)]` report
- [x] 2. Toolchain inference from toolchain files, MSRV, edition and a dated stable table
- [x] 3. Dependency reproducibility: locked fetch, date-bounded lockfile, vendoring
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
