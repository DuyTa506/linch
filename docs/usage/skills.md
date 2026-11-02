# Skills

Use skills when the model should invoke reusable prompt workflows during a run.

[← Usage guide](./README.md)

Skills are prompt workflows exposed to the model through the `Skill` tool. Each skill is a named, reusable prompt that the model can invoke mid-run with arguments — think of them as slash-commands the agent can call on itself.

---

## Enabling skills

Skills are available through the `Skill` tool when `FeatureFlags(skills=True)`. The flag is part of the agent's feature configuration; toggle it (along with the other subsystems) via the `features` argument on `Agent`. See [Agent configuration](./agent.md) for the full `FeatureFlags` surface.

---

## The built-in `verify` skill

Linch includes a built-in `verify` skill:

```text
Skill({"skill": "verify", "args": "focus on billing workflow"})
```

`verify` asks the model to plan and run evidence-based checks for completed work, then end with `VERDICT: PASS`, `VERDICT: FAIL`, or `VERDICT: PARTIAL`. It is domain-agnostic: use it for software changes, data workflows, documents, configuration, or other concrete deliverables.

The prompt requests a terminal `VERDICT:` line, which a host or parent agent can parse; Linch does not enforce the model's compliance. The optional `args` string narrows the verification focus.

---

## Project skills

By default, project skills live at `<cwd>/.linch/skills/<directory>/SKILL.md`. `Agent(config_dir=...)` changes the configuration directory; a relative value resolves against the agent's `cwd`, and an absolute path is used directly. Discovery scans that directory's `skills/` children, not a home or global skills directory. A valid disk skill named `verify` overrides the built-in, case-insensitively.

Each file starts with YAML frontmatter and a markdown body. `description` is required; `name` defaults to the directory name. Optional fields include `arguments`, `argument_hint`, `when_to_use`, `allowed_tools`, `model`, `version`, and `disable_model_invocation`. The tool returns the substituted body to the model and records it for later context reinjection. Invalid definitions and symlinked skill directories are skipped. A skill with `disable_model_invocation: true` cannot be invoked by the model. A project override supplies its own instructions, so preserve the verdict format in its body if your host relies on it.

---

## Reloading skills

To rescan skills after changing files, call `await agent.reload_skills()`. The scan
runs off the event loop and updates the existing `Skill` tool. Active sessions
receive a catalog reminder and can invoke the new skills on their next request;
their original system prefix stays frozen. Removing every skill keeps the tool
registered. See [session prefixes](../architecture/system-prompt.md#session-prefix-and-catalog-changes)
for the current durability limitations.

## Related pages

- [Agent configuration](./agent.md) — `FeatureFlags(skills=...)` and the other subsystem toggles
- [Tools](./tools.md) — how the `Skill` tool sits alongside the rest of the tool surface
- [Examples](./examples.md) — runnable demos
