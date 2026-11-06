//! Iterating an exact `dict` while Python code may run between items.

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Call `f` on each item of `dict`, stopping at the first `Some` it returns.
///
/// Raises the `RuntimeError` CPython raises when `f` changes the dict.
/// PyO3's iterator panics there instead, and the panic reaches Python as
/// `PanicException`, which `except Exception` does not catch. The size is
/// checked after every item, so the iterator's own size panic never fires,
/// and iteration stops after one item past the original length, before its
/// keys-changed panic would.
pub fn for_each_item<'py, T>(
    dict: &Bound<'py, PyDict>,
    mut f: impl FnMut(Bound<'py, PyAny>, Bound<'py, PyAny>) -> PyResult<Option<T>>,
) -> PyResult<Option<T>> {
    let len = dict.len();
    let mut items = dict.iter();
    for _ in 0..len {
        let Some((key, value)) = items.next() else {
            return Ok(None);
        };
        if let Some(found) = f(key, value)? {
            return Ok(Some(found));
        }
        if dict.len() != len {
            return Err(PyRuntimeError::new_err(
                "dictionary changed size during iteration",
            ));
        }
    }
    if items.next().is_some() {
        return Err(PyRuntimeError::new_err(
            "dictionary keys changed during iteration",
        ));
    }
    Ok(None)
}
