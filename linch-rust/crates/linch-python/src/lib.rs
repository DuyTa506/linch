//! Native internals of the `linch` Python package.
//!
//! The Python package stays the public API; this extension only replaces
//! private, data-only helpers whose behaviour is pinned by the Python test
//! suite. Nothing here is importable as part of `linch.__all__`.

#![forbid(unsafe_code)]

mod canonical;
mod deepcopy;
mod json_safe;

use pyo3::prelude::*;

/// The `_linch_native` extension module.
#[pymodule]
mod _linch_native {
    #[pymodule_export]
    use super::canonical::canonical_dumps;
    #[pymodule_export]
    use super::deepcopy::deepcopy;
    #[pymodule_export]
    use super::json_safe::json_safe;
}
