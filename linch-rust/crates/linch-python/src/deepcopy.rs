//! Native `copy.deepcopy`.
//!
//! A port of CPython's `copy.deepcopy` / `copy._reconstruct` (3.11-3.14; 3.14
//! moved the atomic types into a set and checks them before the memo) that shares the caller's memo dict, including the keep-alive
//! list stored under `id(memo)`. Exact `list`/`dict`/`tuple`, the atomic types
//! and the `__reduce_ex__(4)` reconstruction run natively. Every other case
//! (`__deepcopy__` hooks, `copyreg.dispatch_table`, the remaining dispatch
//! entries, type subclasses, and nesting deeper than [`NATIVE_DEPTH`]) is
//! handed to the stdlib with the same memo, so aliasing stays exact across
//! the boundary and the stdlib remains the reference behaviour.

use std::collections::HashMap;

use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};

use crate::dict_iter::for_each_item;

/// Native recursion depth before delegating to the stdlib, which then owns
/// any `RecursionError`. Keeps Rust stack use bounded on worker threads.
const NATIVE_DEPTH: usize = 200;

struct Refs {
    deepcopy: Py<PyAny>,
    reconstruct: Py<PyAny>,
    slotnames: Py<PyAny>,
    newobj: Py<PyAny>,
    object: Py<PyType>,
    dispatch_table: Py<PyDict>,
    atomic: Vec<Py<PyType>>,
    /// 3.14 returns an atomic before reading the memo; 3.11-3.13 read the
    /// memo first, so a memo entry for an atomic wins there.
    atomic_before_memo: bool,
    other_dispatch: Vec<Py<PyType>>,
}

static REFS: PyOnceLock<Refs> = PyOnceLock::new();

fn refs(py: Python<'_>) -> PyResult<&'static Refs> {
    REFS.get_or_try_init(py, || {
        let copy = py.import("copy")?;
        let native = [
            py.get_type::<PyList>(),
            py.get_type::<PyDict>(),
            py.get_type::<PyTuple>(),
        ];
        let (mut atomic, mut other_dispatch) = (Vec::new(), Vec::new());
        // 3.14 keeps the atomic types in a set; older versions map them to
        // `_deepcopy_atomic` in the dispatch table.
        let atomic_before_memo = copy.hasattr("_atomic_types")?;
        let atomic_copier = if atomic_before_memo {
            for cls in copy.getattr("_atomic_types")?.try_iter()? {
                atomic.push(cls?.cast_into::<PyType>()?.unbind());
            }
            None
        } else {
            Some(copy.getattr("_deepcopy_atomic")?)
        };
        let dispatch = copy.getattr("_deepcopy_dispatch")?.cast_into::<PyDict>()?;
        for (cls, copier) in dispatch.iter() {
            let cls = cls.cast_into::<PyType>()?;
            if atomic_copier.as_ref().is_some_and(|f| copier.is(f)) {
                atomic.push(cls.unbind());
            } else if !native.iter().any(|ty| ty.is(&cls)) {
                other_dispatch.push(cls.unbind());
            }
        }
        Ok(Refs {
            deepcopy: copy.getattr("deepcopy")?.unbind(),
            reconstruct: copy.getattr("_reconstruct")?.unbind(),
            newobj: py.import("copyreg")?.getattr("__newobj__")?.unbind(),
            slotnames: py.import("copyreg")?.getattr("_slotnames")?.unbind(),
            object: py.get_type::<PyAny>().unbind(),
            dispatch_table: py
                .import("copyreg")?
                .getattr("dispatch_table")?
                .cast_into::<PyDict>()?
                .unbind(),
            atomic,
            atomic_before_memo,
            other_dispatch,
        })
    })
}

/// How one type is copied, decided once per `deepcopy` call and again if
/// what it was read from changes (see [`Sources`]).
#[derive(Clone, Copy)]
enum Plan {
    Atomic,
    List,
    Dict,
    Tuple,
    Delegate,
    /// Plain slotted class: slots are read and set directly, which is what
    /// the default `__reduce_ex__(4)` round trip does, minus the protocol.
    Slots,
    /// `__reduce_ex__(4)`; `deepcopy_absent` is true when no instance of the
    /// type can expose `__deepcopy__` (see [`Copier::lookup_is_static`]).
    Reduce {
        deepcopy_absent: bool,
    },
}

/// The mappings a cached answer was read from, each with its size then: the
/// namespace of every MRO base below `object`, plus `copyreg.dispatch_table`
/// for a slots plan.
///
/// Code run mid-copy (a sibling's hook or reducer) can add `__deepcopy__`,
/// `__setstate__`, a pickle hook or a `copyreg` reducer, and the stdlib looks
/// these up on every object. Each addition grows one of the mappings, so a
/// cached answer is reused only while all of them keep their size. A change
/// that keeps every size (deleting one entry and adding another) or a
/// `__bases__` assignment is not seen.
type Sources<'py> = Vec<(Bound<'py, PyAny>, usize)>;

fn unchanged(sources: &Sources<'_>) -> PyResult<bool> {
    for (mapping, size) in sources {
        if mapping.len()? != *size {
            return Ok(false);
        }
    }
    Ok(true)
}

struct Copier<'py> {
    py: Python<'py>,
    refs: &'static Refs,
    memo: Bound<'py, PyDict>,
    depth: usize,
    plans: HashMap<usize, (Plan, Sources<'py>)>,
    setstate_absent: HashMap<usize, (bool, Sources<'py>)>,
    slots: HashMap<usize, Vec<Py<PyString>>>,
}

impl<'py> Copier<'py> {
    fn id(&self, value: &Bound<'py, PyAny>) -> usize {
        value.as_ptr() as usize
    }

    fn delegate(&self, x: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        self.refs.deepcopy.bind(self.py).call1((x, &self.memo))
    }

    fn is_atomic(&self, cls: &Bound<'py, PyType>) -> bool {
        self.refs.atomic.iter().any(|ty| ty.bind(self.py).is(cls))
    }

    fn has_reductor(&self, cls: &Bound<'py, PyType>) -> PyResult<bool> {
        match self.refs.dispatch_table.bind(self.py).get_item(cls)? {
            Some(reductor) => reductor.is_truthy(),
            None => Ok(false),
        }
    }

    fn keep_alive(&self, x: &Bound<'py, PyAny>) -> PyResult<()> {
        let key = self.id(self.memo.as_any());
        match self.memo.get_item(key)? {
            Some(alive) => alive.call_method1("append", (x,)).map(drop),
            None => self.memo.set_item(key, PyList::new(self.py, [x])?),
        }
    }

    fn deep(&mut self, x: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        if self.depth >= NATIVE_DEPTH {
            return self.delegate(x);
        }
        self.depth += 1;
        let out = self.deep_inner(x);
        self.depth -= 1;
        out
    }

    /// Whether attribute lookup on instances of `cls` is exactly a lookup in
    /// the type's MRO: no instance `__dict__`, default `__getattribute__`, and
    /// no `__getattr__`. Only then can a per-type answer stand for an instance.
    fn lookup_is_static(&self, cls: &Bound<'py, PyType>) -> PyResult<bool> {
        if cls.getattr("__dictoffset__")?.extract::<isize>()? != 0 {
            return Ok(false);
        }
        let object = self.refs.object.bind(self.py);
        for base in cls.getattr("__mro__")?.try_iter()? {
            let base = base?;
            let namespace = base.getattr("__dict__")?;
            if namespace.contains("__getattr__")?
                || (!base.is(object) && namespace.contains("__getattribute__")?)
            {
                return Ok(false);
            }
        }
        Ok(true)
    }

    fn namespaces(&self, cls: &Bound<'py, PyType>) -> PyResult<Sources<'py>> {
        let object = self.refs.object.bind(self.py);
        let mut namespaces = Vec::new();
        for base in cls.getattr("__mro__")?.try_iter()? {
            let base = base?;
            if !base.is(object) {
                let namespace = base.getattr("__dict__")?;
                let size = namespace.len()?;
                namespaces.push((namespace, size));
            }
        }
        Ok(namespaces)
    }

    fn in_mro(&self, cls: &Bound<'py, PyType>, name: &str) -> PyResult<bool> {
        for base in cls.getattr("__mro__")?.try_iter()? {
            if base?.getattr("__dict__")?.contains(name)? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    /// No instance `__dict__`, no copy/pickle hook below `object`, no
    /// `copyreg` reducer: the default reduce would be `(__newobj__, (cls,),
    /// (None, slots))` with no `__setstate__`, so it can be done inline.
    fn is_plain_slotted(&self, cls: &Bound<'py, PyType>) -> PyResult<bool> {
        const HOOKS: [&str; 8] = [
            "__deepcopy__",
            "__reduce_ex__",
            "__reduce__",
            "__getstate__",
            "__setstate__",
            "__getnewargs__",
            "__getnewargs_ex__",
            "__new__",
        ];
        if !self.lookup_is_static(cls)? || self.has_reductor(cls)? {
            return Ok(false);
        }
        let object = self.refs.object.bind(self.py);
        for base in cls.getattr("__mro__")?.try_iter()? {
            let base = base?;
            if base.is(object) {
                continue;
            }
            // Every base must be a `class` statement with `__slots__`: C types
            // (heap or static) may hold state outside slots, which the stdlib
            // refuses to copy ("cannot pickle").
            let namespace = base.getattr("__dict__")?;
            if !namespace.contains("__slots__")? {
                return Ok(false);
            }
            for hook in HOOKS {
                if namespace.contains(hook)? {
                    return Ok(false);
                }
            }
        }
        Ok(true)
    }

    fn plan(&mut self, cls: &Bound<'py, PyType>) -> PyResult<Plan> {
        let key = cls.as_ptr() as usize;
        if let Some((plan, sources)) = self.plans.get(&key)
            && unchanged(sources)?
        {
            return Ok(*plan);
        }
        let refs = self.refs;
        let is_any = |types: &[Py<PyType>]| types.iter().any(|ty| ty.bind(self.py).is(cls));
        let plan = if self.is_atomic(cls) {
            Plan::Atomic
        } else if cls.is(self.py.get_type::<PyList>()) {
            Plan::List
        } else if cls.is(self.py.get_type::<PyDict>()) {
            Plan::Dict
        } else if cls.is(self.py.get_type::<PyTuple>()) {
            Plan::Tuple
        } else if is_any(&refs.other_dispatch) || cls.is_subclass_of::<PyType>()? {
            Plan::Delegate
        } else if self.is_plain_slotted(cls)? {
            let names = self.refs.slotnames.bind(self.py).call1((cls,))?;
            let names = names
                .try_iter()?
                .map(|name| Ok(name?.cast_into::<PyString>()?.unbind()))
                .collect::<PyResult<Vec<_>>>()?;
            self.slots.insert(key, names);
            Plan::Slots
        } else {
            Plan::Reduce {
                deepcopy_absent: self.lookup_is_static(cls)?
                    && !self.in_mro(cls, "__deepcopy__")?,
            }
        };
        // Read after `_slotnames`, which may store `__slotnames__` on `cls`.
        let mut sources = match plan {
            Plan::Slots | Plan::Reduce { .. } => self.namespaces(cls)?,
            _ => Vec::new(),
        };
        // The reduce arm reads `dispatch_table` on every object instead.
        if let Plan::Slots = plan {
            let table = refs.dispatch_table.bind(self.py);
            sources.push((table.as_any().clone(), table.len()));
        }
        self.plans.insert(key, (plan, sources));
        Ok(plan)
    }

    fn setstate_absent(&mut self, y: &Bound<'py, PyAny>) -> PyResult<bool> {
        let cls = y.get_type();
        let key = cls.as_ptr() as usize;
        if let Some((absent, sources)) = self.setstate_absent.get(&key)
            && unchanged(sources)?
        {
            return Ok(*absent);
        }
        let absent = self.lookup_is_static(&cls)? && !self.in_mro(&cls, "__setstate__")?;
        let sources = self.namespaces(&cls)?;
        self.setstate_absent.insert(key, (absent, sources));
        Ok(absent)
    }

    fn deep_inner(&mut self, x: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let d = self.id(x);
        if let Some(y) = self.memo.get_item(d)? {
            if self.refs.atomic_before_memo && self.is_atomic(&x.get_type()) {
                return Ok(x.clone());
            }
            return Ok(y);
        }
        let cls = x.get_type();
        let y = match self.plan(&cls)? {
            Plan::Atomic => return Ok(x.clone()),
            Plan::List => self.list(x, d)?,
            Plan::Dict => self.dict(x, d)?,
            Plan::Tuple => self.tuple(x, d)?,
            Plan::Delegate => return self.delegate(x),
            Plan::Slots => self.slotted(x, &cls, d)?,
            Plan::Reduce { deepcopy_absent } => {
                // `dispatch_table` is mutable, so it is read on every object.
                if (!deepcopy_absent && x.hasattr("__deepcopy__")?) || self.has_reductor(&cls)? {
                    return self.delegate(x);
                }
                let Some(reductor) = x.getattr_opt("__reduce_ex__")? else {
                    return self.delegate(x);
                };
                let rv = reductor.call1((4,))?;
                if rv.is_instance_of::<PyString>() {
                    x.clone()
                } else {
                    self.reconstruct(x, &rv)?
                }
            }
        };
        if !y.is(x) {
            self.memo.set_item(d, &y)?;
            self.keep_alive(x)?;
        }
        Ok(y)
    }

    fn slotted(
        &mut self,
        x: &Bound<'py, PyAny>,
        cls: &Bound<'py, PyType>,
        d: usize,
    ) -> PyResult<Bound<'py, PyAny>> {
        let y = cls.call_method1("__new__", (cls,))?;
        self.memo.set_item(d, &y)?;
        let key = cls.as_ptr() as usize;
        let names: Vec<Bound<'py, PyString>> = self.slots[&key]
            .iter()
            .map(|name| name.bind(self.py).clone())
            .collect();
        // Copy every value before setting any, as the stdlib copies the whole
        // state first; unset slots are skipped like `object.__getstate__`.
        let mut values = Vec::with_capacity(names.len());
        for name in names {
            if let Some(value) = x.getattr_opt(&name)? {
                values.push((name, self.deep(&value)?));
            }
        }
        for (name, value) in values {
            y.setattr(name, value)?;
        }
        Ok(y)
    }

    fn list(&mut self, x: &Bound<'py, PyAny>, d: usize) -> PyResult<Bound<'py, PyAny>> {
        let y = PyList::empty(self.py);
        self.memo.set_item(d, &y)?;
        for item in x.try_iter()? {
            y.append(self.deep(&item?)?)?;
        }
        Ok(y.into_any())
    }

    fn dict(&mut self, x: &Bound<'py, PyAny>, d: usize) -> PyResult<Bound<'py, PyAny>> {
        let y = PyDict::new(self.py);
        self.memo.set_item(d, &y)?;
        for_each_item(x.cast::<PyDict>()?, |key, value| {
            y.set_item(self.deep(&key)?, self.deep(&value)?)?;
            Ok(None::<()>)
        })?;
        Ok(y.into_any())
    }

    fn tuple(&mut self, x: &Bound<'py, PyAny>, d: usize) -> PyResult<Bound<'py, PyAny>> {
        let items = x.cast::<PyTuple>()?;
        let mut copied = Vec::with_capacity(items.len());
        for item in items.iter() {
            copied.push(self.deep(&item)?);
        }
        // A recursive structure may have memoized this tuple's copy meanwhile.
        if let Some(y) = self.memo.get_item(d)? {
            return Ok(y);
        }
        if copied
            .iter()
            .zip(items.iter())
            .all(|(new, old)| new.is(&old))
        {
            return Ok(x.clone());
        }
        Ok(PyTuple::new(self.py, copied)?.into_any())
    }

    fn reconstruct(
        &mut self,
        x: &Bound<'py, PyAny>,
        rv: &Bound<'py, PyAny>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let parts = match rv.cast::<PyTuple>() {
            Ok(parts) if (2..=5).contains(&parts.len()) => parts,
            // Unusual shapes keep the stdlib's exact errors and behaviour.
            _ => {
                let mut args = vec![x.clone(), self.memo.clone().into_any()];
                for item in rv.try_iter()? {
                    args.push(item?);
                }
                return self
                    .refs
                    .reconstruct
                    .bind(self.py)
                    .call1(PyTuple::new(self.py, args)?);
            }
        };
        let none = self.py.None().into_bound(self.py);
        let part = |i: usize| parts.get_item(i).unwrap_or_else(|_| none.clone());
        let (func, args) = (part(0), part(1));
        let (state, listiter, dictiter) = (part(2), part(3), part(4));

        let mut copied_args = Vec::new();
        if args.is_truthy()? {
            for arg in args.try_iter()? {
                copied_args.push(self.deep(&arg?)?);
            }
        }
        // `copyreg.__newobj__(cls, *args)` is `cls.__new__(cls, *args)`; calling
        // it directly skips one Python frame per object.
        let y = match copied_args.first() {
            Some(cls) if func.is(self.refs.newobj.bind(self.py)) => cls
                .getattr("__new__")?
                .call1(PyTuple::new(self.py, &copied_args)?)?,
            _ => func.call1(PyTuple::new(self.py, copied_args)?)?,
        };
        self.memo.set_item(self.id(x), &y)?;

        if !state.is_none() {
            let state = self.deep(&state)?;
            if !self.setstate_absent(&y)? && y.hasattr("__setstate__")? {
                y.call_method1("__setstate__", (state,))?;
            } else {
                let (state, slots) = match state.cast::<PyTuple>() {
                    Ok(pair) if pair.len() == 2 => (pair.get_item(0)?, pair.get_item(1)?),
                    _ => (state, none.clone()),
                };
                if !state.is_none() {
                    y.getattr("__dict__")?.call_method1("update", (state,))?;
                }
                if !slots.is_none() {
                    for pair in slots.call_method0("items")?.try_iter()? {
                        let (key, value): (Bound<'py, PyAny>, Bound<'py, PyAny>) =
                            pair?.extract()?;
                        if !key.is_instance_of::<PyString>() {
                            return Err(PyTypeError::new_err(format!(
                                "attribute name must be string, not '{}'",
                                key.get_type().name()?
                            )));
                        }
                        y.setattr(key.cast_into::<PyString>()?, value)?;
                    }
                }
            }
        }
        if !listiter.is_none() {
            for item in listiter.try_iter()? {
                y.call_method1("append", (self.deep(&item?)?,))?;
            }
        }
        if !dictiter.is_none() {
            for pair in dictiter.try_iter()? {
                let (key, value): (Bound<'py, PyAny>, Bound<'py, PyAny>) = pair?.extract()?;
                let (key, value) = (self.deep(&key)?, self.deep(&value)?);
                y.set_item(key, value)?;
            }
        }
        Ok(y)
    }
}

/// Deep copy operation on arbitrary Python objects; drop-in for
/// `copy.deepcopy` with the same memo protocol.
#[pyfunction]
#[pyo3(signature = (x, memo = None))]
pub fn deepcopy<'py>(
    x: &Bound<'py, PyAny>,
    memo: Option<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = x.py();
    let mut copier = Copier {
        py,
        refs: refs(py)?,
        memo: memo.unwrap_or_else(|| PyDict::new(py)),
        depth: 0,
        plans: HashMap::new(),
        setstate_absent: HashMap::new(),
        slots: HashMap::new(),
    };
    copier.deep(x)
}
