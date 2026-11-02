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

## Bundled scripts

Use `${SKILL_DIR}` in a skill body to refer to that skill's directory (`Skill.dir`).
`${LINCH_SKILL_DIR}` also expands to the same directory. For example, a skill can
instruct the model to run this command through `Bash`:

```bash
python "${SKILL_DIR}/scripts/x.py"
```

The skill author must quote paths that may contain spaces, as in this example.
Substitution preserves the author's quotes; it does not add shell quoting,
execute commands, or expand placeholders recursively in substituted values.
Scripts run through the normal `Bash` tool and its permission checks, including
when the skill directory is outside the workspace. Substitution grants no
execution permission or workspace access; configure Bash rules and approval
callbacks as described in [Permissions](./tools.md#permissions).

The skill directory must be visible at the substituted path to the execution
backend. For example, a Docker backend needs a mount exposing that directory at
the same path; a host-only directory is not automatically available in a container.

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
