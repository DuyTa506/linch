# Skills

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

The terminal `VERDICT:` line is the contract — it gives the host application (or a parent agent) a single, parseable signal of whether the work checked out. The optional `args` string narrows the focus of the verification without changing that contract.

---

## Project skills

Project skills live at `.linch/skills/<name>/SKILL.md`. A project skill named `verify` overrides the built-in.

Each skill is a `SKILL.md` file: YAML frontmatter (name, description, argument handling) plus a markdown body that becomes the injected prompt. Dropping a directory under `.linch/skills/` is enough to make the skill discoverable through the `Skill` tool — no code changes required. Because a project `verify` overrides the built-in, you can specialize the verification workflow for your domain while keeping the same invocation and `VERDICT:` contract.

---

## Related pages

- [Agent configuration](./agent.md) — `FeatureFlags(skills=...)` and the other subsystem toggles
- [Tools](./tools.md) — how the `Skill` tool sits alongside the rest of the tool surface
- [Examples](./examples.md) — runnable demos
