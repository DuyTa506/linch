//! Native validation of model-call journal records.
//!
//! Mirrors `linch.model_calls`' journal walker exactly: scalars must be
//! exactly `None`, `str`, `bool`, `int` or a finite `float` (subclasses such
//! as `IntEnum` are rejected), containers are `dict`/`list` instances nested
//! at most [`MAX_DEPTH`] levels below the root, `seen` is the ancestor path
//! (shared siblings are not cycles), and every key of an object is checked
//! before any of its children. Exact `dict`/`list` are walked natively;
//! subclasses go through their own `items()`/`__iter__` as Python does.

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

use crate::dict_iter::for_each_item;

/// Deepest container depth a journal record may use; the root is depth 0.
const MAX_DEPTH: usize = 64;

const BOUNDED: &str = "journal record must be bounded, finite JSON without cycles";
const KEYS: &str = "journal object keys must be strings";

struct Checker {
    ancestors: Vec<usize>,
}

impl Checker {
    /// Return the first violation in visit order, or `None` when `value` is valid.
    fn visit(&mut self, value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Option<&'static str>> {
        if value.is_none()
            || value.is_exact_instance_of::<PyString>()
            || value.is_exact_instance_of::<PyBool>()
            || value.is_exact_instance_of::<PyInt>()
        {
            return Ok(None);
        }
        if value.is_exact_instance_of::<PyFloat>() && value.cast::<PyFloat>()?.value().is_finite() {
            return Ok(None);
        }
        let is_dict = value.is_instance_of::<PyDict>();
        let identity = value.as_ptr() as usize;
        if !(is_dict || value.is_instance_of::<PyList>())
            || depth > MAX_DEPTH
            || self.ancestors.contains(&identity)
        {
            return Ok(Some(BOUNDED));
        }
        if let Ok(dict) = value.cast_exact::<PyDict>() {
            if dict
                .iter()
                .any(|(key, _)| !key.is_instance_of::<PyString>())
            {
                return Ok(Some(KEYS));
            }
        } else if is_dict {
            for key in value.try_iter()? {
                if !key?.is_instance_of::<PyString>() {
                    return Ok(Some(KEYS));
                }
            }
        }
        self.ancestors.push(identity);
        let result = self.children(value, is_dict, depth + 1);
        self.ancestors.pop();
        result
    }

    fn children(
        &mut self,
        value: &Bound<'_, PyAny>,
        is_dict: bool,
        depth: usize,
    ) -> PyResult<Option<&'static str>> {
        if let Ok(dict) = value.cast_exact::<PyDict>() {
            if let Some(error) = for_each_item(dict, |_, child| self.visit(&child, depth))? {
                return Ok(Some(error));
            }
        } else if let Ok(list) = value.cast_exact::<PyList>() {
            for child in list.iter() {
                if let Some(error) = self.visit(&child, depth)? {
                    return Ok(Some(error));
                }
            }
        } else if is_dict {
            for item in value.call_method0("items")?.try_iter()? {
                let (_, child): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item?.extract()?;
                if let Some(error) = self.visit(&child, depth)? {
                    return Ok(Some(error));
                }
            }
        } else {
            for child in value.try_iter()? {
                if let Some(error) = self.visit(&child?, depth)? {
                    return Ok(Some(error));
                }
            }
        }
        Ok(None)
    }
}

/// Validate a model-call journal record without copying it.
///
/// Returns `None` when `value` is bounded, finite JSON with string keys and
/// no cycles, otherwise the message of the first violation in the order the
/// Python walker reports it. Exceptions raised by container subclasses
/// propagate unchanged.
#[pyfunction]
pub fn check_journal_json(value: &Bound<'_, PyAny>) -> PyResult<Option<&'static str>> {
    Checker {
        ancestors: Vec::new(),
    }
    .visit(value, 0)
}
