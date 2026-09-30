//! Tests that produce every libtest line cargorewind parses: passes, failures,
//! ignored tests with and without a reason, should_panic tests, noisy tests, a panic
//! in a spawned thread, a test returning Err, and doctests of every kind.

/// Adds two numbers.
///
/// ```
/// assert_eq!(libtest_zoo::add(2, 2), 4);
/// ```
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

/// Always panics.
///
/// ```should_panic
/// libtest_zoo::boom();
/// ```
///
/// ```compile_fail
/// let x: i32 = "not a number";
/// ```
///
/// ```no_run
/// loop {}
/// ```
///
/// ```ignore
/// this is not rust
/// ```
///
/// ```
/// assert_eq!(libtest_zoo::add(1, 1), 3);
/// ```
pub fn boom() {
    panic!("boom")
}

#[test]
fn shared_name() {
    assert_eq!(add(1, 1), 2);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn passes() {
        assert_eq!(add(1, 2), 3);
    }

    #[test]
    fn fails() {
        assert_eq!(add(1, 2), 4, "wrong sum");
    }

    #[test]
    #[ignore]
    fn ignored_plain() {}

    #[test]
    #[ignore = "needs a network"]
    fn ignored_with_reason() {}

    #[test]
    #[should_panic(expected = "boom")]
    fn panics_as_expected() {
        boom();
    }

    #[test]
    #[should_panic]
    fn should_panic_but_does_not() {}

    #[test]
    fn noisy() {
        println!("hello from noisy");
        eprintln!("noisy writes to stderr too");
    }

    #[test]
    fn thread_panics() {
        let handle = std::thread::spawn(|| panic!("inner thread panic"));
        assert!(handle.join().is_err());
    }

    #[test]
    fn result_err() -> Result<(), String> {
        Err("an error value".to_string())
    }
}
