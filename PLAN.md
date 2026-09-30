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
  the integration-test (tests/) path of the split. Rejected in slice 6 (a regression,
  see there); dtolnay/semver `d92a4d8` took its place.
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

### Decisions made while building slice 4 (2026-09-30)

- The `Recipe` stays in `dockerfile.py` and gains the lockfile sha256, the probe words
  and the warm and test commands (empty means the default for the vendoring mode).
  It validates every field with `fullmatch` in `__post_init__` (`RecipeError`), so
  the Dockerfile is safe by construction and not only because its inputs were
  checked upstream. `recipe.json` is the recipe plus its hash; `Recipe.from_dict`
  reads it back (for the planned `verify`).
- Recipe hash: sha256 of `json.dumps(as_dict(), sort_keys=True, separators=(",",
  ":"), ensure_ascii=True)`, with a `schema` field. The source URL is not part of it
  (the base commit identifies the content), so a bundle and a URL of the same
  repository share images. Tag: `cargorewind/<repo slug>:<first 16 hex digits>`; the
  full hash is the `cargorewind.recipe` label on the last line of the Dockerfile, so it
  never invalidates Docker's layer cache.
- A date-bounded recipe renders its toolchain stage on its own (no probe, no label)
  for the pin loop, then the whole file once the lockfile's sha256 is known. Both
  demo transcripts were re-recorded live (the strsim-rs Dockerfile gained the probe
  and the label; the which-rs stage file lost the final stage); the pin loop's output
  was identical.
- Probe identifiers: `fn`, `struct`, `enum`, `trait`, `const` (only `const NAME:`
  outside generic parameters) and `macro_rules!` names defined on added lines, found
  with the Rust lexer on the file after its patch. A name is kept only if it occurs
  nowhere at base as a whole word (one `git grep -I -o -w -F` call over `*.rs`), which
  makes the in-container `grep -w` exact. A name added by both patches belongs to the
  test patch. At most 16 probes, the fix's names first. The probe line numbers refer
  to the file after the probe's own patch.
- Where each check runs: the image build greps for absence (a `RUN grep ... ; test
  $? -eq 1` step after the checkout copy, in the final stage); the before and after
  stage scripts grep for presence before cargo and exit 97 with a marker line; the
  host checks definitions in the exact overlay files. A failed host check stops
  `rewind` before Docker (exit 1, `ProbeError`); a failed stage probe makes the task
  unverified (exit 2). The `git apply --check` results of the split are part of
  `probes.json`.
- Transcripts record the sha256 of each stage script, so a replay with different
  probe words is refused; transcripts without it (older ones) still replay.
- Build cache: `build-index.json` in the cache directory (`--cache-dir`, the same one
  the registry and index caches use). A hit needs the image's recipe label to match;
  the indexed tag is tried before the requested one. One `flock` per recipe
  (`build-locks/<hash>.lock`) from lookup to the end of the build, polled with
  `LOCK_NB` up to a timeout (2 h by default); the index has its own short lock and is
  replaced atomically. An unusable cache directory or index never fails a build.
- `--rebuild` means `docker build --no-cache` (with Docker's layer cache the rebuild
  would otherwise be a no-op), then `docker image prune -f --filter
  label=project=cargorewind --filter dangling=true`. The cache is off for `--record`
  (a transcript must hold a real build) and `--replay`, and with `--no-build-cache`.
- `cargorewind cache list` and `cache prune` (dangling images of this project, then
  index entries whose image is gone) are a Typer sub-app.

### Decisions made while fixing the slice 3 and 4 review findings (2026-09-30)

- A dependent that asks for one crate several times (a renamed second version, optional
  aliases of different versions, per-target tables) constrains each lockfile edge only
  with the requirements its locked version satisfies, because that is the requirement
  cargo resolved the edge against. When none is satisfied, all are kept (the entry is
  then reported). The fake resolver in the tests checks each edge against its own
  requirement too, so the unit tests can no longer hide this.
- The manifest scan follows path dependencies (transitively, once per manifest; paths
  in `[workspace.dependencies]` are relative to the workspace root), because cargo makes
  them workspace members and resolves their crates.io dependencies. A git dependency,
  or a path dependency outside the tree or without a readable manifest, may bring
  crates.io packages that only a generated lockfile shows, so it also selects the
  date-bounded strategy (the pin loop reads the lockfile, not the manifests). The
  generated strategy is left for crates whose whole reachable graph has no crates.io
  or git dependency.
- `BoundLock.pins()` drops a pin only when it failed in its own round, so the summary
  counts a pin that cargo accepted on retry.
- `rewind --cache-dir` reaches the crates.io index (`RewindOptions.cache_dir`), as it
  already did for `lock`.
- Index cache files are written through `tempfile.mkstemp` in the target directory and
  `os.replace`, so runs that share the cache never move each other's temporary files.
  A cache that cannot be written (read-only home, a file in its place) is noted once in
  `lock.json` and the fetched answer is used, like the digest cache.

### Decisions made while building slice 5 (2026-09-30)

- One parser reads both libtest formats line by line: JSON events where a line parses
  as one, cargo's `Running` and `Doc-tests` lines (which stay text even with
  `--format json`) to know the binary, and the text result lines otherwise. A text
  result line whose status is missing (`--nocapture`, `--test-threads=1`, or a panic
  from another thread that split the line, all recorded on 1.39.0) stays pending until
  a line that is only a status; a test that started and never reported failed, or timed
  out when the run was stopped.
- A test is identified by (cargo target, stable name). Binaries resolve to targets
  through the layout of the fix and base commits (the crate-style binary name, plus the
  source path newer cargo prints; old cargo prints only the binary, where the library
  wins over a test target of the same name), with path conventions for targets the
  layout does not list and an `unknown` target (no selector, every binary runs and the
  result is picked by binary) as the last resort. The id in the lists is the name alone
  unless two targets share it (`shared_name [test it]`), so the strsim-rs task keeps
  its names.
- Mode suffixes (` - should panic`, ` - compile fail`, ` - compile`) are display only
  and leave the name, so text and JSON names agree and `--exact` matches. Doctests of
  the crate's own docs have no item (`src/lib.rs - (line 8)`, seen on which-rs) and get
  the stable name `src/lib.rs - (crate)`.
- rustdoc splits its test arguments on whitespace, so a doctest cannot be selected by
  its exact name (`cargo test --doc -- --exact "src/lib.rs - add (line 7)"` ran 0 tests
  on 1.39.0, 1.73.0 and 1.98.1). Doctests are rerun with the item path as a substring
  filter (`--doc -- add`) and the exact name is picked from the output. libtest
  before 1.5x ignores every filter but the first, so each rerun is one cargo
  invocation (about 7 ms each on 1.39.0 when nothing changed).
- Reruns: every FAIL_TO_PASS and PASS_TO_PASS candidate, N times (default 3, `--reruns
  0` turns them off), in one container per stage (`rerun-after` for all candidates,
  `rerun-before` for those the before run reported, `rerun-base` for PASS_TO_PASS
  tests whose verdict came from the base run because before did not build). Each
  command runs under coreutils `timeout` (`--test-timeout`, default 300 s; exit 124 or
  137 is `timeout`). Statuses: passed, failed, ignored, compile-error (the segment shows
  a build error and no binary started), timeout, missing (the run finished without
  reporting the test). A candidate whose outcomes differ between the stage run and any
  rerun leaves both lists with the reason; the flip stays verified when a FAIL_TO_PASS
  test remains. The reruns are recorded and replayed like the stage runs
  (`runs["rerun-<stage>"]`), so the strsim-rs transcript was re-recorded live (21 s
  including 2 x 3 x 104 reruns). The CI e2e step went from 51 s to 3 min 0 s.
- The JSON format is requested only for nightly channels installed with rustup
  (`-- -Z unstable-options --format json` on the stage and rerun commands; the recipe
  hash changes for those recipes only). The JSON fixtures were recorded on stable
  images with `RUSTC_BOOTSTRAP=1`, which unlocks the same libtest code path; the text
  fixtures come from the same three images (1.39.0, 1.73.0, 1.98.1) running the
  `tests/fixtures/libtest/zoo` crate (`record.sh`).
- `task.json` keeps schema 1 and gains `runs.<stage>.state`, `reruns`, `tests` (target,
  rerun command, status per stage, rerun outcomes for every test seen) and `flaky`.

### Decisions made while fixing the slice 4 and 5 review findings (2026-09-30)

- The base-commit word search reads its words from a pattern file (`git grep -f`), so
  a generated fix with tens of thousands of new names never exceeds the argument
  length limit, and it fixes its output format on the command line (`-c
  grep.lineNumber=false -c grep.column=false -c color.grep=never`, `--no-color`), so
  a user's git configuration cannot turn every name into "absent at base".
- The recipe hash now covers the Dockerfile body (every line but the label) after the
  canonical JSON, and `recipe.json` records the body's own sha256. A template change
  in a newer cargorewind is a new recipe, so a cached image built from the old
  template is never reused and the exported Dockerfile always describes the image
  the runs used. The strsim-rs transcript was re-recorded live (the label changed).
- Build index entries are checked field by field (`build_seconds` a number, the rest
  strings); a mistyped entry is ignored like a missing one, and the image is found
  again through its label. The prune after `--rebuild` is best effort: a failure
  (Docker refuses concurrent prunes) goes into the build reason, not the exit code.
- `const NAME` is a definition only when the name is not followed by `::` and `const`
  is not preceded by `*`, so a raw pointer to a path (`*const std::ffi::c_void`) no
  longer becomes a bogus `const std`.

### Decisions made while building slice 6 (2026-09-30)

- `task.json` is schema 2, typed twice: dataclasses in `bundle.py` (what the pipeline
  fills and `verify` reads) and a JSON Schema 2020-12 document packaged as
  `cargorewind/schemas/task.schema.json` (`additionalProperties: false` everywhere,
  enums for statuses, states and strategies, sha256 and commit patterns), checked with
  `jsonschema` on every write and read. Per-stage test statuses moved under
  `tests.<id>.statuses`; `FAIL_TO_PASS` and `PASS_TO_PASS` keep their names.
- The bundle carries the base tree as `base.bundle`: a git bundle with one root commit
  made by `git commit-tree` from the base commit's tree, with fixed author, committer
  and date, so its id is reproducible and the tree id equals the base commit's (both
  are recorded under `base_tree` and checked by `verify`). It holds no history, so it
  is as small as the tree; `git clone` of it warns about a missing HEAD, which is
  harmless. `task.json` also records the sha256 of every other bundle file
  (`files`), `fail_to_pass.txt` and `pass_to_pass.txt` list one id per line.
- `cargorewind verify <bundle>` reads nothing outside the bundle: it checks the
  manifest, that `recipe.json` hashes to the recorded hash and renders the bundle's
  `Dockerfile` byte for byte (plus the lockfile's sha256 for a date-bounded recipe and
  the probe words), clones `base.bundle`, applies both patches and commits the result
  so the rest of the pipeline (overlays, targets, probes, stages, reruns) runs
  unchanged, and compares the new lists with the recorded ones. An inconsistent
  bundle is an error before any build (exit 1); a flip or a list that does not hold
  again is NOT VERIFIED (exit 2). It writes `verify.json` and its logs into
  `<bundle>/verify/` by default, never into the manifest's files. The strsim-rs
  transcript replays a verify as well as a rewind (same Dockerfile, overlays and
  scripts), so `make verify-demo` is offline.
- `rewind.py` exposes the shared execution (`run_stages`, `rerun_candidates`,
  `execute`, `build_image`, `targets_of`) over an `Execution` protocol that both
  reports implement.
- `cargorewind batch recipes.toml`: `[[task]]` tables (`repo`, `fix`, optional
  `base`, `name`, `vendor`, `image`, `index_dir`, `replay`, `reruns`,
  `test_timeout`) plus `[defaults]`; unknown keys and bad types are errors with the
  task number. Local paths are relative to the recipes file and spelled relative to
  the working directory when they lie below it, so logs, `task.json` and the summary
  hold no absolute home paths.
- Dedupe key: (repository slug, resolved fix commit), so a short and a full SHA, or a
  bundle and a URL of one repository, are one task. (Widened in the refresh below.) The key is claimed only by a task
  that reached a verdict; after an error the next spelling runs. The bundle directory
  is the task's `name` or `<slug>-<fix12>`; a name held by a task with a verdict is an
  error of the later task, never an overwrite.
- Checkouts live under `<workdir>/<slug>-<sha256(source)[:8]>`, not `<slug>`: two
  repositories with one name (forks) sharing a checkout made the second task fetch
  the first repository and fail with an unknown commit (found while finishing the
  stash; regression test added).
- Errors listed in the CLI's `FAILURES` (a missing transcript included: the batch
  builds `ReplayBackend` itself instead of the rewind helper that exits) end one task
  with status `error` and the batch goes on. Exit code: 1 if any error, else 2 if any
  not verified, else 0. `summary.json` (schema 1) and `summary.md` list name,
  repository, commits, status, seconds (wall, including the clone), bundle,
  toolchain, lockfile strategy, list sizes and detail. `--live` drops the replay
  files.
- Second crate: dtolnay/semver `d92a4d8` (MIT OR Apache-2.0, bundled under MIT),
  "Add a dedicated error for parsing Version from empty string", committed
  2023-03-12. It changes `tests/test_version.rs` (integration test path) and has no
  Cargo.lock with one optional crates.io dependency, so it is the verified flip with a
  date-bounded lockfile that slice 3 left open: rust 1.68.0, 7 late crates.io
  packages, one pin (serde 1.0.229 -> 1.0.155), FAIL_TO_PASS `test_parse`, 34
  PASS_TO_PASS, 0 flaky in 3 reruns. `rust:1.68.0-slim` joined the offline digest table
  (the registry's index digest, checked with `docker buildx imagetools inspect`).
- strsim-rs `f6a759324b` was rejected: a live run (rust 1.75.0; the commit is
  authored 2023-12-31 but committed 2024-01-05) finds 2 FAIL_TO_PASS tests, but
  `tests::jaro_winkler_very_long_prefix` passes at base and fails before and after the
  fix (`actual: 0.985, expected: 0.9851851851851852`), a regression, so NOT VERIFIED.
- The docker job runs `make batch-demo` (offline) and the e2e suite gains
  `test_live_batch_verifies_two_real_crates`, which runs `examples/batch.toml` through
  Docker with the replay files dropped. cargo 1.68 still downloads the crates.io git
  index (sparse became the default in 1.70), which dominates the semver live time;
  setting the sparse protocol for 1.68 and 1.69 is left as a known issue because it
  would change those recipes.

## Refresh 2026-09-30: review findings

Five confirmed findings were fixed, each with regression tests:

- Bundle names are claimed by `dir_key(name)` (NFD, casefold, NFC), so `Demo` and
  `demo` (or two normalizations of one name) are one directory, as on APFS; the later
  task is an error naming the owner. A samefile check was not added: the key covers
  the default macOS file system and Linux is case-sensitive.
- Dedupe key is now (slug, fix, resolved base, vendor, image). The base is the
  explicit one or the first parent (empty for a root commit, which rewind rejects
  later). Two unnamed tasks with one fix and different bases share the default name
  `<slug>-<fix12>`, so the second is a name error; kept as a known issue rather than
  inventing a longer default name.
- `rewind` calls `bundle.clear_bundle(out)` before writing: it removes the bundle
  files, `Cargo.lock`, `task.json`, `logs/*.log`, `verify/verify.json` and
  `verify/logs/*.log`, and nothing else (no `rmtree` of a user-chosen `--out`).
- `open_checkout` fetches only when `remote.origin.url` names the same source (URLs
  compared as strings, local paths by `realpath`), otherwise it re-clones. Default
  work directories of split, toolchain, lock and rewind are
  `.cargorewind/<slug>-<sha256(identity)[:8]>`, verify's
  `.cargorewind/verify-<dir>-<8 hex>` of the resolved bundle directory. Batch keeps
  its own `BatchTask.checkout` key.
- `gitops.is_remote` applies git's rule (`://`, or a colon before the first slash)
  and, like `git clone`, an existing local path wins; the batch loader uses it.
- The live-batch table in the README predates the new duplicate message; it is kept
  as recorded and labeled as such rather than re-running 3 min 42 s of Docker.

## Late review of slice 5: findings fixed (2026-09-30)

Five confirmed findings from a late review of slice 5, each fixed with a regression
test that fails on the old code:

- Old cargo prints only the binary, so the library, `src/main.rs` and
  `tests/<crate>.rs` all run as `<crate>-<hash>`. Such a binary now resolves to a
  `shared` target naming every candidate (`lib x or test x`), rerun with `--tests`.
  Chosen over a union of `--lib --test x` because a target can be absent in one stage
  (a test file the test patch adds does not exist at base, and `--test x` would then
  be a cargo error); `--tests` runs every binary a stage runs and no doctests. Chosen
  over mapping by run order, which breaks when a candidate does not run (`test =
  false`). Checked on rust 1.39.0 in this project's strsim-rs image; the recording is
  `tests/fixtures/libtest/shared-binary-1.39.0.txt` (an original crate).
- rustdoc 1.39 names a crate-level doctest `src/lib.rs -  (line N)` (two spaces); the
  name and doctest patterns accept an empty item, and the stable name is
  `src/lib.rs - (crate)` on every toolchain.
- A rerun round that never reached its test (the rerun run hit `--timeout`, or the
  command hit `--test-timeout` before any test binary started) is left out of the
  flaky comparison and logged, instead of counting as `timeout`. No up-front budget
  check was added: the worst case (rounds x candidates x `--test-timeout`) exceeds
  the default `--timeout` for any real suite, so a warning would fire on every run;
  the README lists it under Known issues. No task schema change: a shorter `reruns`
  list plus the stage's `timed_out` say what happened.
- The per-test `command` in task.json is built with `shlex.join`.
- `assign_ids_qualify_names_that_two_targets_share` lacked the `test_` prefix and
  never ran; renamed.

## Follow-up on the shared target fix (2026-09-30)

- A shared target's rerun (`--tests`) dropped `--no-fail-fast` with every other
  rerun, so a failing test of the same name in a binary that runs first (tests/api.rs
  before tests/<crate>.rs) stopped cargo before the shared binary: every round was
  `missing` and the test was dropped as flaky. `rerun_command` now drops
  `--no-fail-fast` only when the selector runs one binary (lib, bin, test, bench,
  example, doc) and keeps it, adding it if the stage command lacks it, for shared and
  unknown targets, which run several. Single-binary reruns keep their old command, so
  the recorded strsim-rs and semver transcripts (checked by script digest) still
  replay. Checked on rust 1.39.0; the recording is
  `tests/fixtures/libtest/shared-binary-fail-fast-1.39.0.txt` (an original crate).
- Left as a known issue: the namesake in the other binary still runs, and one that
  hangs uses up the rerun's `--test-timeout`, so the shared test reads `timeout` for
  that round.

## Stashed work

- `stash@{0}` "wip from interrupted agent" was restored with `git stash pop` on
  2026-09-30 and finished in slice 6 (see its decisions). Kept after review:
  `batch.py`, `tests/test_batch.py`, the `batch` command, `examples/batch.toml`, the
  semver bundle and its LICENSE. Dropped: the empty semver transcript (re-recorded
  live) and the strsim-rs bundle with a `demo` ref at `f6a759324b` (that commit is not
  a clean flip), so the committed strsim-rs bundle is unchanged.

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
- [x] 4. Dockerfile generation with sanity probes and a recipe-hash build cache
- [x] 5. Test execution by exact name, libtest text and JSON parsing, flaky detection
- [x] 6. Task bundle export, `verify` command and batch recipes with two-crate e2e

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
