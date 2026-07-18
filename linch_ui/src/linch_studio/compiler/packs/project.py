"""Core project, runtime wiring, and deterministic documentation pack."""

from __future__ import annotations

import json
from typing import Any

from ..contributions import FileContribution
from ..ir import CompilerIR
from .common import file, generated_dependency, py, toml


class ProjectPack:
    capability_id = "project.core"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        package = ir.package
        files: list[FileContribution] = [
            file("pyproject.toml", _pyproject(ir), "project.packaging"),
            file("README.md", _readme(ir), "docs.readme"),
            file("DEVELOPMENT.md", _development(ir), "docs.development"),
            file("ARCHITECTURE.md", _architecture(ir), "docs.architecture"),
            file("SECURITY.md", _security(ir), "docs.security"),
            file(".env.example", _env_example(ir), "project.environment"),
            file(".gitignore", _gitignore(), "project.packaging"),
            file("linch-studio.yaml", ir.blueprint_yaml, "studio.provenance"),
            file(f"src/{package}/__init__.py", _package_init(ir), "project.runtime"),
            file(f"src/{package}/settings.py", _settings(ir), "project.settings"),
            file(f"src/{package}/resources.py", _resources(ir), "project.resources"),
            file(
                f"src/{package}/integrations/__init__.py",
                '"""Host-owned integrations and explicit adapter TODOs."""\n',
                "extensions.package",
            ),
            file(f"src/{package}/observability.py", _observability(ir), "observation.logging"),
            file(f"src/{package}/agent.py", _agent(ir), f"execution.{ir.primary_mode}"),
            file(f"src/{package}/__main__.py", _main(ir), "project.entrypoint"),
            file("tests/test_agent.py", _test_agent(ir), "tests.offline"),
            file("tests/test_provenance.py", _test_provenance(), "tests.provenance"),
        ]
        if _has_prompt_config(ir):
            files.append(file(f"src/{package}/prompts.py", _prompts(ir), "prompt.static"))
        files.append(file(f"src/{package}/reliability.py", _reliability(ir), "reliability.runtime"))
        if _needs_permissions_module(ir):
            files.append(
                file(
                    f"src/{package}/permissions.py",
                    _permissions(ir),
                    "permissions.ordered_rules",
                )
            )
        files.extend(_skeleton_modules(ir))
        return tuple(files)


def _pyproject(ir: CompilerIR) -> str:
    dependency = generated_dependency(ir)
    verifier_kinds = {verifier.kind for verifier in ir.completion.verifiers} | {
        verifier.kind
        for routine in ir.routines
        for verifier in (routine.verify, routine.done_when)
        if verifier is not None
    }
    runtime_dependencies = [dependency]
    if "json_schema" in verifier_kinds:
        runtime_dependencies.append("jsonschema>=4.0")
    rendered_dependencies = ", ".join(toml(item) for item in runtime_dependencies)
    return f'''\
[build-system]
requires = ["hatchling>=1.24"]
build-backend = "hatchling.build"

[project]
name = {toml(ir.project_name)}
version = "0.1.0"
description = {toml(ir.description or ir.title)}
requires-python = {toml(ir.python_constraint)}
dependencies = [{rendered_dependencies}]

[project.optional-dependencies]
dev = [
  "pytest>=8.0,<9.0",
  "pytest-asyncio>=0.23,<1.0",
  "ruff>=0.5",
  "pyright>=1.1",
]

[project.scripts]
{ir.project_name.replace("_", "-")} = "{ir.package}.__main__:main"

[tool.hatch.build.targets.wheel]
packages = ["src/{ir.package}"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src"]

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
# Blueprint text and JSON Schema literals can be intrinsically long; Ruff's
# formatter also leaves some such literals intact.
ignore = ["E501"]

[tool.pyright]
include = ["src"]
pythonVersion = "3.10"
typeCheckingMode = "basic"
reportMissingImports = false
'''


def _package_init(ir: CompilerIR) -> str:
    return f'''\
"""{ir.title} — generated from a Linch Studio blueprint."""

from .agent import build_agent

__all__ = ["build_agent"]
'''


def _settings(ir: CompilerIR) -> str:
    return f'''\
"""Environment-backed settings. Secret values never live in the blueprint."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    model: str = {py(ir.provider.model)}

    @classmethod
    def from_env(cls) -> Settings:
        return cls(model=os.environ.get("LINCH_MODEL", {py(ir.provider.model)}))
'''


def _resources(ir: CompilerIR) -> str:
    caps = ir.capabilities()
    persistence = caps["persistence"]
    memory = caps["memory"]
    filesystem = caps["filesystem"]
    external_db = caps["externalDatabase"]

    imports: list[str] = []
    session_import = {
        "in_memory": "from linch import InMemorySessionStore",
        "sqlite": "from linch import SqliteSessionStore",
        "external": "",
    }[persistence["sessionStore"]]
    run_import = {
        "in_memory": "from linch import InMemoryRunStore",
        "sqlite": "from linch import SqliteRunStore",
        "external": "",
    }[persistence["runStore"]]
    imports.extend(item for item in (session_import, run_import) if item)
    memory_imports = {
        "in_memory": "InMemoryKeywordMemoryStore",
        "sqlite": "SqliteMemoryStore",
        "postgres_keyword": "PostgresMemoryStore",
        "tiered": "InMemoryKeywordMemoryStore, TieredMemoryStore",
    }
    if memory["backend"] in memory_imports:
        imports.append(f"from linch import {memory_imports[memory['backend']]}")
    fs_imports = {
        "state": "StateFileBackend",
        "disk": "DiskFileBackend",
        "sqlite": "SqliteFileBackend",
        "composite": "CompositeFileBackend, SqliteFileBackend, StateFileBackend",
    }
    if filesystem["backend"] in fs_imports:
        imports.append(f"from linch import {fs_imports[filesystem['backend']]}")
    if external_db["enabled"]:
        imports.append(
            f"from {ir.package}.integrations.database import DatabaseClient, create_database_client"
        )
        database_annotation = "DatabaseClient | None"
    else:
        database_annotation = "Any"

    session_body = {
        "in_memory": "return InMemorySessionStore()",
        "sqlite": 'return SqliteSessionStore(".linch/sessions.db")',
        "external": (
            'raise NotImplementedError("TODO: implement the external SessionStore adapter")'
        ),
    }[persistence["sessionStore"]]
    run_body = {
        "in_memory": "return InMemoryRunStore()",
        "sqlite": 'return SqliteRunStore(".linch/runs.db")',
        "external": 'raise NotImplementedError("TODO: implement the external RunStore adapter")',
    }[persistence["runStore"]]
    memory_body = _memory_factory(memory)
    filesystem_body = _filesystem_factory(filesystem)
    db_open = (
        "database = await create_database_client()" if external_db["enabled"] else "database = None"
    )
    db_close = (
        """\
        closer = getattr(database, "aclose", None) if database is not None else None
        if closer is not None:
            await closer()"""
        if external_db["enabled"]
        else "        pass"
    )
    linch_names: set[str] = set()
    other_imports: list[str] = []
    for import_line in imports:
        if import_line.startswith("from linch import "):
            linch_names.update(
                part.strip() for part in import_line.removeprefix("from linch import ").split(",")
            )
        else:
            other_imports.append(import_line)
    linch_import = ""
    if linch_names:
        linch_import = (
            "from linch import (\n"
            + "".join(f"    {name},\n" for name in sorted(linch_names))
            + ")"
        )
    import_groups = [
        group for group in (linch_import, "\n".join(sorted(set(other_imports)))) if group
    ]
    import_block = "\n\n".join(import_groups)
    os_import = "import os\n" if memory["backend"] == "postgres_keyword" else ""
    return f'''\
"""Application dependencies and resource lifecycle."""

from __future__ import annotations

{os_import}from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

{import_block}


@dataclass(slots=True)
class AppDeps:
    database: {database_annotation} = None
    memory_store: Any = None


def build_session_store() -> Any:
    {session_body}


def build_run_store() -> Any:
    {run_body}


def build_memory_store() -> Any:
    {memory_body}


def build_filesystem() -> Any:
    {filesystem_body}


@asynccontextmanager
async def open_resources() -> AsyncIterator[AppDeps]:
    {db_open}
    memory_store = build_memory_store()
    deps = AppDeps(database=database, memory_store=memory_store)
    try:
        yield deps
    finally:
{db_close}
        memory_closer = getattr(memory_store, "aclose", None) if memory_store is not None else None
        if memory_closer is not None:
            await memory_closer()
'''


def _memory_factory(memory: dict[str, Any]) -> str:
    backend = memory["backend"]
    if backend == "none":
        return "return None"
    if backend == "in_memory":
        return "return InMemoryKeywordMemoryStore()"
    if backend == "sqlite":
        return 'return SqliteMemoryStore(".linch/memory.db")'
    if backend == "postgres_keyword":
        env = memory["dsnEnv"]
        return (
            f"dsn = os.environ.get({env!r})\n"
            "    if not dsn:\n"
            f'        raise RuntimeError({env!r} + " is required for Postgres memory")\n'
            "    return PostgresMemoryStore(dsn)"
        )
    if backend == "tiered":
        return (
            "store = InMemoryKeywordMemoryStore\n"
            "    return TieredMemoryStore(working=store(), episodic=store(), semantic=store())"
        )
    return (
        'raise NotImplementedError("TODO: implement the selected MemoryStore adapter in '
        'integrations/custom_memory.py")'
    )


def _filesystem_factory(filesystem: dict[str, Any]) -> str:
    backend = filesystem["backend"]
    root = filesystem["root"]
    if backend == "none":
        return "return None"
    if backend == "state":
        return "return StateFileBackend()"
    if backend == "disk":
        return f"return DiskFileBackend({py(root)})"
    if backend == "sqlite":
        return f"return SqliteFileBackend({py(root)})"
    if backend == "composite":
        return (
            "return CompositeFileBackend(\n"
            "        default=StateFileBackend(),\n"
            f'        routes={{"/persistent": SqliteFileBackend({py(root)})}},\n'
            "    )"
        )
    return (
        'raise NotImplementedError("TODO: implement the external FileBackend adapter in '
        'integrations/custom_filesystem.py")'
    )


def _observability(ir: CompilerIR) -> str:
    caps = ir.capabilities()["observation"]
    observers: list[str] = []
    imports = ["RunTelemetryHook"]
    if caps["logging"]:
        imports.append("LoggingObserver")
        observers.append("LoggingObserver()")
    if caps["otel"]:
        imports.append("OpenTelemetryObserver")
        observers.append("OpenTelemetryObserver()")
    import_names = ", ".join(sorted(imports))
    return f'''\
"""Typed observation hooks. Reports remain local unless a host exports them."""

from linch import {import_names}


def build_observation_hooks() -> list[object]:
    observers = [{", ".join(observers)}]
    return [RunTelemetryHook(observers)] if observers else []
'''


def _prompts(ir: CompilerIR) -> str:
    caps = ir.capabilities()["prompt"]
    text_parts = [part for part in (ir.primary_instructions, caps["text"]) if part]
    append = "\n\n".join(text_parts) or None
    prompt_imports = "SystemPromptConfig"
    if caps["sections"]:
        prompt_imports += ", SystemPromptSection"
    section_lines = []
    for section in caps["sections"]:
        section_lines.append(
            "SystemPromptSection(\n"
            f"                name={py(section['id'])},\n"
            f"                text={py(section['text'])},\n"
            f"                cacheable={py(section['cacheable'])},\n"
            f"                placement={py(section['placement'])},\n"
            "            )"
        )
    sections_expr = (
        "[\n" + "\n".join(f"            {section}," for section in section_lines) + "\n        ]"
        if section_lines
        else "[]"
    )
    return f'''\
"""Stable, ordered prompt assembly generated from the blueprint."""

from linch import {prompt_imports}


def build_prompt_config() -> SystemPromptConfig:
    return SystemPromptConfig(
        append={py(append)},
        replace_defaults={py(caps["mode"] == "replace_defaults")},
        sections={sections_expr},
    )
'''


def _reliability(ir: CompilerIR) -> str:
    caps = ir.capabilities()
    reliability = caps["reliability"]
    compaction = caps["compaction"]
    structured = caps["structuredOutput"]
    imports = ["LoopGuard", "RunBudget"]
    lines = [
        f'"max_retries": {reliability["providerRetry"]["maxAttempts"]}',
        f'"include_partial_messages": {py(ir.provider.kind in {"openai_chat", "anthropic"})}',
    ]
    optional_values = {
        "max_output_tokens": reliability["maxOutputTokens"],
        "max_tool_concurrency": reliability["maxToolConcurrency"],
        "tool_timeout_ms": reliability["toolTimeoutMs"],
    }
    for key, value in optional_values.items():
        if value is not None:
            lines.append(f"{py(key)}: {py(value)}")
    if reliability["toolRetry"] is not None:
        imports.append("RetryOptions")
        retry = reliability["toolRetry"]
        lines.append(
            '"tool_retry": RetryOptions('
            f"max_attempts={retry['maxAttempts']}, base_delay_ms={retry['baseDelayMs']}, "
            f"max_delay_ms={retry['maxDelayMs']}, jitter={py(retry['jitter'])})"
        )
    guard = reliability["loopGuard"]
    guard_expr = (
        "LoopGuard(\n"
        f"            max_identical_tool_calls={guard['maxIdenticalToolCalls']},\n"
        f"            max_consecutive_failures={guard['maxConsecutiveFailures']},\n"
        f"            force_final_answer={py(guard['forceFinalAnswer'])},\n"
        "        )"
        if guard["enabled"]
        else "None"
    )
    lines.append(f'"loop_guard": {guard_expr}')
    recovery = reliability["truncationRecovery"]
    if recovery["enabled"]:
        imports.append("TruncationRecovery")
        feedback = (
            f", feedback={py(recovery['feedback'])}" if recovery["feedback"] is not None else ""
        )
        lines.append(
            '"truncation_recovery": TruncationRecovery('
            f"max_attempts={recovery['maxAttempts']}{feedback})"
        )

    strategy = compaction["strategy"]
    if strategy == "default_coding":
        imports.append("DefaultCompaction")
        lines.append('"compaction": DefaultCompaction()')
    elif strategy == "general":
        imports.extend(["DefaultCompaction", "GENERAL_SUMMARY_PROMPT"])
        lines.append('"compaction": DefaultCompaction(prompt=GENERAL_SUMMARY_PROMPT)')
    elif strategy == "detailed":
        imports.append("DetailedCompaction")
        lines.append(
            '"compaction": DetailedCompaction('
            f"keep_recent_turns={compaction['keepRecentTurns']}, "
            f"max_output_tokens={compaction['maxOutputTokens']})"
        )
    elif strategy == "custom":
        lines.append('"compaction": CustomCompaction()')
    ladder = compaction["ladder"]
    if ladder["enabled"]:
        imports.append("CompactionLadder")
        lines.append(
            '"compaction_ladder": CompactionLadder(\n'
            f"            micro={py(ladder['micro'])},\n"
            f"            keep_recent_turns={ladder['keepRecentTurns']},\n"
            f"            max_forced_compactions={ladder['maxForcedCompactions']},\n"
            f"            reset_read_tracker={py(ladder['resetReadTracker'])},\n"
            "        )"
        )
    if structured["enabled"]:
        imports.append("OutputSchema")
        lines.append(
            '"output_schema": OutputSchema('
            f"name={py(structured['name'])}, schema={py(structured['schema'])}, "
            f"strict={py(structured['strict'])})"
        )
        lines.append(f'"structured_output_retries": {structured["repairRetries"]}')
        if structured["finalToolName"]:
            lines.append(f'"final_tool_name": {py(structured["finalToolName"])}')
    if reliability["customTokenEstimator"]:
        lines.append('"token_estimator": custom_token_estimator')

    custom_imports = []
    if strategy == "custom":
        custom_imports.append(
            f"from {ir.package}.integrations.custom_reliability import CustomCompaction"
        )
    if reliability["customTokenEstimator"]:
        custom_imports.append(
            f"from {ir.package}.integrations.custom_reliability import custom_token_estimator"
        )
    options = ",\n        ".join(lines) + ","
    budget_data = json.loads(ir.primary_budget_json)
    budget_args = []
    for source, target in (
        ("maxTokens", "max_tokens"),
        ("maxCostUsd", "max_cost_usd"),
        ("warnRatio", "warn_ratio"),
    ):
        value = budget_data.get(source)
        if value is not None:
            budget_args.append(f"{target}={py(value)}")
    budget_expr = (
        f"RunBudget({', '.join(budget_args)})"
        if any(budget_data.get(key) is not None for key in ("maxTokens", "maxCostUsd"))
        else "None"
    )
    imports_text = "".join(f"    {name},\n" for name in sorted(set(imports)))
    custom_text = ("\n" + "\n".join(custom_imports)) if custom_imports else ""
    return f'''\
"""Reliability, compaction, output-contract, and budget wiring."""

from __future__ import annotations

from typing import Any

from linch import (
{imports_text}){custom_text}


def build_budget() -> RunBudget | None:
    return {budget_expr}


def build_reliability_options() -> dict[str, Any]:
    return {{
        {options}
    }}
'''


def _permissions(ir: CompilerIR) -> str:
    caps = ir.capabilities()["permissions"]
    mode = {
        "interactive": "default",
        "accept_edits": "acceptEdits",
        "trusted": "skip-dangerous",
    }[caps["mode"]]
    imports: set[str] = set()
    rules: list[str] = []
    for rule in caps["rules"]:
        if rule["kind"] == "tool":
            imports.add("ToolRule")
            rules.append(
                f"ToolRule({py(rule['tool'])}, {py(rule['decision'])}, arg={py(rule['argument'])})"
            )
        elif rule["kind"] == "path":
            imports.add("PathRule")
            tools = rule["tools"] or None
            rules.append(
                f"PathRule(paths={py(rule['paths'])}, decision={py(rule['decision'])}, "
                f"tools={py(tools)})"
            )
        else:
            imports.add("BashRule")
            rules.append(
                f"BashRule(patterns={py(rule['patterns'])}, decision={py(rule['decision'])})"
            )
    import_line = f"from linch import {', '.join(sorted(imports))}\n\n" if imports else ""
    return f'''\
"""Explicit ordered permission policy generated from the blueprint."""

from __future__ import annotations

from typing import Any

{import_line}
def build_permissions() -> dict[str, Any]:
    return {{"mode": {py(mode)}, "rules": [{", ".join(rules)}]}}
'''


def _agent(ir: CompilerIR) -> str:
    caps = ir.capabilities()
    memory = caps["memory"]
    filesystem = caps["filesystem"]
    extensions = caps["extensions"]
    hooks = caps["hooks"]
    deep = ir.primary_mode in {"deep_agent", "coordinator"}
    imports = ["Agent", "FeatureFlags", "default_tools" if deep else "empty_tools"]
    if deep:
        imports.append("create_deep_agent")
    if filesystem["offloadEnabled"]:
        imports.append("OffloadConfig")
    if memory["searchTool"]:
        imports.append("MemorySearchTool")
    if memory["upsertTool"]:
        imports.append("MemoryUpsertTool")
    if memory["backend"] != "none" and caps["context"]["memoryRecall"]:
        imports.extend(["ContextInjectionHook", "MemoryContextBuilder"])
    if hooks["redactionRules"]:
        imports.extend(["RedactionConfig", "RedactionHook", "RedactionRule"])

    custom_tool_import = ""
    custom_tool_code = "registry = default_tools()" if deep else "registry = empty_tools()"
    primary_tool_ids = (
        tuple(tool.id for tool in ir.tools) if ir.primary_tools is None else ir.primary_tools
    )
    if primary_tool_ids:
        custom_tool_import = f"from {ir.package}.tools import ALL_TOOLS\n"
        if ir.primary_tools is None and not deep:
            custom_tool_code = "registry = empty_tools(*ALL_TOOLS)"
        else:
            custom_tool_code = "registry = default_tools()" if deep else "registry = empty_tools()"
            custom_tool_code += "\nfor tool in ALL_TOOLS:\n"
            if ir.primary_tools is None:
                custom_tool_code += "    registry.register(tool)"
            else:
                custom_tool_code += (
                    f"    if tool.name in {py(set(primary_tool_ids))}:\n"
                    "        registry.register(tool)"
                )
    memory_tool_lines = []
    if memory["searchTool"]:
        memory_tool_lines.append(
            "registry.register(MemorySearchTool(memory_store, "
            f"namespace={py(memory['namespace'])}))"
        )
    if memory["upsertTool"]:
        memory_tool_lines.append(
            "registry.register(MemoryUpsertTool(memory_store, "
            f"namespace={py(memory['namespace'])}))"
        )
    tool_setup = "\n".join(
        f"    {line.replace(chr(10), chr(10) + '    ')}"
        for line in [custom_tool_code, *memory_tool_lines]
    )

    prompt_import = ""
    prompt_arg = "None"
    if _has_prompt_config(ir):
        prompt_import = f"from {ir.package}.prompts import build_prompt_config\n"
        prompt_arg = "build_prompt_config()"
    permissions_import = ""
    permissions_arg = '{"mode": "default", "rules": []}'
    if _needs_permissions_module(ir):
        permissions_import = f"from {ir.package}.permissions import build_permissions\n"
        permissions_arg = "build_permissions()"
    mcp_import = ""
    mcp_arg = "None"
    if extensions["mcpServers"]:
        mcp_import = f"from {ir.package}.integrations.mcp import build_mcp_servers\n"
        mcp_arg = "build_mcp_servers() if enable_integrations else None"

    completion_import = ""
    if ir.completion.verifiers:
        completion_import = f"from {ir.package}.completion import build_completion_hooks\n"

    hook_lines = ["*(build_observation_hooks() if enable_integrations else [])"]
    if ir.completion.verifiers:
        hook_lines.insert(0, "*build_completion_hooks()")
    if memory["backend"] != "none" and caps["context"]["memoryRecall"]:
        context = caps["context"]
        hook_lines.append(
            "ContextInjectionHook(\n"
            "    MemoryContextBuilder(\n"
            "        memory_store,\n"
            f"        limit={context['maxItems']},\n"
            f"        namespace={py(memory['namespace'])},\n"
            f"        max_tokens={py(context['maxTokens'])},\n"
            "    )\n"
            ")"
        )
    if hooks["redactionRules"]:
        rules = "\n".join(
            "            RedactionRule(\n"
            f"                pattern={py(rule['pattern'])},\n"
            f"                replacement={py(rule['replacement'])},\n"
            "            ),"
            for rule in hooks["redactionRules"]
        )
        hook_lines.append(
            f"RedactionHook(\n    RedactionConfig(\n        rules=(\n{rules}\n        ),\n    )\n)"
        )
    hooks_expr = (
        "[\n"
        + "\n".join(
            f"        {line.replace(chr(10), chr(10) + '        ')}," for line in hook_lines
        )
        + "\n    ]"
    )

    result_offload = "None"
    if filesystem["offloadEnabled"]:
        result_offload = (
            "OffloadConfig(\n"
            f"            threshold_tokens={py(filesystem['offloadThresholdTokens'])},\n"
            f"            threshold_fraction={py(filesystem['offloadThresholdFraction'])},\n"
            f"            preview_lines={filesystem['previewLines']},\n"
            "        )"
        )
    imports_text = "".join(f"    {name},\n" for name in sorted(set(imports)))
    primary_max_turns = py(ir.primary_max_turns)
    max_turns_line = (
        "    effective_max_turns = max_turns_override "
        f"if max_turns_override is not None else {primary_max_turns}"
    )
    deep_call = ""
    if deep:
        deep_call = f"""\
    return create_deep_agent(
        model=settings.model,
        provider=active_provider,
        coordinator={py(ir.primary_mode == "coordinator")},
        durable=False,
        cwd=cwd,
        tools=registry,
        permissions={permissions_arg},
        session_store=active_session_store,
        run_store=active_run_store,
        features=features,
        system_prompt_config={prompt_arg},
        hooks=hooks_list,
        deps=active_deps,
        max_turns=effective_max_turns,
        budget=build_budget(),
        filesystem=active_filesystem,
        result_offload={result_offload},
        mcp_servers={mcp_arg},
        read_before_write={py(hooks["readBeforeWrite"])},
        tool_cache={py(hooks["toolCache"])},
        fallback_models={py(list(ir.provider.fallback_models))},
        **build_reliability_options(),
    )"""
    else:
        deep_call = f"""\
    return Agent(
        model=settings.model,
        provider=active_provider,
        cwd=cwd,
        tools=registry,
        permissions={permissions_arg},
        session_store=active_session_store,
        run_store=active_run_store,
        features=features,
        system_prompt_config={prompt_arg},
        hooks=hooks_list,
        deps=active_deps,
        max_turns=effective_max_turns,
        budget=build_budget(),
        filesystem=active_filesystem,
        result_offload={result_offload},
        mcp_servers={mcp_arg},
        read_before_write={py(hooks["readBeforeWrite"])},
        tool_cache={py(hooks["toolCache"])},
        fallback_models={py(list(ir.provider.fallback_models))},
        **build_reliability_options(),
    )"""
    return f'''\
"""Central Linch runtime wiring generated from the blueprint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from linch import (
{imports_text})

{completion_import}{mcp_import}from {ir.package}.observability import build_observation_hooks
{permissions_import}{prompt_import}from {ir.package}.providers import build_provider
from {ir.package}.reliability import build_budget, build_reliability_options
from {ir.package}.resources import (
    AppDeps,
    build_filesystem,
    build_memory_store,
    build_run_store,
    build_session_store,
)
from {ir.package}.settings import Settings
{custom_tool_import}

def build_agent(
    provider: Any = None,
    *,
    deps: AppDeps | None = None,
    session_store: Any = None,
    run_store: Any = None,
    memory_store: Any = None,
    filesystem: Any = None,
    cwd: str | None = None,
    enable_integrations: bool = True,
    max_turns_override: int | None = None,
) -> Agent:
    """Build the configured runtime without executing a workflow."""
    settings = Settings.from_env()
    active_provider = provider if provider is not None else build_provider()
    active_session_store = session_store if session_store is not None else build_session_store()
    active_run_store = run_store if run_store is not None else build_run_store()
    active_memory_store = (
        memory_store
        if memory_store is not None
        else (deps.memory_store if deps is not None else build_memory_store())
    )
    active_filesystem = filesystem if filesystem is not None else build_filesystem()
    active_deps = deps or AppDeps(memory_store=active_memory_store)
{max_turns_line}
    memory_store = active_memory_store
{tool_setup}
    hooks_list = {hooks_expr}
    features = FeatureFlags(
        skills={py(bool(ir.skills))},
        subagents={py(bool(ir.subagents) or deep)},
        mcp={py(bool(extensions["mcpServers"]))} and enable_integrations,
        filesystem={py(filesystem["backend"] != "none")},
    )
    cwd = cwd or str(Path.cwd())
{deep_call}
'''


def _main(ir: CompilerIR) -> str:
    return f'''\
"""One-shot command-line host. Process lifetime remains the host's responsibility."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from {ir.package}.agent import build_agent
from {ir.package}.resources import open_resources


async def run(prompt: str) -> int:
    async with open_resources() as deps:
        agent = build_agent(deps=deps)
        try:
            session = await agent.session()
            async for event in session.run(prompt):
                if event.type == "result":
                    print(event.final_text or "")
                    return 0 if event.subtype == "success" else 1
        finally:
            await agent.close()
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog={py(ir.project_name.replace("_", "-"))})
    parser.add_argument("prompt", nargs="?", default="Run the configured agent task.")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.prompt))


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _test_agent(ir: CompilerIR) -> str:
    external_db = ir.capabilities()["externalDatabase"]["enabled"]
    deps_import = (
        f"from {ir.package}.integrations.database import FakeDatabaseClient\n"
        if external_db
        else ""
    )
    deps_expr = "AppDeps(database=FakeDatabaseClient())" if external_db else "AppDeps()"
    session_args = 'meta={"parentSessionId": "offline-smoke"}' if ir.completion.verifiers else ""
    declared_tool_names = {tool.id for tool in ir.tools}
    expected_primary_tools = (
        declared_tool_names if ir.primary_tools is None else set(ir.primary_tools)
    )
    deep_tool_assertion = ""
    if ir.primary_mode == "deep_agent":
        deep_tool_assertion = '        assert {"Bash", "Read", "TaskCreate"} <= actual_tool_names\n'
    elif ir.primary_mode == "coordinator":
        deep_tool_assertion = (
            '        assert "Read" not in actual_tool_names\n'
            '        assert "TaskCreate" in actual_tool_names\n'
        )
    return f'''\
"""Offline runtime smoke test; no API credentials or network required."""

from linch import (
    InMemoryKeywordMemoryStore,
    InMemoryRunStore,
    InMemorySessionStore,
    ScriptedProvider,
    StateFileBackend,
    TextTurn,
)

from {ir.package}.agent import build_agent
{deps_import}from {ir.package}.resources import AppDeps


async def test_agent_runs_offline() -> None:
    agent = build_agent(
        provider=ScriptedProvider([TextTurn("offline-ready")]),
        deps={deps_expr},
        session_store=InMemorySessionStore(),
        run_store=InMemoryRunStore(),
        memory_store=InMemoryKeywordMemoryStore(),
        filesystem=StateFileBackend(),
        enable_integrations=False,
    )
    try:
        declared_project_tools = {py(declared_tool_names)}
        expected_project_tools = {py(expected_primary_tools)}
        actual_tool_names = {{tool.name for tool in agent.tools.list()}}
        assert actual_tool_names & declared_project_tools == expected_project_tools
{deep_tool_assertion}        session = await agent.session({session_args})
        results = [event async for event in session.run("smoke") if event.type == "result"]
        assert results[-1].final_text == "offline-ready"
    finally:
        await agent.close()
'''


def _test_provenance() -> str:
    return '''\
"""The generated manifest is provenance, not executable configuration."""

import hashlib
import json
from pathlib import Path


def test_manifest_hashes_generated_files() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ".linch-studio-manifest.json").read_text(encoding="utf-8"))
    for record in manifest["files"]:
        content = (root / record["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == record["sha256"]
'''


def _readme(ir: CompilerIR) -> str:
    return f"""\
# {ir.title}

{ir.description or "A Linch project generated by Linch Studio."}

This is a one-way generated skeleton. The Python code is now the source of truth;
`linch-studio.yaml` and `.linch-studio-manifest.json` preserve provenance only.

## Install and test

```bash
python -m pip install -e '.[dev]'
pytest
python -m {ir.package} "Your prompt"
```

Copy `.env.example` to your host's secret manager or environment. Never commit
secret values. Studio does not run, deploy, or schedule this project.

New to the SDK? Start with the [Linch Quickstart]({_SDK_DOCS_BASE}/usage/quickstart.md).
See `DEVELOPMENT.md` for capability-specific TODOs and the SDK docs to read for
each, and `SECURITY.md` before enabling write/exec tools or headless triggers.
"""


def _development(ir: CompilerIR) -> str:
    todos = _todos(ir)
    todo_lines = "\n".join(
        f"{index}. `{path}` — {text}" for index, (path, text) in enumerate(todos, 1)
    )
    if not todo_lines:
        todo_lines = (
            "1. No blocking skeleton TODOs were selected; review policy and tests before release."
        )
    env_lines = "\n".join(f"- `{name}`" for name in ir.required_env) or (
        "- None required for offline tests."
    )
    capabilities = "\n".join(f"- `{item}`" for item in ir.selected_capabilities)
    doc_links = _sdk_doc_links(ir)
    sdk_lines = (
        "\n".join(f"- [{label}]({_SDK_DOCS_BASE}/{path})" for path, label in doc_links)
        or f"- [Quickstart]({_SDK_DOCS_BASE}/usage/quickstart.md) — install and first agent"
    )
    primary_tool_policy = (
        "all declared project tools"
        if ir.primary_tools is None
        else f"the explicit allow-list `{list(ir.primary_tools)}`"
    )
    routine_handoff = _routine_handoff(ir)
    return f"""\
# Development guide

Generated deterministically by Linch Studio. Do not regenerate into this directory.

## Ordered blocking TODOs

{todo_lines}

## Learn the Linch SDK

Each blocking TODO above is a seam whose types and contract are documented in the
Linch SDK. Read the pages relevant to this design before filling one in:

{sdk_lines}

## Required environment variables

Names only; values belong in the host secret manager:

{env_lines}

## Blueprint-to-Linch mapping

{capabilities}

- The runtime is built in `src/{ir.package}/agent.py` from top-level `linch` symbols.
- The primary runtime registry exposes {primary_tool_policy}. An explicit empty list exposes no
  declared project tools; capability-provided and deep-agent primitive tools remain independent.
- Provider construction is isolated in `providers.py`; fallbacks are models on that same provider.
- Workflow graph nodes become `wf.agent`; nodes at one topological depth use `wf.parallel`.
- Agent-tick routines use one-shot `LoopRunner.run_once()`; workflow routines call the generated
  directed workflow directly. The host owns cron and process lifetime.

## Workflow and trigger handoff

{routine_handoff}

## Implementation guidance

- Custom and external tools must return explicit errors until implemented. Write operations need
  durable idempotency keyed by the input key and `ToolContext.idempotency_key`.
- Keep stable prompt sections before volatile context so provider prefix caching stays warm.
- Compaction changes only provider-visible history; memory and offloaded files are separate durable
  seams. Re-test read-before-write behavior after changing either.
- Hook order is declaration order. Linch application verifiers are permissive by default;
  Studio-generated completion gates explicitly stop on implementation errors and exhaustion.
  Telemetry failures must not crash a run.
- Headless cron, CI, or webhook hosts cannot answer interactive permission prompts. Resolve every
  write/exec decision explicitly before deployment.
- `LoggingObserver` and local `RunReport` are the baseline. Configure OTel exporters in the host;
  inspect reports for slow/failing tools, context pressure, compaction, and cost.
- SQLite is single-host durability. Implement and contract-test external stores before claiming
  cross-process or multi-host resume.

## Commands

```bash
python -m pip install -e '.[dev]'
pytest
ruff check . && ruff format --check .
pyright
python -m {ir.package} "Run one task"
```

Target runtime constraint: `{ir.linch_constraint}`. On upgrade, review `linch.__all__`, run the
offline tests and adapter contract tests, then exercise one real provider call in staging.
"""


def _architecture(ir: CompilerIR) -> str:
    workflow_text = (
        "Directed, replayable workflows are static DAGs compiled into depth-ordered "
        "`wf.parallel` calls."
        if ir.workflows
        else "No directed workflow DAG is selected."
    )
    routine_text = (
        "Routines are one-shot trigger wrappers; generated code never owns a daemon."
        if ir.routines
        else "No routine is selected."
    )
    primary_tool_policy = (
        "all declared project tools"
        if ir.primary_tools is None
        else f"an explicit declared-tool allow-list: {', '.join(ir.primary_tools) or '(empty)'}"
    )
    return f"""\
# Architecture

```text
host input -> {ir.package}.agent.build_agent -> Linch Agent/Session -> typed events -> host
```

- Execution model: `{ir.primary_mode}`.
- Provider: `{ir.provider.kind}` / `{ir.provider.model}`.
- One provider runtime is shared by workflow subagents.
- Primary runtime declared-tool policy: {primary_tool_policy}.
- {workflow_text}
- {routine_text}
- Tools declare scope, parallelism, resources, retryability, and an input schema.
- Stores, database clients, and filesystem backends enter through `resources.py` / `AppDeps`.
- The host owns process lifetime, deployment, secrets, scheduling, and human approval UX.
"""


def _security(ir: CompilerIR) -> str:
    return """\
# Security

- Prompts, tool inputs/results, event streams, reports, memory, and offloaded files may contain
  sensitive data. Treat all of them as application data with the same access controls.
- Redaction hooks are policy helpers, not complete event-level sanitization. They do not prove
  that every provider payload, exception, custom observer, database log, or host log is scrubbed.
- Keep secret values out of `linch-studio.yaml`, manifests, telemetry, ZIPs, and source control.
  This project reads secret values only through the environment names listed in `.env.example`.
- Review ordered Tool/Path/Bash rules before enabling write or exec tools. Trusted mode is an
  explicit high-risk choice, especially for headless runs.
- MCP commands and external adapters execute with host privileges. Pin and audit dependencies,
  isolate workloads, restrict network/filesystem access, and validate all external responses.
- Generated TODO tools fail explicitly. Do not replace their error with placeholder success.
"""


def _env_example(ir: CompilerIR) -> str:
    names = ["LINCH_MODEL", *ir.required_env]
    return "\n".join(f"{name}=" for name in dict.fromkeys(names))


def _gitignore() -> str:
    return """\
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/
.pyright/
dist/
*.egg-info/
.env
.linch/*.db
domains/
"""


def _has_prompt_config(ir: CompilerIR) -> bool:
    prompt = ir.capabilities()["prompt"]
    return bool(
        ir.primary_instructions
        or prompt["text"]
        or prompt["sections"]
        or prompt["mode"] != "append"
    )


def _needs_permissions_module(ir: CompilerIR) -> bool:
    permissions = ir.capabilities()["permissions"]
    return permissions["mode"] != "interactive" or bool(permissions["rules"])


_SDK_DOCS_BASE = "https://github.com/DuyTa506/linch/blob/main/docs"

# Capability area (the part before the first dot) → the SDK doc page that teaches
# the seam a generated skeleton leaves open, and a short label for it. Kept in the
# parent repo's docs/ tree; test_development_guide_links_only_to_docs_that_exist
# fails if any of these paths is renamed away.
_AREA_DOCS: dict[str, tuple[str, str]] = {
    "tools": ("usage/tools.md", "Tool protocol: @tool, ToolContext, ToolResult, deps"),
    "execution": ("usage/agent.md", "Agent and session wiring"),
    "providers": ("usage/providers.md", "Providers and the model catalog"),
    "memory": ("usage/context-and-memory.md", "Memory primitives and the MemoryStore seam"),
    "context_rag": ("usage/context-and-memory.md", "Per-turn context building"),
    "hooks": ("usage/hooks.md", "Hooks: the extension chokepoints"),
    "completion": ("usage/hooks.md", "Verifiers and completion gates"),
    "filesystem": ("usage/filesystem.md", "Virtual filesystem backends"),
    "persistence": ("usage/production.md", "Persistence, resume, and shutdown"),
    "permissions": ("usage/tools.md", "Tool permissions"),
    "observation": ("usage/events.md", "Events, run reports, and cost"),
    "evals": ("usage/evals.md", "Eval suites and scorers"),
    "reliability": ("usage/agent.md", "Budgets and truncation recovery"),
    "structured_output": ("usage/structured-output.md", "Structured output schemas"),
    "prompt": ("usage/agent.md", "System prompt assembly"),
    "compaction": ("usage/agent.md", "Compaction"),
    "extensions": ("usage/extending.md", "Custom seams: Mailbox, ScheduleStore, isolation"),
}

# Specific capability ids whose skeleton points at a more precise page than its area.
_CAPABILITY_DOCS: dict[str, tuple[str, str]] = {
    "providers.custom_adapter": (
        "architecture/provider-contract.md",
        "Provider contract (BaseProvider)",
    ),
    "memory.pgvector": ("usage/vector-memory-adapters.md", "Vector memory adapters"),
    "memory.qdrant": ("usage/vector-memory-adapters.md", "Vector memory adapters"),
    "memory.faiss": ("usage/vector-memory-adapters.md", "Vector memory adapters"),
    "memory.extraction_hook": ("usage/extending.md", "Memory lifecycle: extraction"),
    "hooks.memory_extraction": ("usage/extending.md", "Memory lifecycle: extraction"),
    "extensions.skill_files": ("usage/skills.md", "Skills"),
    "extensions.subagent_files": ("usage/deep-agent.md", "Deep agent and subagents"),
    "execution.deep_agent": ("usage/deep-agent.md", "Deep agent"),
    "execution.coordinator": ("usage/deep-agent.md", "Coordinator mode"),
    "execution.directed_workflow": ("usage/workflows.md", "Directed workflows"),
    "execution.routine": ("usage/loop-runner.md", "Outer loop runner (cron, CI, webhooks)"),
    "observation.vendor_exporter": ("usage/production.md", "OTel exporters"),
}


def _sdk_doc_links(ir: CompilerIR) -> list[tuple[str, str]]:
    """Resolve the selected capabilities to (doc_path, label) pairs to learn the SDK.

    A specific-capability entry wins over its area default. Deduped by page and
    sorted so the same design always emits the same section.
    """
    resolved: dict[str, str] = {}
    for capability in ir.selected_capabilities:
        entry = _CAPABILITY_DOCS.get(capability)
        if entry is None:
            entry = _AREA_DOCS.get(capability.split(".", 1)[0])
        if entry is None:
            continue
        path, label = entry
        resolved.setdefault(path, label)
    return sorted(resolved.items())


def _routine_handoff(ir: CompilerIR) -> str:
    """Render concrete host responsibilities from IR, never model-generated prose."""

    if not ir.routines:
        return (
            "No generated routine wrapper is selected. The host invokes `build_agent()` directly."
        )
    trigger_by_id = {item.id: item for item in ir.triggers}
    lines: list[str] = []
    for routine in ir.routines:
        if not routine.triggers:
            lines.append(
                f"- `{routine.id}` is manual-only; call `run_{routine.id}_once()` from your host."
            )
            continue
        for trigger_id in routine.triggers:
            trigger = trigger_by_id[trigger_id]
            wrapper = f"trigger_{routine.id}_from_{trigger.id}"
            lines.append(
                f"- Host entry point: `src/{ir.package}/integrations/triggers.py:{wrapper}`. "
                "The host supplies the raw trigger input as `payload` and a stable delivery ID "
                "when "
                "available; Studio does not fetch CI diffs, start cron, or authenticate deliveries."
            )
        if routine.kind == "workflow_run":
            lines.append(
                f"- `{routine.id}` invokes `src/{ir.package}/workflows/{routine.target}.py` "
                "once per "
                "host delivery using one shared RunBudget."
            )
        if routine.done_when is not None and routine.done_when.kind == "custom_todo":
            lines.append(
                f"- Blocking completion seam: implement `{routine.done_when.id}` in "
                f"`src/{ir.package}/completion.py:GeneratedVerifier.verify`. Until then the "
                "routine returns `done=False`; this is a runtime-readiness blocker, not a reason "
                "to invent a "
                "successful human approval."
            )
    return "\n".join(lines)


def _todos(ir: CompilerIR) -> list[tuple[str, str]]:
    caps = ir.capabilities()
    todos: list[tuple[str, str]] = []
    if ir.provider.kind == "custom":
        todos.append(
            (
                f"src/{ir.package}/integrations/custom_provider.py:CustomProvider",
                "implement the provider transport",
            )
        )
    for tool in ir.tools:
        todos.append(
            (
                f"src/{ir.package}/tools/{tool.id}.py:{tool.id}",
                "implement the integration; keep errors explicit until complete",
            )
        )
    for verifier in (
        *ir.completion.verifiers,
        *(
            item
            for routine in ir.routines
            for item in (routine.verify, routine.done_when)
            if item is not None
        ),
    ):
        if verifier.kind == "custom_todo":
            todos.append(
                (
                    f"src/{ir.package}/completion.py:GeneratedVerifier.verify",
                    f"implement custom verifier {verifier.id}; it intentionally stops until then",
                )
            )
    if caps["externalDatabase"]["enabled"]:
        todos.append(
            (
                f"src/{ir.package}/integrations/database.py:create_database_client",
                "connect the production database and define ownership",
            )
        )
    skeleton_flags = [
        (caps["prompt"]["customDynamicPolicy"], "custom dynamic prompt/context policy"),
        (caps["reliability"]["customTokenEstimator"], "custom token estimator"),
        (caps["reliability"]["customRecoveryPolicy"], "custom recovery policy"),
        (caps["compaction"]["strategy"] == "custom", "custom compaction strategy"),
        (caps["context"]["customBuilder"], "custom context builder"),
        (caps["context"]["dynamicToolSelector"], "dynamic tool selector"),
        (caps["memory"]["extractionHook"], "memory extraction hook"),
        (caps["hooks"]["customMiddleware"], "custom hook/middleware"),
        (caps["hooks"]["stopPredicateSkeleton"], "stop predicate"),
        (caps["observation"]["vendorExporter"] is not None, "vendor telemetry exporter"),
        (caps["evals"]["domainScorerSkeleton"], "domain eval scorer"),
    ]
    for enabled, label in skeleton_flags:
        if enabled:
            todos.append(
                (
                    f"src/{ir.package}/integrations/custom_reliability.py:{_todo_symbol(label)}",
                    f"implement {label}",
                )
            )
    for adapter in caps["externalDatabase"]["adapterTemplates"]:
        todos.append(
            (
                f"src/{ir.package}/integrations/{adapter}.py",
                f"implement and contract-test {adapter}",
            )
        )
    return todos


def _skeleton_modules(ir: CompilerIR) -> list[FileContribution]:
    caps = ir.capabilities()
    files: list[FileContribution] = []
    needs_reliability = any(
        [
            caps["prompt"]["customDynamicPolicy"],
            caps["reliability"]["customTokenEstimator"],
            caps["reliability"]["customRecoveryPolicy"],
            caps["compaction"]["strategy"] == "custom",
            caps["context"]["customBuilder"],
            caps["context"]["dynamicToolSelector"],
            caps["memory"]["extractionHook"],
            caps["hooks"]["customMiddleware"],
            caps["hooks"]["stopPredicateSkeleton"],
            caps["observation"]["vendorExporter"] is not None,
            caps["evals"]["domainScorerSkeleton"],
        ]
    )
    if needs_reliability:
        files.append(
            file(
                f"src/{ir.package}/integrations/custom_reliability.py",
                '''\
"""Selected custom-policy skeletons. Implement before production use."""

from __future__ import annotations

from typing import Any


class CustomCompaction:
    id = "custom-todo"

    async def compact(self, context: Any, provider: Any) -> list[Any]:
        raise NotImplementedError("TODO: implement CustomCompaction.compact")


def custom_token_estimator(messages: list[Any], model: str | None = None) -> int:
    raise NotImplementedError("TODO: implement custom_token_estimator")


def custom_dynamic_prompt_policy(context: Any) -> Any:
    raise NotImplementedError("TODO: implement custom_dynamic_prompt_policy")


def custom_recovery_policy(error: BaseException) -> Any:
    raise NotImplementedError("TODO: implement custom_recovery_policy")


def custom_context_builder(context: Any) -> Any:
    raise NotImplementedError("TODO: implement custom_context_builder")


def dynamic_tool_selector(context: Any) -> list[str]:
    raise NotImplementedError("TODO: implement dynamic_tool_selector")


def memory_extraction_hook(context: Any) -> Any:
    raise NotImplementedError("TODO: implement memory_extraction_hook")


def custom_middleware(context: Any) -> Any:
    raise NotImplementedError("TODO: implement custom_middleware")


def stop_predicate(result: Any) -> bool:
    raise NotImplementedError("TODO: implement stop_predicate")


def build_vendor_exporter() -> Any:
    raise NotImplementedError("TODO: implement build_vendor_exporter")


def domain_scorer(output: Any) -> bool:
    raise NotImplementedError("TODO: implement domain_scorer")
''',
                "reliability.custom.skeleton",
            )
        )
    memory_backend = caps["memory"]["backend"]
    if memory_backend in {"faiss", "pgvector", "qdrant", "custom"}:
        files.append(
            file(
                f"src/{ir.package}/integrations/custom_memory.py",
                '''\
"""MemoryStore adapter skeleton; see DEVELOPMENT.md for contract requirements."""

from __future__ import annotations

from typing import Any


class CustomMemoryStore:
    async def search(self, query: str, **kwargs: Any) -> list[Any]:
        raise NotImplementedError("TODO: implement CustomMemoryStore.search")

    async def upsert(self, items: list[Any], **kwargs: Any) -> None:
        raise NotImplementedError("TODO: implement CustomMemoryStore.upsert")
''',
                f"memory.{memory_backend}.skeleton",
            )
        )
    if caps["filesystem"]["backend"] == "external":
        files.append(
            file(
                f"src/{ir.package}/integrations/custom_filesystem.py",
                '"""TODO: implement a public FileBackend-compatible adapter."""\n',
                "filesystem.external.skeleton",
            )
        )
    return files


def _todo_symbol(label: str) -> str:
    return {
        "custom dynamic prompt/context policy": "custom_dynamic_prompt_policy",
        "custom token estimator": "custom_token_estimator",
        "custom recovery policy": "custom_recovery_policy",
        "custom compaction strategy": "CustomCompaction",
        "custom context builder": "custom_context_builder",
        "dynamic tool selector": "dynamic_tool_selector",
        "memory extraction hook": "memory_extraction_hook",
        "custom hook/middleware": "custom_middleware",
        "stop predicate": "stop_predicate",
        "vendor telemetry exporter": "build_vendor_exporter",
        "domain eval scorer": "domain_scorer",
    }.get(label, "TODO")


__all__ = ["ProjectPack"]
