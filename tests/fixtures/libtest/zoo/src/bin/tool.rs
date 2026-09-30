fn main() {
    println!("{}", libtest_zoo::add(1, 2));
}

#[test]
fn bin_test() {
    assert_eq!(libtest_zoo::add(0, 0), 0);
}
