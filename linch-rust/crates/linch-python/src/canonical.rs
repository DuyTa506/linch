//! Native JSON encoding for plain JSON trees.
//!
//! `canonical_dumps` produces exactly the bytes of `json.dumps(value,
//! sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))`
//! and `dumps` those of `json.dumps(value, allow_nan=False)`, for plain trees: exact `dict` (with exact `str` keys), `list`, `str`, `int`, `float`,
//! `bool` and `None`. Anything else — subclasses, tuples, non-finite floats,
//! lone surrogates, cycles, or nesting at `max_depth` — returns `None` so the
//! Python caller takes its existing path and raises its own errors.

use std::fmt::Write as _;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString};

/// Native recursion bound regardless of `max_depth`, to keep stack use small.
const NATIVE_DEPTH: usize = 200;

/// Output format: the canonical form or `json.dumps` defaults.
#[derive(Clone, Copy)]
struct Format {
    sort_keys: bool,
    ensure_ascii: bool,
    item_sep: &'static str,
    key_sep: &'static str,
}

const CANONICAL: Format = Format {
    sort_keys: true,
    ensure_ascii: false,
    item_sep: ",",
    key_sep: ":",
};

const DEFAULT: Format = Format {
    sort_keys: false,
    ensure_ascii: true,
    item_sep: ", ",
    key_sep: ": ",
};

struct Encoder {
    out: String,
    max_depth: usize,
    ancestors: Vec<usize>,
    format: Format,
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
                self.out.push_str(self.format.item_sep);
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
        if self.format.sort_keys {
            // Valid UTF-8 byte order is code point order, which is how Python
            // compares `str` keys.
            items.sort_by(|left, right| left.0.cmp(&right.0));
        }
        self.out.push('{');
        for (index, (key, item)) in items.iter().enumerate() {
            if index > 0 {
                self.out.push_str(self.format.item_sep);
            }
            self.string(key);
            self.out.push_str(self.format.key_sep);
            if !self.value(item)? {
                return Ok(false);
            }
        }
        self.out.push('}');
        Ok(true)
    }

    /// `json`'s string escaping: quote, backslash, the five short control
    /// escapes and `\u00XX` (lowercase) for other C0 controls; with
    /// `ensure_ascii`, every character outside `' '..='~'` as `\uXXXX`
    /// (astral characters as a surrogate pair).
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
                ch if (ch as u32) < 0x20 || (self.format.ensure_ascii && ch > '~') => {
                    let mut units = [0u16; 2];
                    for unit in ch.encode_utf16(&mut units) {
                        write!(self.out, "\\u{unit:04x}").expect("write to String");
                    }
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
    encode(value, max_depth, CANONICAL)
}

/// Encode a plain JSON tree as `json.dumps(value, allow_nan=False)` does, or
/// return `None` to defer to Python.
#[pyfunction]
pub fn dumps(value: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    encode(value, usize::MAX, DEFAULT)
}

fn encode(value: &Bound<'_, PyAny>, max_depth: usize, format: Format) -> PyResult<Option<String>> {
    let mut encoder = Encoder {
        out: String::new(),
        max_depth,
        ancestors: Vec::new(),
        format,
    };
    Ok(encoder.value(value)?.then_some(encoder.out))
}
