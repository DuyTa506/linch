# linch-native

Native internals for the [linch](https://pypi.org/project/linch/) Python
package: JSON canonicalization, `deepcopy`, run-store value normalization and
journal validation, written in Rust with PyO3.

`pip install linch` installs it. Nothing here is a public API; import from
`linch` instead.

Building from source needs a Rust toolchain:

```sh
pip install ./linch-rust/crates/linch-python
```
