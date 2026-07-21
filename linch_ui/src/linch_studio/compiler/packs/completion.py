"""Goal-verification contribution pack.

The Linch verifier adapter is intentionally permissive for application-owned
verifiers. Studio-generated gates wrap that contract with explicit fail-closed
behavior and skip child sessions so only the project invocation is gated.
"""

from __future__ import annotations

import json

from ..contributions import FileContribution
from ..ir import CompilerIR, VerifierIR
from .common import file, py


class CompletionPack:
    capability_id = "completion.verifier_gated"

    def contribute(self, ir: CompilerIR) -> tuple[FileContribution, ...]:
        routine_verifiers = [
            verifier
            for routine in ir.routines
            for verifier in (routine.verify, routine.done_when)
            if verifier is not None
        ]
        all_verifiers = [*ir.completion.verifiers, *routine_verifiers]
        if not all_verifiers:
            return ()
        return (
            file(
                f"src/{ir.package}/completion.py",
                _completion_module(ir, tuple(all_verifiers)),
                "completion.verifiers",
            ),
            file(
                "tests/test_completion.py",
                _completion_tests(ir),
                "tests.completion",
            ),
        )


def _completion_module(ir: CompilerIR, verifiers: tuple[VerifierIR, ...]) -> str:
    specs = {item.id: json.loads(item.config_json) for item in verifiers}
    root_ids = [item.id for item in ir.completion.verifiers]
    has_json_schema = any(item.kind == "json_schema" for item in verifiers)
    json_import = "import json\n" if has_json_schema else ""
    json_schema_branch = (
        """\
            if kind == "json_schema":
                import jsonschema  # type: ignore[reportMissingModuleSource]

                try:
                    value = ctx.structured_output
                    if value is None:
                        value = json.loads(str(ctx.final_text or ""))
                    jsonschema.validate(value, self.spec["schema"])
                except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
                    feedback = self.spec.get("feedback") or (
                        f"Return JSON matching the schema: {exc}"
                    )
                    return self._failed(ctx, str(feedback), self.name)
                except jsonschema.SchemaError as exc:
                    return Verdict(
                        action="stop",
                        feedback=f"Invalid generated verifier schema: {exc}",
                        reason=f"{self.name}_configuration",
                    )
                return Verdict()
"""
        if has_json_schema
        else ""
    )
    return f'''\
"""Generated deterministic completion gates.

Verifier failures, verifier implementation errors, and retry exhaustion stop the
root invocation. Child subagent sessions are never gated by project completion.
"""

from __future__ import annotations

{json_import}from typing import Any

from linch import FinalAnswerVerifierHook, Verdict

MAX_RETRIES = {ir.completion.max_retries}
VERIFIER_SPECS: dict[str, dict[str, Any]] = {py(specs)}


class GeneratedVerifier:
    def __init__(self, spec: dict[str, Any], *, max_retries: int = MAX_RETRIES) -> None:
        self.spec = dict(spec)
        self.name = str(spec["id"])
        self.max_retries = max(0, int(max_retries))

    def _failed(self, ctx: Any, feedback: str, reason: str) -> Verdict:
        if int(getattr(ctx, "attempt", 0)) >= self.max_retries:
            return Verdict(action="stop", feedback=feedback, reason=reason)
        return Verdict(action="retry", feedback=feedback, reason=reason)

    def verify(self, ctx: Any) -> Verdict:
        try:
            kind = self.spec["kind"]
            if kind == "text_contains":
                expected = str(self.spec["text"])
                if expected.casefold() in str(ctx.final_text or "").casefold():
                    return Verdict()
                feedback = self.spec.get("feedback") or f"Final answer must contain: {{expected}}"
                return self._failed(ctx, str(feedback), self.name)
{json_schema_branch}            if kind == "custom_todo":
                return Verdict(
                    action="stop",
                    feedback=(
                        "TODO: implement custom verifier "
                        + self.name
                        + " in src/{ir.package}/completion.py before this result can pass."
                    ),
                    reason=f"{{self.name}}_not_implemented",
                )
            return Verdict(
                action="stop",
                feedback="Generated verifier has an unsupported kind.",
                reason=f"{{self.name}}_configuration",
            )
        except Exception as exc:
            return Verdict(
                action="stop",
                feedback=f"Verifier {{self.name}} failed internally: {{type(exc).__name__}}",
                reason=f"{{self.name}}_exception",
            )


class RootOnlyFinalAnswerVerifierHook(FinalAnswerVerifierHook):
    """Apply completion only to root sessions, never workflow/subagent children."""

    async def on_before_final_answer(self, ctx: Any) -> Any:
        session = getattr(ctx, "session", None)
        meta = getattr(session, "meta", {{}}) or {{}}
        if meta.get("parentSessionId"):
            return None
        return await super().on_before_final_answer(ctx)


def build_verifier(identifier: str, *, max_retries: int = MAX_RETRIES) -> GeneratedVerifier:
    try:
        spec = VERIFIER_SPECS[identifier]
    except KeyError as exc:
        raise ValueError(f"unknown generated verifier: {{identifier}}") from exc
    return GeneratedVerifier(spec, max_retries=max_retries)


def build_completion_hooks() -> list[object]:
    identifiers = {py(root_ids)}
    if not identifiers:
        return []
    verifiers = [build_verifier(identifier) for identifier in identifiers]
    return [RootOnlyFinalAnswerVerifierHook(verifiers, max_retries=MAX_RETRIES)]


def verify_result(identifier: str, result: Any) -> bool:
    """Evaluate a routine invocation result once; configuration/errors fail closed."""
    verifier = build_verifier(identifier, max_retries=0)
    context = type(
        "RoutineVerificationContext",
        (),
        {{
            "final_text": getattr(
                result, "final_text", result if isinstance(result, str) else None
            ),
            "structured_output": getattr(result, "structured_output", None),
            "structured_error": getattr(result, "structured_error", None),
            "attempt": 0,
        }},
    )()
    return verifier.verify(context).action == "pass"
'''


def _completion_tests(ir: CompilerIR) -> str:
    has_json_schema = any(
        verifier.kind == "json_schema"
        for verifier in (
            *ir.completion.verifiers,
            *(
                item
                for routine in ir.routines
                for item in (routine.verify, routine.done_when)
                if item is not None
            ),
        )
    )
    json_tests = (
        """\


def test_json_schema_uses_structured_output_or_parsed_final_text() -> None:
    spec = {
        "kind": "json_schema",
        "id": "json_gate",
        "schema": {
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    }
    verifier = GeneratedVerifier(spec, max_retries=1)
    assert verifier.verify(_ctx(structured={"status": "ok"})).action == "pass"
    assert verifier.verify(_ctx('{"status":"ok"}')).action == "pass"
    assert verifier.verify(_ctx("not-json")).action == "retry"
    assert verifier.verify(_ctx("not-json", attempt=1)).action == "stop"


def test_invalid_generated_json_schema_stops() -> None:
    verifier = GeneratedVerifier(
        {
            "kind": "json_schema",
            "id": "broken_schema",
            "schema": {"type": "not-a-json-schema-type"},
        },
        max_retries=1,
    )
    verdict = verifier.verify(_ctx(structured={}))
    assert verdict.action == "stop"
    assert verdict.reason == "broken_schema_configuration"
"""
        if has_json_schema
        else ""
    )
    return f'''\
"""Offline tests for generated fail-closed, root-only completion gates."""

from types import SimpleNamespace

from {ir.package}.completion import (
    GeneratedVerifier,
    RootOnlyFinalAnswerVerifierHook,
    build_verifier,
)


def _ctx(text: str = "", *, structured=None, attempt: int = 0):
    return SimpleNamespace(
        final_text=text,
        structured_output=structured,
        structured_error=None,
        attempt=attempt,
    )


def test_text_contains_passes_case_insensitively_then_retries_and_stops() -> None:
    verifier = GeneratedVerifier(
        {{"kind": "text_contains", "id": "done_gate", "text": "DONE"}},
        max_retries=1,
    )
    assert verifier.verify(_ctx("work done")).action == "pass"
    assert verifier.verify(_ctx("unfinished")).action == "retry"
    assert verifier.verify(_ctx("unfinished", attempt=1)).action == "stop"


def test_unexpected_verifier_exception_stops() -> None:
    class BrokenContext:
        attempt = 0

        @property
        def final_text(self):
            raise RuntimeError("boom")

    verifier = GeneratedVerifier(
        {{"kind": "text_contains", "id": "broken", "text": "DONE"}},
        max_retries=1,
    )
    verdict = verifier.verify(BrokenContext())
    assert verdict.action == "stop"
    assert verdict.reason == "broken_exception"


def test_custom_todos_never_fake_pass() -> None:
    module = __import__("{ir.package}.completion", fromlist=["VERIFIER_SPECS"])
    for identifier, spec in module.VERIFIER_SPECS.items():
        if spec["kind"] == "custom_todo":
            assert build_verifier(identifier).verify(_ctx()).action == "stop"


async def test_completion_hook_skips_child_sessions() -> None:
    hook = RootOnlyFinalAnswerVerifierHook([], max_retries=0)
    ctx = SimpleNamespace(session=SimpleNamespace(meta={{"parentSessionId": "parent"}}))
    assert await hook.on_before_final_answer(ctx) is None
{json_tests}'''


__all__ = ["CompletionPack"]
