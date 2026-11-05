//! Native canonical JSON encoding.
//!
//! Produces exactly the bytes of `json.dumps(value, sort_keys=True,
//! ensure_ascii=False, allow_nan=False, separators=(",", ":"))` for plain JSON
//! trees: exact `dict` (with exact `str` keys), `list`, `str`, `int`, `float`,
//! `bool` and `None`. Anything else — subclasses, tuples, non-finite floats,
//! lone surrogates, cycles, or nesting at `max_depth` — returns `None` so the
//! Python caller takes its existing path and raises its own errors.

use std::fmt::Write as _;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

/// Native recursion bound regardless of `max_depth`, to keep stack use small.
const NATIVE_DEPTH: usize = 200;

struct Encoder {
    out: String,
    max_depth: usize,
    ancestors: Vec<usize>,
}

impl Encoder {
    /// Returns `Ok(false)` when the value is outside the plain-JSON fast path.
    fn value(&mut self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        if value.is_none() {
            self.out.push_str("null");
        } else if value.is_exact_instance_of::<PyBool>() {
            self.out
                .push_str(if value.is_truthy()? { "true" } else { "false" });
        } else if value.is_exact_instance_of::<PyString>() {
            let Ok(text) = value.cast::<PyString>()?.to_str() else {
                return Ok(false);
            };
            self.string(text);
        } else if value.is_exact_instance_of::<PyInt>() {
            match value.extract::<i64>() {
                Ok(number) => write!(self.out, "{number}").expect("write to String"),
                Err(_) => self.out.push_str(value.str()?.to_str()?),
            }
        } else if value.is_exact_instance_of::<PyFloat>() {
            if !value.cast::<PyFloat>()?.value().is_finite() {
                return Ok(false);
            }
            // `float.__repr__` is what `json` emits; Rust's shortest form differs
            // in exponent spelling (`1e16` vs `1e+16`).
            self.out.push_str(value.repr()?.to_str()?);
        } else if value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyDict>() {
            return self.container(value);
        } else {
            return Ok(false);
        }
        Ok(true)
    }

    fn container(&mut self, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let id = value.as_ptr() as usize;
        let depth = self.ancestors.len();
        if self.ancestors.contains(&id) || depth >= self.max_depth || depth >= NATIVE_DEPTH {
            return Ok(false);
        }
        self.ancestors.push(id);
        let ok = if let Ok(list) = value.cast::<PyList>() {
            self.list(list)?
        } else {
            self.dict(value.cast::<PyDict>()?)?
        };
        self.ancestors.pop();
        Ok(ok)
    }

    fn list(&mut self, list: &Bound<'_, PyList>) -> PyResult<bool> {
        self.out.push('[');
        for (index, item) in list.iter().enumerate() {
            if index > 0 {
                self.out.push(',');
            }
            if !self.value(&item)? {
                return Ok(false);
            }
        }
        self.out.push(']');
        Ok(true)
    }

    fn dict(&mut self, dict: &Bound<'_, PyDict>) -> PyResult<bool> {
        let mut items = Vec::with_capacity(dict.len());
        for (key, item) in dict.iter() {
            if !key.is_exact_instance_of::<PyString>() {
                return Ok(false);
            }
            let Ok(text) = key.cast::<PyString>()?.to_str().map(str::to_owned) else {
                return Ok(false);
            };
            items.push((text, item));
        }
        // Valid UTF-8 byte order is code point order, which is how Python
        // compares `str` keys.
        items.sort_by(|left, right| left.0.cmp(&right.0));
        self.out.push('{');
        for (index, (key, item)) in items.iter().enumerate() {
            if index > 0 {
                self.out.push(',');
            }
            self.string(key);
            self.out.push(':');
            if !self.value(item)? {
                return Ok(false);
            }
        }
        self.out.push('}');
        Ok(true)
    }

    /// `json`'s `ensure_ascii=False` escaping: quote, backslash, the five
    /// short control escapes, and `\u00XX` (lowercase) for other C0 controls.
    fn string(&mut self, text: &str) {
        self.out.push('"');
        for ch in text.chars() {
            match ch {
                '"' => self.out.push_str("\\\""),
                '\\' => self.out.push_str("\\\\"),
                '\n' => self.out.push_str("\\n"),
                '\r' => self.out.push_str("\\r"),
                '\t' => self.out.push_str("\\t"),
                '\u{08}' => self.out.push_str("\\b"),
                '\u{0c}' => self.out.push_str("\\f"),
                ch if (ch as u32) < 0x20 => {
                    write!(self.out, "\\u{:04x}", ch as u32).expect("write to String");
                }
                ch => self.out.push(ch),
            }
        }
        self.out.push('"');
    }
}

/// Encode a plain JSON tree canonically, or return `None` to defer to Python.
///
/// Containers at nesting `max_depth` (root is depth 0) are not encoded.
#[pyfunction]
#[pyo3(signature = (value, *, max_depth))]
pub fn canonical_dumps(value: &Bound<'_, PyAny>, max_depth: usize) -> PyResult<Option<String>> {
    let mut encoder = Encoder {
        out: String::new(),
        max_depth,
        ancestors: Vec::new(),
    };
    Ok(encoder.value(value)?.then_some(encoder.out))
}
