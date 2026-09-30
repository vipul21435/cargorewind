#!/bin/sh
# Record real libtest output of the zoo crate with one Rust toolchain image.
# Usage: sh record.sh <image> <label>   (writes <label>-*.txt next to this script)
# Runs with --network none as the image's user; the crate has no dependencies.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
image=$1
label=$2
docker run --rm --network none -v "$here/zoo:/zoo:ro" -e CARGO_TERM_COLOR=never \
    --label project=cargorewind "$image" sh -c '
set +e
cp -r /zoo /tmp/zoo && cd /tmp/zoo
export CARGO_TARGET_DIR=/tmp/zoo-target
cargo test --no-run >/dev/null 2>&1
echo "=== text"; cargo test --no-fail-fast 2>&1; echo "=== exit $?"
echo "=== nocapture"; cargo test --no-fail-fast -- --nocapture --test-threads=1 2>&1; echo "=== exit $?"
echo "=== json"; RUSTC_BOOTSTRAP=1 cargo test --no-fail-fast -- -Z unstable-options --format json 2>&1; echo "=== exit $?"
echo "=== exact-lib"; cargo test --lib -- --exact tests::passes 2>&1; echo "=== exit $?"
echo "=== exact-test"; cargo test --test more-checks -- --exact slow_but_fine 2>&1; echo "=== exit $?"
echo "=== exact-bin"; cargo test --bin tool -- --exact bin_test 2>&1; echo "=== exit $?"
echo "=== exact-doc"; cargo test --doc -- --exact "src/lib.rs - add (line 7)" 2>&1; echo "=== exit $?"
echo "=== exact-missing"; cargo test --lib -- --exact tests::no_such_test 2>&1; echo "=== exit $?"
sed -i "s/assert_eq!(add(1, 2), 3);/assert_eq!(add(1, 2), missing_fn());/" src/lib.rs
echo "=== compile-error"; cargo test --no-fail-fast 2>&1; echo "=== exit $?"
' > "$here/$label.txt" 2>&1 || true
