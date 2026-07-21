"""Stateless, bounded Linch-backed blueprint proposal generation."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from linch import (
    Agent,
    AssistantEvent,
    BudgetEvent,
    FeatureFlags,
    FinalAnswerVerifierHook,
    InMemorySessionStore,
    OutputSchema,
    PartialAssistantEvent,
    ResultEvent,
    RunBudget,
    SystemPromptConfig,
    ThinkingBlock,
    ToolCallEndEvent,
    ToolCallStartEvent,
    Usage,
    UsageEvent,
    Verdict,
    empty_tools,
)
from pydantic import ValidationError

from linch_studio.compiler import CompilerError, compile_source
from linch_studio.spec import (
    Blueprint,
    canonical_digest,
    canonical_json,
    dump_blueprint,
    has_errors,
    sort_diagnostics,
    validate_blueprint,
)

from .config import AuthoringConfig
from .diff import semantic_diff
from .errors import (
    InvalidInstructionError,
    MalformedProposalError,
    ManualOnlyFieldError,
    ProposalBudgetError,
    ProposalGenerationError,
    ProposalTimeoutError,
    SemanticProposalError,
)
from .guards import manual_only_diagnostics
from .knowledge import KnowledgeBase
from .models import (
    MAX_TOOL_DETAIL_CHARS,
    MAX_TOOL_SUMMARY_CHARS,
    MAX_TRANSCRIPT_CHARS,
    MAX_TRANSCRIPT_MESSAGES,
    AuthoringMessage,
    AuthoringStage,
    AuthoringTurn,
    AuthoringTurnOutput,
    Proposal,
    ProposalTelemetry,
    TelemetryStatus,
    ToolCallRecord,
    ToolCallUpdate,
)
from .providers import create_authoring_provider
from .repository import InMemoryProposalRepository
from .tools import AuthoringDeps, ReadSectionTool, SearchDocsTool

MAX_INSTRUCTION_CHARS = 32_768

AUTHORING_SYSTEM_PROMPT = """You are Linch Studio's stateless v1alpha2 blueprint architect.

Translate the user's latest intent into one complete Linch Blueprint. The complete current
blueprint is provided on every request and is the only prior context. Preserve every setting the
instruction does not ask to change. Inspect existing identifiers before naming anything; preserve
references and make every new ID unique under the strict schema.

Keep four product axes independent:
1. Agent loop preset: standard_agent, deep_agent, or coordinator.
2. Directed workflow: code owns a static acyclic plan, journal, and replay.
3. Completion: agent_judged or verifier_gated feedback with bounded retries.
4. Routine: a manual/cron/CI/webhook wrapper whose host owns process lifetime.

When intent calls for multiple specialists, define complete subagents with bounded responsibilities;
they inherit the runtime turn ceiling and share the runtime RunBudget. For a directed workflow, emit
kind directed with agent_call nodes, deliberate dependsOn/subagent/tools relations, and exactly one
terminal output. Do not simulate unsupported branches, direct-tool workflow nodes, agent-to-agent
edges, hook control-flow edges, or cross-workflow dependencies.

You may select deterministic text_contains or json_schema verifiers. For project-specific
verification, select custom_todo, which must remain a visibly blocking implementation seam. Never
add Python, shell, callable/module paths, executable verifier code, MCP commands, secret values, or
dangerous permission grants.

The request's currentDiagnostics lists the draft's unresolved validation errors: your candidate
must design every one of them away. Ask about an unset provider or model in the chat stage; in
the build stage nothing may stay unset — when the user never chose one, set a sensible default
(such as openai_chat with a current model) and disclose that in the summary. In a routine
triggered by cron, webhook, or CI — a headless routine — never declare shell or exec tools: no
permission mode you may set can approve them there, and an unresolved write-scope tool blocks
the same way. A build-stage candidate with a headless routine must satisfy all of these at once:
every declared tool has scope read, permissions and redaction stay copied unchanged, provider
and model are set, and every write or execute step becomes a blocking custom_todo verifier for
the human. A tool's kind is always exactly one of function, class, or database — catalog
capability IDs such as function_skeleton are tier labels, never kind values, and custom_todo is
never a tool.

Conversation protocol: every reply is exactly one of three shapes through the required output
schema — clarifying questions, a plan, or one complete blueprint. Every reply must be exactly
this JSON envelope, with the unused shapes null:
{"questions": [{"question": "...", "options": ["...", "..."]}] | null, "plan": "..." | null,
"note": "..." | null, "blueprint": {complete LinchProject object} | null, "summary": "..." | null}
The blueprint always goes inside that envelope's blueprint field, never at the top level.
Null every unused field. On the
first turn of a conversation (the transcript has no assistant message yet), reply with up to five
clarifying questions plus a short note; give every question two to four concrete answer options
(the interface adds its own free-form option). Once the user has answered, lay out a short
numbered plan in plan and stop: while the request stage is "chat" the user has not approved a
plan yet, so never return a blueprint. Only when the request stage is "build" has the user
approved the plan — then return the complete blueprint and describe what you built plus any open
TODOs in summary.

Knowledge: use the read-only search_docs and read_section tools over the documentation map below
before inventing anything. The capability catalog (catalog/capabilities.md) defines what is
allowed; the documentation explains how and why. The complete blueprint YAML shape lives in
studio/blueprint.md as full worked examples — read those when building; the SDK docs describe the
SDK, not the blueprint format, so never hunt there for spec fields. Never fabricate capability
IDs, fields, or relations the catalog does not list. Consult the documentation sparingly — every
tool round-trip spends one of your limited turns. Read only the few sections you actually need,
never re-read a section you have already seen, and the moment the design is clear reply with the
required JSON turn instead of another tool call.

When proposing, return the entire studio.linch.dev/v1alpha2 LinchProject object in the blueprint
field. Do not return a patch or prose. Do not change redaction regular expressions, local MCP
stdio command or argument fields, trusted permission mode, or broad write/exec allow rules; those
are manual-only. Copy the current blueprint's permissions and redaction sections into your
candidate unchanged — even when the design needs shell or write access, declare the tools and
leave permission widening to the human. This is a proposal for human review. Never claim it has
been accepted, saved, run, or deployed.
"""

ProviderFactory = Callable[[AuthoringConfig], Any]


@dataclass(frozen=True, slots=True)
class AgentBuildRequest:
    config: AuthoringConfig
    provider: Any = field(repr=False)
    output_schema: OutputSchema
    verifier: Any = field(repr=False)
    system_prompt: str
    deps: AuthoringDeps | None


AgentFactory = Callable[[AgentBuildRequest], Any]


def create_authoring_agent(request: AgentBuildRequest) -> Agent:
    """Build the bounded design agent: two read-only knowledge tools, nothing else."""

    return Agent(
        provider=request.provider,
        model=request.config.model,
        tools=empty_tools(SearchDocsTool(), ReadSectionTool()),
        deps=request.deps,
        permissions={"mode": "skip-dangerous"},
        session_store=InMemorySessionStore(),
        system_prompt_config=SystemPromptConfig(
            append=request.system_prompt,
            replace_defaults=True,
        ),
        max_retries=2,
        max_output_tokens=request.config.max_output_tokens,
        max_turns=request.config.max_turns,
        budget=RunBudget(max_tokens=request.config.token_budget),
        # Surface thinking deltas as they stream; the assembled message the
        # capture reads is unchanged by this.
        include_partial_messages=True,
        features=FeatureFlags(skills=False, subagents=False, mcp=False, filesystem=False),
        output_schema=request.output_schema,
        structured_output_retries=2,
        loop_guard=None,
        filesystem=None,
        result_offload=None,
        hooks=[FinalAnswerVerifierHook(request.verifier, max_retries=2)],
        read_before_write=False,
        mcp_servers=None,
        extra_subagents=[],
        enable_worker_tools=False,
        retain_subagents=False,
        enable_background_subagents=False,
        enable_task_stop=False,
        enable_background_tools=False,
        mailbox=None,
        schedule_store=None,
    )


def _assemble_system_prompt(knowledge: KnowledgeBase) -> str:
    return (
        AUTHORING_SYSTEM_PROMPT
        + "\nDocumentation map (each line is a read_section path):\n"
        + knowledge.toc_prompt()
    )


def _existing_identifiers(current: Blueprint) -> dict[str, Any]:
    spec = current.spec
    routines = getattr(spec, "routines", getattr(spec, "loops", ()))
    return {
        "tools": [item.id for item in spec.tools],
        "subagents": [item.id for item in spec.subagents],
        "skills": [item.id for item in spec.skills],
        "workflows": [item.id for item in spec.workflows],
        "workflowNodes": {
            workflow.id: [node.id for node in workflow.nodes] for workflow in spec.workflows
        },
        "routines": [item.id for item in routines],
        "triggers": [item.id for item in spec.triggers],
    }


def build_proposal_prompt(current: Blueprint, instruction: str) -> str:
    """Build the full, self-contained request with no conversational dependency."""

    return (
        "Create one reviewable full-blueprint proposal from this self-contained request:\n"
        '{"currentBlueprint":'
        + canonical_json(current)
        + ',"existingIdentifiers":'
        + json.dumps(
            _existing_identifiers(current),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + ',"instruction":'
        + json.dumps(instruction, ensure_ascii=False)
        + "}"
    )


def build_conversation_prompt(
    current: Blueprint,
    messages: Sequence[AuthoringMessage],
    stage: AuthoringStage = "chat",
) -> str:
    """Build one self-contained request carrying the whole client-held transcript.

    Args:
        current: The blueprint the conversation is editing.
        messages: The full client-held transcript, oldest first.
        stage: "chat" while the user is still answering/approving; "build" once
            they accepted the plan and the reply must be a blueprint.
    """

    transcript = [{"content": item.content, "role": item.role} for item in messages]
    return (
        "Continue this design conversation; reply with clarifying questions, a plan, or one "
        "complete blueprint proposal:\n"
        '{"currentBlueprint":'
        + canonical_json(current)
        + ',"currentDiagnostics":'
        + json.dumps(
            _outstanding_errors(current),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + ',"existingIdentifiers":'
        + json.dumps(
            _existing_identifiers(current),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + ',"stage":'
        + json.dumps(stage)
        + ',"transcript":'
        + json.dumps(transcript, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "}"
    )


def _outstanding_errors(current: Blueprint) -> list[dict[str, str]]:
    """The draft's unresolved validation errors, so the agent designs them away
    instead of rediscovering them when its candidate is rejected."""

    return [
        {"code": item.code, "message": item.message, "path": item.path}
        for item in sort_diagnostics(validate_blueprint(current))
        if item.severity == "error"
    ][:12]


def _concise_findings(findings: Sequence[Any]) -> str:
    """Render retry feedback the model can act on: code, path, and the value-free
    diagnostic message — a bare code is not enough to stop a provider from
    re-proposing the same forbidden change."""

    return "; ".join(f"{item.code} at {item.path}: {item.message[:200]}" for item in findings[:6])


def _compile_diagnostics(candidate: Blueprint) -> tuple[Any, ...]:
    """Dry-run the deterministic compiler; pure text generation, nothing executes."""
    try:
        compile_source(dump_blueprint(candidate).encode("utf-8"))
    except CompilerError as error:
        return error.diagnostics
    return ()


class _TurnVerifier:
    name = "linch_studio_authoring_turn"

    def __init__(
        self,
        current: Blueprint,
        *,
        require_questions: bool,
        stage: AuthoringStage,
    ) -> None:
        self.current = current
        self.require_questions = require_questions
        self.stage = stage

    def verify(self, context: Any) -> Verdict:
        raw = context.structured_output
        if raw is None:
            # The independent structured-output gate owns malformed JSON/schema
            # retries. Semantic retries must not multiply that retry budget.
            return Verdict()
        try:
            turn = AuthoringTurnOutput.model_validate(raw, strict=True)
        except ValidationError:
            return Verdict(
                action="retry",
                feedback=(
                    "Return one turn matching the required schema: set exactly one of "
                    "questions, plan, or blueprint and null every unused field."
                ),
                reason="turn_structure",
            )
        set_shapes = sum(
            1 for value in (turn.questions, turn.plan, turn.blueprint) if value is not None
        )
        if set_shapes != 1:
            return Verdict(
                action="retry",
                feedback=(
                    "Set exactly one of questions, plan, or blueprint; null the fields "
                    "you are not using."
                ),
                reason="turn_shape",
            )
        if self.stage == "chat":
            if self.require_questions and turn.questions is None:
                return Verdict(
                    action="retry",
                    feedback=(
                        "This is the first turn of the conversation: reply with up to five "
                        "clarifying questions, each carrying two to four concrete options, "
                        "instead of a plan or blueprint."
                    ),
                    reason="questions_first",
                )
            if turn.blueprint is not None:
                return Verdict(
                    action="retry",
                    feedback=(
                        "Do not build yet: the user has not accepted a plan. Present the "
                        "plan and wait for them to approve it before returning a blueprint."
                    ),
                    reason="plan_approval",
                )
            return Verdict()
        if turn.blueprint is None:
            return Verdict(
                action="retry",
                feedback=(
                    "The user approved the plan: return the one complete blueprint that "
                    "implements the approved design, not more questions or planning."
                ),
                reason="build_required",
            )
        diagnostics = _candidate_diagnostics(self.current, turn.blueprint)
        errors = [item for item in diagnostics if item.severity == "error"]
        if errors:
            return Verdict(
                action="retry",
                feedback=(
                    "Return a corrected complete blueprint. Resolve these validation "
                    "findings: " + _concise_findings(errors)
                ),
                reason="blueprint_semantics",
            )
        compile_errors = _compile_diagnostics(turn.blueprint)
        if compile_errors:
            return Verdict(
                action="retry",
                feedback=(
                    "The blueprint fails deterministic compilation. Resolve: "
                    + _concise_findings(compile_errors)
                ),
                reason="blueprint_compile",
            )
        return Verdict()


@dataclass(slots=True)
class _RunCapture:
    result: ResultEvent | None = None
    usage: Usage = field(default_factory=Usage)
    budget_exceeded: bool = False
    thinking: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    # in-flight starts, keyed by tool_use_id, until their matching end arrives
    _pending_tool_calls: dict[str, ToolCallStartEvent] = field(default_factory=dict)

    def joined_thinking(self) -> str | None:
        """The full reasoning trace across the run's turns, never truncated."""

        text = "\n\n".join(part for part in self.thinking if part.strip())
        return text or None


class LinchProposalService:
    """Generate candidates without retaining conversation or mutating project state."""

    def __init__(
        self,
        config: AuthoringConfig,
        *,
        repository: InMemoryProposalRepository | None = None,
        record_proposals: bool = True,
        provider_factory: ProviderFactory = create_authoring_provider,
        agent_factory: AgentFactory = create_authoring_agent,
        id_factory: Callable[[], str] | None = None,
        knowledge: KnowledgeBase | None = None,
    ) -> None:
        self.config = config
        self.repository = repository or InMemoryProposalRepository()
        self._record_proposals = record_proposals
        self._provider_factory = provider_factory
        self._agent_factory = agent_factory
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._knowledge = knowledge if knowledge is not None else KnowledgeBase()
        self._deps = AuthoringDeps(knowledge=self._knowledge)
        self._system_prompt = _assemble_system_prompt(self._knowledge)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> LinchProposalService:
        return cls(AuthoringConfig.from_env(environ), **kwargs)

    async def propose(self, current: Blueprint, instruction: str) -> Proposal:
        """One-shot legacy entry point: a single instruction must yield a proposal."""
        instruction = instruction.strip()
        if not instruction:
            raise InvalidInstructionError("An authoring instruction is required.")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise InvalidInstructionError(
                f"Authoring instructions are limited to {MAX_INSTRUCTION_CHARS} characters."
            )
        turn = await self._run_turn(
            current,
            build_proposal_prompt(current, instruction),
            require_questions=False,
            stage="build",
        )
        if turn.kind != "proposal" or turn.proposal is None:
            raise ProposalGenerationError(
                "The authoring model asked follow-up questions; use the conversational "
                "authoring endpoint to answer them.",
                telemetry=turn.telemetry,
            )
        return turn.proposal

    async def converse(
        self,
        current: Blueprint,
        messages: Sequence[AuthoringMessage],
        *,
        stage: AuthoringStage = "chat",
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> AuthoringTurn:
        """Run one conversational turn over the client-held transcript.

        In the chat stage the first turn of a conversation (no assistant message
        yet) must come back as clarifying questions; later turns ask again or
        present a plan, never a blueprint. The build stage — entered when the
        user approves the plan — must return one full reviewable Proposal.
        Nothing is stored for question- or plan-kind turns.

        Args:
            current: The blueprint the conversation is editing.
            messages: The full client-held transcript, oldest first.
            stage: "chat" until the user approves a plan, then "build".
            on_thinking: Optional non-blocking callback invoked with each
                reasoning delta as the provider streams it; display-only.
            on_tool_call: Optional non-blocking callback invoked once when a
                knowledge-tool call starts and once when it ends; display-only.

        Raises:
            InvalidInstructionError: when the transcript is empty, over the
                message/char caps, or does not end with a user message.
        """
        if not messages:
            raise InvalidInstructionError("A conversation transcript is required.")
        if len(messages) > MAX_TRANSCRIPT_MESSAGES:
            raise InvalidInstructionError(
                f"Transcripts are limited to {MAX_TRANSCRIPT_MESSAGES} messages."
            )
        total_chars = sum(len(item.content) for item in messages)
        if total_chars > MAX_TRANSCRIPT_CHARS:
            raise InvalidInstructionError(
                f"Transcripts are limited to {MAX_TRANSCRIPT_CHARS} characters in total."
            )
        if messages[-1].role != "user":
            raise InvalidInstructionError("The transcript must end with a user message.")
        require_questions = stage == "chat" and not any(
            item.role == "assistant" for item in messages
        )
        return await self._run_turn(
            current,
            build_conversation_prompt(current, messages, stage),
            require_questions=require_questions,
            stage=stage,
            on_thinking=on_thinking,
            on_tool_call=on_tool_call,
        )

    async def _run_turn(
        self,
        current: Blueprint,
        prompt: str,
        *,
        require_questions: bool,
        stage: AuthoringStage,
        on_thinking: Callable[[str], None] | None = None,
        on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
    ) -> AuthoringTurn:
        base_digest = canonical_digest(current)
        verifier = _TurnVerifier(current, require_questions=require_questions, stage=stage)
        schema = OutputSchema(
            name="linch_studio_authoring_turn",
            schema=AuthoringTurnOutput.model_json_schema(by_alias=True),
            strict=True,
            description=(
                "One authoring turn: clarifying questions, a plan for approval, or one "
                "complete Linch Studio v1alpha2 blueprint candidate."
            ),
        )
        started = time.perf_counter()
        capture = _RunCapture()
        provider: Any = None
        agent: Any = None
        elapsed_ms = 0

        try:
            provider = self._provider_factory(self.config)
            request = AgentBuildRequest(
                config=self.config,
                provider=provider,
                output_schema=schema,
                verifier=verifier,
                system_prompt=self._system_prompt,
                deps=self._deps,
            )
            agent = self._agent_factory(request)
            if inspect.isawaitable(agent):
                agent = await agent
            await asyncio.wait_for(
                _run_once(agent, prompt, capture, on_thinking, on_tool_call),
                timeout=self.config.timeout_seconds,
            )
            elapsed_ms = _elapsed_ms(started)
        except asyncio.TimeoutError as exc:
            elapsed_ms = _elapsed_ms(started)
            raise ProposalTimeoutError(
                "The authoring model did not finish before the configured timeout.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "timeout"),
            ) from exc
        except (InvalidInstructionError, ProposalTimeoutError):
            raise
        except Exception as exc:
            elapsed_ms = _elapsed_ms(started)
            raise ProposalGenerationError(
                "The authoring model could not produce a proposal.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "runtime_error"),
                cause=type(exc).__name__,
            ) from exc
        finally:
            if agent is not None:
                await _close_quietly(agent)
            elif provider is not None:
                await _close_quietly(provider)

        if capture.budget_exceeded:
            raise ProposalBudgetError(
                "The authoring run exhausted its configured token budget.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "budget_exhausted"),
            )
        result = capture.result
        if result is None or result.subtype != "success":
            # subtype/stop_reason are closed, static vocabularies (never
            # free-form provider text) — safe to log as the failure's cause.
            cause = "no_result" if result is None else f"{result.subtype}/{result.stop_reason}"
            raise ProposalGenerationError(
                "The authoring model ended without a successful proposal.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "runtime_error"),
                cause=cause,
            )
        if result.structured_output is None:
            raise MalformedProposalError(
                "The authoring model did not return a turn matching the strict schema.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "malformed"),
            )
        try:
            output = AuthoringTurnOutput.model_validate(result.structured_output, strict=True)
        except ValidationError as exc:
            raise MalformedProposalError(
                "The authoring model did not return a turn matching the strict schema.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "malformed"),
            ) from exc

        set_shapes = sum(
            1 for value in (output.questions, output.plan, output.blueprint) if value is not None
        )
        if set_shapes != 1:
            raise MalformedProposalError(
                "The authoring model did not choose exactly one of questions, a plan, "
                "or a blueprint.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "malformed"),
            )
        if output.questions is not None:
            return AuthoringTurn(
                kind="questions",
                questions=tuple(output.questions),
                note=output.note,
                telemetry=_telemetry(self.config, elapsed_ms, capture, "questions"),
                thinking=capture.joined_thinking(),
                tool_calls=tuple(capture.tool_calls),
            )
        if output.plan is not None:
            return AuthoringTurn(
                kind="plan",
                plan=output.plan,
                note=output.note,
                telemetry=_telemetry(self.config, elapsed_ms, capture, "plan"),
                thinking=capture.joined_thinking(),
                tool_calls=tuple(capture.tool_calls),
            )

        candidate = output.blueprint
        assert candidate is not None
        diagnostics = _candidate_diagnostics(current, candidate)
        if has_errors(diagnostics):
            manual_only = any(
                item.code == "authoring.manual_only_field" and item.severity == "error"
                for item in diagnostics
            )
            status: TelemetryStatus = "manual_only" if manual_only else "semantic_invalid"
            error_type = ManualOnlyFieldError if manual_only else SemanticProposalError
            raise error_type(
                "The proposed blueprint did not pass authoring validation.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, status),
                diagnostics=diagnostics,
            )
        compile_errors = _compile_diagnostics(candidate)
        if compile_errors:
            raise SemanticProposalError(
                "The proposed blueprint did not pass the deterministic compile dry-run.",
                telemetry=_telemetry(self.config, elapsed_ms, capture, "semantic_invalid"),
                diagnostics=sort_diagnostics(compile_errors),
            )

        proposal = Proposal(
            id=self._id_factory(),
            base_digest=base_digest,
            candidate=candidate,
            diagnostics=diagnostics,
            diff=semantic_diff(current, candidate),
            telemetry=_telemetry(self.config, elapsed_ms, capture, "success"),
            summary=output.summary,
        )
        if self._record_proposals:
            self.repository.add(proposal)
        return AuthoringTurn(
            kind="proposal",
            note=output.note,
            proposal=proposal,
            telemetry=proposal.telemetry,
            thinking=capture.joined_thinking(),
            tool_calls=tuple(capture.tool_calls),
        )

    def accept(self, proposal_id: str, current: Blueprint) -> Blueprint:
        return self.repository.accept(proposal_id, current)

    def reject(self, proposal_id: str) -> None:
        self.repository.reject(proposal_id)


def _candidate_diagnostics(current: Blueprint, candidate: Blueprint) -> tuple[Any, ...]:
    return sort_diagnostics(
        (*validate_blueprint(candidate), *manual_only_diagnostics(current, candidate))
    )


async def _run_once(
    agent: Any,
    prompt: str,
    capture: _RunCapture,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_call: Callable[[ToolCallUpdate], None] | None = None,
) -> None:
    session = await agent.session()
    try:
        async for event in session.run(prompt):
            if isinstance(event, UsageEvent):
                capture.usage = event.cumulative
            elif isinstance(event, BudgetEvent) and event.kind == "exceeded":
                capture.budget_exceeded = True
            elif isinstance(event, PartialAssistantEvent):
                if on_thinking is not None and event.delta.get("kind") == "thinking":
                    on_thinking(str(event.delta.get("text", "")))
            elif isinstance(event, AssistantEvent):
                for block in event.message.content:
                    if isinstance(block, ThinkingBlock):
                        capture.thinking.append(block.thinking)
            elif isinstance(event, ToolCallStartEvent):
                capture._pending_tool_calls[event.tool_use_id] = event
                if on_tool_call is not None:
                    on_tool_call(
                        ToolCallUpdate(
                            phase="start",
                            tool_use_id=event.tool_use_id,
                            tool_name=event.tool_name,
                            summary=_bounded(event.summary, MAX_TOOL_SUMMARY_CHARS),
                        )
                    )
            elif isinstance(event, ToolCallEndEvent):
                _record_tool_call_end(event, capture, on_tool_call)
            elif isinstance(event, ResultEvent):
                capture.result = event
                capture.usage = event.total_usage
    finally:
        closer = getattr(session, "aclose", None)
        if closer is not None:
            result = closer(force=True)
            if inspect.isawaitable(result):
                await result


def _bounded(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap]


def _record_tool_call_end(
    event: ToolCallEndEvent,
    capture: _RunCapture,
    on_tool_call: Callable[[ToolCallUpdate], None] | None,
) -> None:
    start = capture._pending_tool_calls.pop(event.tool_use_id, None)
    pre_summary = _bounded(start.summary, MAX_TOOL_SUMMARY_CHARS) if start else event.tool_name
    result_text = event.tool_result.summary if event.tool_result is not None else ""
    result_summary = _bounded(result_text or event.result, MAX_TOOL_SUMMARY_CHARS) or None
    detail = _bounded(event.result, MAX_TOOL_DETAIL_CHARS) or None
    duration_ms = max(event.duration_ms, 0)
    capture.tool_calls.append(
        ToolCallRecord(
            tool_use_id=event.tool_use_id,
            tool_name=event.tool_name,
            summary=pre_summary,
            result_summary=result_summary,
            detail=detail,
            is_error=event.is_error,
            duration_ms=duration_ms,
        )
    )
    if on_tool_call is not None:
        on_tool_call(
            ToolCallUpdate(
                phase="end",
                tool_use_id=event.tool_use_id,
                tool_name=event.tool_name,
                summary=result_summary or "",
                detail=detail,
                is_error=event.is_error,
                duration_ms=duration_ms,
            )
        )


async def _close_quietly(resource: Any) -> None:
    closer = getattr(resource, "close", None) or getattr(resource, "aclose", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1_000))


def _telemetry(
    config: AuthoringConfig,
    duration_ms: int,
    capture: _RunCapture,
    status: TelemetryStatus,
) -> ProposalTelemetry:
    usage = capture.usage
    return ProposalTelemetry(
        provider=config.provider,
        model=config.model,
        duration_ms=duration_ms,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        status=status,
    )


__all__ = [
    "AUTHORING_SYSTEM_PROMPT",
    "MAX_INSTRUCTION_CHARS",
    "AgentBuildRequest",
    "AgentFactory",
    "LinchProposalService",
    "ProviderFactory",
    "build_conversation_prompt",
    "build_proposal_prompt",
    "create_authoring_agent",
]
