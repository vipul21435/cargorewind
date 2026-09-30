#[test]
fn shared_name() {
    assert_eq!(libtest_zoo::add(2, 3), 5);
}

#[test]
fn it_passes() {}

#[test]
fn it_fails() {
    panic!("integration failure");
}
