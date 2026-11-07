# Changelog

Notable changes to `linch-rust`: the Rust crates under `crates/` and the
`linch-rust` Python client that ships the `linch-server` binary. The Python
package `linch` has its own [changelog](../CHANGELOG.md).

## 0.1.1 - 2026-11-07

### Fixed

- The task tools (`TaskCreate`, `TaskList`, `TaskGet`, `TaskUpdate`) now belong
  to the session of the root run of the agent tree. A subagent's worker used to
  see an empty list and could not update the coordinator's tasks; it now reads
  and updates them. Unrelated sessions stay apart.
- `TaskStop` on a worker that already finished no longer says it stopped it. The
  reply says the worker already finished and how it ended (`completed`, `failed`
  or `killed`); the worker's status is unchanged, as before.

### Changed

- `linch_tools::task_tools` takes the run store as a second argument:
  `task_tools(store, runs, namespace)`. `TaskCreate::new`, `TaskList::new`,
  `TaskGet::new` and `TaskUpdate::new` take it too, as `new(store, runs,
  namespace)`. Hosts that build the tools themselves pass their `RunStore`; a
  call whose run the store does not know uses its own session.

### Tests

- New tests pin that none of the Python bugs #99 to #108 is present in the Rust
  port (tool scope, file namespaces, secrets kept out of argv, PreToolUse
  actions, Grep cancellation, offload, the roster, worker status and the child
  tool filter).
