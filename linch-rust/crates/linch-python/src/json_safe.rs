//! Native `linch.run_store._json_safe`.
//!
//! Mirrors the Python walker exactly: the dispatch order is load-bearing
//! (`str`/`int` subclasses such as `StrEnum` pass through before the `Enum`
//! check), `seen` is the ancestor path rather than a visited set (shared
//! siblings are not recursion), and only container values consume depth.
//! Exact `dict`/`list`/`tuple` take fast paths; every other type goes through
//! the same `isinstance` chain as Python.

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{
    PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PyMemoryView, PySet,
    PyString, PyTuple, PyType,
};

struct Refs {
    decimal: Py<PyType>,
    enum_: Py<PyType>,
    date: Py<PyType>,
    path: Py<PyType>,
    uuid: Py<PyType>,
    mapping: Py<PyType>,
    sequence: Py<PyType>,
    is_dataclass: Py<PyAny>,
    fields: Py<PyAny>,
    b64encode: Py<PyAny>,
}

static REFS: PyOnceLock<Refs> = PyOnceLock::new();
// Imported on first use, never at module init: `linch.run_store` imports this
// extension, so resolving it eagerly would be circular.
static CANONICAL_JSON: PyOnceLock<Py<PyAny>> = PyOnceLock::new();

fn attr<'py>(py: Python<'py>, module: &str, name: &str) -> PyResult<Bound<'py, PyAny>> {
    py.import(module)?.getattr(name)
}

fn class(py: Python<'_>, module: &str, name: &str) -> PyResult<Py<PyType>> {
    Ok(attr(py, module, name)?.cast_into::<PyType>()?.unbind())
}

fn refs(py: Python<'_>) -> PyResult<&'static Refs> {
    REFS.get_or_try_init(py, || {
        Ok(Refs {
            decimal: class(py, "decimal", "Decimal")?,
            enum_: class(py, "enum", "Enum")?,
            date: class(py, "datetime", "date")?,
            path: class(py, "pathlib", "Path")?,
            uuid: class(py, "uuid", "UUID")?,
            mapping: class(py, "collections.abc", "Mapping")?,
            sequence: class(py, "collections.abc", "Sequence")?,
            is_dataclass: attr(py, "dataclasses", "is_dataclass")?.unbind(),
            fields: attr(py, "dataclasses", "fields")?.unbind(),
            b64encode: attr(py, "base64", "b64encode")?.unbind(),
        })
    })
}

#[derive(Clone, Copy)]
enum Kind {
    Dataclass,
    Mapping,
    Set,
    Sequence,
}

struct Walker<'py> {
    py: Python<'py>,
    refs: &'static Refs,
    strict: bool,
    max_depth: usize,
    ancestors: Vec<usize>,
}

impl<'py> Walker<'py> {
    fn is(&self, value: &Bound<'py, PyAny>, ty: &Py<PyType>) -> PyResult<bool> {
        value.is_instance(ty.bind(self.py).as_any())
    }

    fn walk(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if value.is_none() || value.is_instance_of::<PyString>() || value.is_instance_of::<PyInt>()
        {
            return Ok(value.clone());
        }
        if value.is_instance_of::<PyFloat>() {
            if value.cast::<PyFloat>()?.value().is_finite() {
                return Ok(value.clone());
            }
            return Ok(value.str()?.into_any());
        }
        if value.is_exact_instance_of::<PyDict>() {
            return self.nested(value, Kind::Mapping);
        }
        if value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>() {
            return self.nested(value, Kind::Sequence);
        }
        self.walk_other(value)
    }

    fn walk_other(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let refs = self.refs;
        if self.is(value, &refs.decimal)? {
            return Ok(value.str()?.into_any());
        }
        if self.is(value, &refs.enum_)? {
            return self.walk(&value.getattr("value")?);
        }
        if self.is(value, &refs.date)? {
            return value.call_method0("isoformat");
        }
        if self.is(value, &refs.path)? || self.is(value, &refs.uuid)? {
            return Ok(value.str()?.into_any());
        }
        if value.is_instance_of::<PyBytes>()
            || value.is_instance_of::<PyByteArray>()
            || value.is_instance_of::<PyMemoryView>()
        {
            let raw = self.py.get_type::<PyBytes>().call1((value,))?;
            let encoded = refs
                .b64encode
                .bind(self.py)
                .call1((raw,))?
                .call_method1("decode", ("ascii",))?;
            let out = PyDict::new(self.py);
            out.set_item("$bytes_base64", encoded)?;
            return Ok(out.into_any());
        }
        let kind = if refs
            .is_dataclass
            .bind(self.py)
            .call1((value,))?
            .is_truthy()?
            && !value.is_instance_of::<PyType>()
        {
            Kind::Dataclass
        } else if self.is(value, &refs.mapping)? {
            Kind::Mapping
        } else if value.is_instance_of::<PySet>() || value.is_instance_of::<PyFrozenSet>() {
            Kind::Set
        } else if self.is(value, &refs.sequence)? {
            Kind::Sequence
        } else {
            if self.strict {
                return Err(PyTypeError::new_err(format!(
                    "run contract value of type {} is not deterministically serializable",
                    value.get_type().name()?
                )));
            }
            return Ok(value.str()?.into_any());
        };
        self.nested(value, kind)
    }

    fn nested(&mut self, value: &Bound<'py, PyAny>, kind: Kind) -> PyResult<Bound<'py, PyAny>> {
        let id = value.as_ptr() as usize;
        if self.ancestors.contains(&id) {
            if self.strict {
                return Err(PyTypeError::new_err(
                    "run contract contains a recursive value",
                ));
            }
            return Ok(PyString::new(self.py, "<recursion>").into_any());
        }
        // The ancestor path length is Python's `depth`: both grow only on
        // container values, and a repeated id is caught above.
        if self.ancestors.len() >= self.max_depth {
            if self.strict {
                return Err(PyTypeError::new_err(format!(
                    "run contract exceeds maximum nesting depth {}",
                    self.max_depth
                )));
            }
            return Ok(PyString::new(self.py, "<max-depth>").into_any());
        }
        self.ancestors.push(id);
        let out = match kind {
            Kind::Dataclass => self.dataclass(value),
            Kind::Mapping => self.mapping(value),
            Kind::Set => self.set(value),
            Kind::Sequence => self.sequence(value),
        };
        self.ancestors.pop();
        out
    }

    fn dataclass(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let out = PyDict::new(self.py);
        for field in self.refs.fields.bind(self.py).call1((value,))?.try_iter()? {
            let name = field?.getattr("name")?.cast_into::<PyString>()?;
            let item = value.getattr(&name)?;
            out.set_item(name, self.walk(&item)?)?;
        }
        Ok(out.into_any())
    }

    fn mapping(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let out = PyDict::new(self.py);
        if let Ok(dict) = value.cast_exact::<PyDict>()
            && dict
                .iter()
                .all(|(key, _)| key.is_exact_instance_of::<PyString>())
        {
            // Exact `str` keys are already their own normalized names and are
            // unique, so neither collision branch can fire.
            for (key, item) in dict.iter() {
                out.set_item(key, self.walk(&item)?)?;
            }
            return Ok(out.into_any());
        }
        let reserved = PySet::empty(self.py)?;
        for key in value.try_iter()? {
            let key = key?;
            if key.is_instance_of::<PyString>() {
                reserved.add(key)?;
            }
        }
        for pair in value.call_method0("items")?.try_iter()? {
            let (key, item): (Bound<'py, PyAny>, Bound<'py, PyAny>) = pair?.extract()?;
            let is_str = key.is_instance_of::<PyString>();
            if self.strict && !is_str {
                return Err(PyTypeError::new_err(format!(
                    "run contract mapping key must be str, got {}",
                    key.get_type().name()?
                )));
            }
            let mut name = key.str()?.into_any();
            if out.contains(&name)? || (!is_str && reserved.contains(&name)?) {
                if self.strict {
                    return Err(PyTypeError::new_err(format!(
                        "run contract mapping keys collide after normalization: {}",
                        name.repr()?
                    )));
                }
                let template = PyString::new(self.py, "{}~{}");
                let base = template.call_method1("format", (&name, key.get_type().name()?))?;
                name = base.clone();
                let mut discriminator = 2_u64;
                while out.contains(&name)? || reserved.contains(&name)? {
                    name = template.call_method1("format", (&base, discriminator))?;
                    discriminator += 1;
                }
            }
            out.set_item(name, self.walk(&item)?)?;
        }
        Ok(out.into_any())
    }

    fn set(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let items = self.sequence(value)?;
        let key = CANONICAL_JSON
            .get_or_try_init(self.py, || {
                Ok::<_, PyErr>(attr(self.py, "linch.run_store", "canonical_json")?.unbind())
            })?
            .bind(self.py);
        let kwargs = PyDict::new(self.py);
        kwargs.set_item("key", key)?;
        items.call_method("sort", (), Some(&kwargs))?;
        Ok(items)
    }

    fn sequence(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let out = PyList::empty(self.py);
        for item in value.try_iter()? {
            out.append(self.walk(&item?)?)?;
        }
        Ok(out.into_any())
    }
}

/// Return an isolated JSON-safe representation of arbitrary runtime data.
///
/// Native implementation of `linch.run_store._json_safe`; `max_depth` is
/// passed from Python so the ignorable-event limit stays the single source.
#[pyfunction]
#[pyo3(signature = (value, *, strict, max_depth))]
pub fn json_safe<'py>(
    value: &Bound<'py, PyAny>,
    strict: bool,
    max_depth: usize,
) -> PyResult<Bound<'py, PyAny>> {
    let py = value.py();
    let mut walker = Walker {
        py,
        refs: refs(py)?,
        strict,
        max_depth,
        ancestors: Vec::new(),
    };
    walker.walk(value)
}
