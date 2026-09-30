#[test]
fn slow_but_fine() {
    std::thread::sleep(std::time::Duration::from_millis(50));
}
