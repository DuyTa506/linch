from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass, replace
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from ._telemetry import _TelemetryProvider, trace_compaction
from ._token_estimation import custom_token_estimate
from .abort import AbortContext
from .catalog import snapshot_enabled, snapshot_for, snapshot_skill_listing
from .errors import ConfigError, ProviderError
from .events import CompactionEvent
from .model_calls import (
    _AccountedAuxiliaryCaller,
    _AuxiliaryDelivery,
    _context_fingerprint,
    _model_call_fingerprint,
    _ModelCallError,
    _ModelCallLedger,
    _projection_record,
)
from .providers.limiter import provider_slot
from .run_store import LeaseLostError, durable_call
from .types import (
    CatalogChangeBlock,
    Message,
    ProviderRequest,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    message_to_dict,
)


@dataclass
class CompactionContext:
    messages: list[Message]
    model: str
    signal: AbortContext
    auxiliary_caller: _AccountedAuxiliaryCaller | None = None
    logical_call_id: str | None = None


@dataclass(slots=True)
class CompactionLadder:
    """Opt-in LLM-free recovery rungs that run before full summarization.

    Pass via ``Agent(compaction_ladder=CompactionLadder())``.  When unset
    (default ``None``) the loop behaves exactly as before.

    Attributes:
        micro: Enable micro-compact — elide old tool-result contents (no LLM
            call) both proactively (in :func:`maybe_compact`) and reactively
            (on ``ContextLengthError``, once per turn).
        keep_recent_turns: Tool results within the last N assistant turns are
            never elided.
        max_forced_compactions: Per-run circuit breaker on forced (LLM)
            compactions triggered by ``ContextLengthError``; once exhausted
            the error surfaces.
        reset_read_tracker: After any compaction (micro or forced) elides or
            summarizes away earlier messages, clear ``session.file_read_tracker``
            so a file whose contents left the context is re-read before it can
            be edited (the ``Edit`` tool gates on ``has_read``). Cheap insurance
            against blind edits on stale content; worst case is a redundant
            re-read of a file still present in the kept recent tail.

        Args:
            micro: Whether to elide old tool-result contents before summarization.
            keep_recent_turns: Number of recent assistant turns protected from elision.
            max_forced_compactions: Maximum forced LLM compactions allowed per run.
            reset_read_tracker: Whether compaction clears the file-read tracker.
    """

    micro: bool = True
    keep_recent_turns: int = 10
    max_forced_compactions: int = 3
    reset_read_tracker: bool = True

    def __post_init__(self) -> None:
        if self.keep_recent_turns < 1:
            raise ValueError("compaction_ladder.keep_recent_turns must be >= 1")


_ELIDED = "[tool result elided to save context]"


def micro_compact(
    messages: list[Message], *, keep_recent_turns: int = 10
) -> tuple[list[Message], int]:
    """Return ``(new_messages, n_elided)`` with old tool-result contents elided.

    Pure and copy-on-write: never mutates *messages* or their blocks (they are
    shared with ``session.full_history``); untouched messages are reused by
    identity.  Only ``ToolResultBlock`` contents older than the last
    *keep_recent_turns* assistant turns are replaced, so every ``tool_use_id``
    stays paired and the message structure remains provider-valid.  No LLM call.
    """
    if keep_recent_turns < 1:
        raise ValueError("micro_compact.keep_recent_turns must be >= 1")
    recent = last_n_turn_boundaries(messages, keep_recent_turns)
    boundary = len(messages) - len(recent)
    if boundary <= 0:
        return messages, 0

    n_elided = 0
    out: list[Message] = []
    for i, message in enumerate(messages):
        if i >= boundary:
            out.append(message)
            continue
        new_blocks: list[Any] | None = None
        for j, block in enumerate(message.content):
            if not isinstance(block, ToolResultBlock):
                continue
            content = block.content
            worthwhile = (isinstance(content, str) and len(content) > len(_ELIDED)) or (
                isinstance(content, list) and len(content) > 0
            )
            if not worthwhile or content == _ELIDED:
                continue
            if new_blocks is None:
                new_blocks = list(message.content)
            new_blocks[j] = ToolResultBlock(
                tool_use_id=block.tool_use_id,
                content=_ELIDED,
                is_error=block.is_error,
            )
            n_elided += 1
        if new_blocks is None:
            out.append(message)
        else:
            out.append(
                Message(
                    role=message.role,
                    content=new_blocks,
                    provider_metadata=message.provider_metadata,
                )
            )

    if n_elided == 0:
        return messages, 0
    return out, n_elided


def apply_micro_compaction(
    session: Any,
    agent: Any,
    *,
    keep_recent_turns: int,
    model: str | None = None,
) -> bool:
    """Elide old tool results in ``session.provider_view`` via a logged projection.

    Args:
        session: Session whose provider view is being compacted.
        agent: Agent supplying the model and token estimator.
        keep_recent_turns: Number of recent assistant turns protected from elision.
        model: Optional model override for token estimation.

    Returns:
        Whether any results were elided. On success, sets ``last_compaction_info``
        with strategy ``"micro"`` and clears provider continuation IDs.
    """
    resolved_model = _resolve_compaction_model(session, agent, model)
    # One snapshot for the whole pre-compaction pass: each read is a detached
    # deep copy, so re-reading the projection per use is O(history) each time.
    current_view = list(session.provider_view)
    tokens_before = _estimate_tokens(agent, current_view, model=resolved_model)
    new_view, n_elided = micro_compact(current_view, keep_recent_turns=keep_recent_turns)
    if n_elided == 0:
        return False
    messages_count = len(current_view)
    new_view = strip_response_chaining(new_view)
    session.session_log.record_projection(new_view, reason="micro-compaction")
    session.last_compaction_info = {
        "type": "compaction",
        "messages_before": messages_count,
        "messages_after": messages_count,
        "tokens_before": tokens_before,
        # ``new_view`` is what the projection now holds — no second snapshot.
        "tokens_after": _estimate_tokens(agent, new_view, model=resolved_model),
        "strategy": "micro",
        "model": resolved_model,
    }
    return True


def reset_read_tracker_after_compaction(session: Any, agent: Any) -> None:
    """Clear ``session.file_read_tracker`` after a compaction, if opted in.

    A compaction (micro elision or forced summary) can remove a file's contents
    from the provider view while the read tracker still records it as read. The
    ``Edit`` tool gates on ``has_read``, so a stale entry would permit a blind
    edit on content the model can no longer see. Resetting the tracker forces a
    fresh ``Read`` before the next edit.

    Opt-in via ``CompactionLadder(reset_read_tracker=True)`` (the ladder default);
    a no-op when no ladder is configured, so default behavior is byte-identical.
    """
    ladder = getattr(agent, "compaction_ladder", None)
    if ladder is None or not getattr(ladder, "reset_read_tracker", True):
        return
    tracker = getattr(session, "file_read_tracker", None)
    if tracker is not None:
        tracker.clear()


@runtime_checkable
class CompactionStrategy(Protocol):
    id: str

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        """Compact *ctx.messages* using *provider* (a :class:`~linch.providers.BaseProvider`)."""
        ...


def last_n_turn_boundaries(messages: list[Message], n: int) -> list[Message]:
    # Defensive behavior for direct/internal callers.  Public compaction
    # configs reject n < 1, but returning the complete view here is the only
    # safe fallback: the old `messages[-1:]` result could retain a trailing
    # ToolResultBlock while dropping its matching ToolUseBlock.
    if n < 1:
        return messages

    assistants_found = 0
    boundary_idx = -1

    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "assistant":
            assistants_found += 1
            if assistants_found == n:
                boundary_idx = i
                break

    if assistants_found < n:
        return messages

    return messages[boundary_idx:]


_SUMMARY_PROMPT = (
    "Summarize the conversation so far. Capture:\n"
    "- Decisions made\n"
    "- Files read, written, or edited (with paths)\n"
    "- Tool calls made and their outcomes\n"
    "- Open questions and current task state\n"
    "Keep it factual and dense. Omit greetings, restated goals, and meta commentary."
)

#: Domain-neutral alternative to :data:`_SUMMARY_PROMPT` for non-coding hosts.
#: linch is embeddable, so its built-in default leans coding (file paths, tool
#: calls). Pass this to a strategy — ``DefaultCompaction(prompt=GENERAL_SUMMARY_PROMPT)``
#: — when the host is not a software-engineering agent and the file/tool framing
#: would just be noise. Part of the public API.
GENERAL_SUMMARY_PROMPT = (
    "Summarize the conversation so far. Capture:\n"
    "- Decisions made and conclusions reached\n"
    "- Key facts, entities, and data established (with identifiers)\n"
    "- Actions taken and their outcomes\n"
    "- Open questions and current task state\n"
    "Keep it factual and dense. Omit greetings, restated goals, and meta commentary."
)

_DETAILED_SUMMARY_PROMPT = (
    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
    "\n"
    "Create a continuation-safe summary of the earlier conversation. Capture enough "
    "detail for another agent to continue the work without access to the "
    "compacted messages.\n"
    "\n"
    "Before the final summary, you may use an <analysis> block to organize details. "
    "The final answer must include a <summary> block with these exact numbered "
    "sections:\n"
    "\n"
    "1. Primary Request and Intent: the user's explicit requests and intent.\n"
    "2. Key Information and Artifacts: facts, decisions, data, documents, files, or "
    "other artifacts read, produced, or discussed; include identifiers (paths, URLs, "
    "IDs, names) when known.\n"
    "3. Errors and Fixes: errors, failures, wrong assumptions, and how they were "
    "resolved.\n"
    "4. Pending Tasks: outstanding tasks explicitly requested or discovered.\n"
    "5. Current Work: what was being worked on immediately before compaction.\n"
    "6. Next Step: the next concrete action aligned with the latest user request; "
    "write 'None' if the prior task was complete.\n"
    "\n"
    "Be factual and dense. Preserve exact identifiers, values, parameters, and "
    "results, along with any user corrections. Do not invent work that was not done."
)


async def summarize_with_provider(
    provider: Any,
    model: str,
    older: list[Message],
    signal: AbortContext,
    prompt: str = _SUMMARY_PROMPT,
    max_output_tokens: int = 4096,
) -> str:
    """Summarise *older* messages using *provider* and return the summary text.

    The provider's ``stream()`` already yields normalized wire events
    (``text_delta``, ``message_end``, …) so we consume them directly without
    wrapping through any provider-specific translator.

    Args:
        provider: Provider to stream the summarization request through.
        model: Model identifier passed to the provider.
        older: Messages being summarized (typically the compaction window
            being dropped from provider_view).
        signal: Abort context forwarded to the provider request.
        prompt: System prompt instructing how to summarize; defaults to the
            built-in coding-oriented summary prompt.
        max_output_tokens: Cap forwarded to the provider request.

    Returns:
        The summary text, stripped of leading/trailing whitespace.
    """
    from .types import ProviderRequest

    req = ProviderRequest(
        model=model,
        system=[SystemBlock(text=prompt, cacheable=False)],
        tools=[],
        messages=older,
        max_output_tokens=max_output_tokens,
        signal=signal,
        thinking={"type": "disabled"}
        if hasattr(provider, "capabilities") and provider.capabilities(model).can_disable_thinking
        else None,
    )

    text = ""
    async with aclosing(provider.stream(req)) as stream:
        async for ev in stream:
            if ev["type"] == "text_delta":
                text += ev["text"]
            elif ev["type"] == "message_end":
                break

    return text.strip()


async def _summarize_context(
    ctx: CompactionContext,
    provider: Any,
    older: list[Message],
    *,
    prompt: str,
    max_output_tokens: int = 4096,
) -> str:
    if ctx.auxiliary_caller is None:
        return await summarize_with_provider(
            provider,
            ctx.model,
            older,
            ctx.signal,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
        )
    result = await ctx.auxiliary_caller.text(
        logical_call_id=ctx.logical_call_id or str(uuid4()),
        model=ctx.model,
        system=[SystemBlock(text=prompt, cacheable=False)],
        messages=older,
        max_output_tokens=max_output_tokens,
        signal=ctx.signal,
    )
    if result.receipt.disposition != "accepted" or result.text is None:
        raise ProviderError("compaction provider returned an unusable summary", retryable=False)
    return result.text


class _AccountedCompactionProvider:
    """Account only streams made through the supplied provider.

    This facade does not observe opaque external calls or promise replay of a
    custom strategy's arbitrary transformation. Its scope closes early streams
    before the strategy's result may be committed to the provider view.
    """

    def __init__(
        self, caller: _AccountedAuxiliaryCaller, logical_call_id: str, signal: AbortContext
    ) -> None:
        self._caller, self._logical_call_id, self._signal = caller, logical_call_id, signal
        self._streams: set[AsyncGenerator[dict[str, Any], None]] = set()
        self._call_index = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._caller._provider, name)

    def stream(self, request: ProviderRequest) -> AsyncGenerator[dict[str, Any], None]:
        # Distinct calls by a custom strategy are distinct intended operations;
        # the same ordered calls in a retry retain their operation bindings.
        index = self._call_index
        self._call_index += 1
        logical_id = self._logical_call_id if index == 0 else f"{self._logical_call_id}/{index}"
        request = replace(request, signal=self._signal)

        async def accounted() -> AsyncGenerator[dict[str, Any], None]:
            delivery = _AuxiliaryDelivery(self._caller, request, logical_id, self._signal)
            stream = delivery.stream()
            try:
                async for event in stream:
                    yield event
            finally:
                try:
                    await stream.aclose()
                finally:
                    self._streams.discard(wrapped)

        wrapped = accounted()
        self._streams.add(wrapped)
        return wrapped

    async def aclose(self) -> None:
        # A plain async-for break does not synchronously close a generator. The
        # strategy scope owns every facade stream, including such early breaks.
        failure: BaseException | None = None
        try:
            for stream in tuple(self._streams):
                try:
                    await stream.aclose()
                except BaseException as exc:
                    failure = failure or exc
        finally:
            self._streams.clear()
        if failure is not None:
            raise failure


class DefaultCompaction:
    """Summarize older messages while retaining the ten most recent turns.

    Args:
        prompt: System instructions supplied to the summarization provider.
    """

    id = "default-keep-recent-10"

    def __init__(self, *, prompt: str = _SUMMARY_PROMPT) -> None:
        """Create a summarizer with optional host-specific instructions.

        Args:
            prompt: Summary instructions; defaults to the coding-oriented built-in prompt.
        """
        self.prompt = prompt

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        """Summarize messages older than the retained ten-turn tail.

        Args:
            ctx: Conversation messages and provider-call context.
            provider: Provider used for the summary request.

        Returns:
            A summary user message followed by the retained recent messages,
            or the original messages when there is no older history to summarize.
        """
        recent = last_n_turn_boundaries(ctx.messages, 10)
        boundary = len(ctx.messages) - len(recent)
        older = ctx.messages[:boundary] if boundary > 0 else []

        if not older:
            return ctx.messages

        summary_text = await _summarize_context(ctx, provider, older, prompt=self.prompt)

        summary_msg = Message(
            role="user",
            content=[TextBlock(text=f"<summary of earlier conversation>\n\n{summary_text}")],
        )

        return [summary_msg, *recent]


class DetailedCompaction:
    """Continuation-safe compaction with detailed, domain-neutral handoff sections.

    Args:
        keep_recent_turns: Number of recent assistant turns to retain.
        max_output_tokens: Summary output cap forwarded to the provider.
        prompt: System instructions for the detailed handoff summary.
    """

    id = "detailed-continuation-keep-recent-10"

    def __init__(
        self,
        *,
        keep_recent_turns: int = 10,
        max_output_tokens: int = 8192,
        prompt: str = _DETAILED_SUMMARY_PROMPT,
    ) -> None:
        """Configure the retained tail and detailed summary request.

        Args:
            keep_recent_turns: Number of recent assistant turns to retain.
            max_output_tokens: Maximum tokens requested for the summary.
            prompt: Summary instructions supplied to the provider.

        Raises:
            ValueError: If ``keep_recent_turns`` is less than one.
        """
        if keep_recent_turns < 1:
            raise ValueError("detailed_compaction.keep_recent_turns must be >= 1")
        self.keep_recent_turns = keep_recent_turns
        self.max_output_tokens = max_output_tokens
        self.prompt = prompt

    async def compact(self, ctx: CompactionContext, provider: Any) -> list[Message]:
        """Summarize older messages using the detailed continuation format.

        Args:
            ctx: Conversation messages and provider-call context.
            provider: Provider used for the summary request.

        Returns:
            A detailed summary user message followed by the retained recent messages,
            or the original messages when there is no older history to summarize.
        """
        recent = last_n_turn_boundaries(ctx.messages, self.keep_recent_turns)
        boundary = len(ctx.messages) - len(recent)
        older = ctx.messages[:boundary] if boundary > 0 else []

        if not older:
            return ctx.messages

        summary_text = await _summarize_context(
            ctx, provider, older, prompt=self.prompt, max_output_tokens=self.max_output_tokens
        )

        summary_msg = Message(
            role="user",
            content=[
                TextBlock(text=(f"<detailed summary of earlier conversation>\n\n{summary_text}"))
            ],
        )

        return [summary_msg, *recent]


default_compaction = DefaultCompaction()


_DEFAULT_COMPACT_IMPL = DefaultCompaction.compact
_DETAILED_COMPACT_IMPL = DetailedCompaction.compact


def _builtin_compaction_config(strategy: Any) -> dict[str, Any] | None:
    """Only the unchanged concrete built-ins declare this deterministic seam.

    A subclass, a matching id, or a replaced compact method proves nothing about
    an arbitrary transformation. Such strategies retain facade accounting only.
    """
    strategy = default_compaction if strategy is None else strategy
    compact = getattr(getattr(strategy, "compact", None), "__func__", None)
    if type(strategy) is DefaultCompaction and compact is _DEFAULT_COMPACT_IMPL:
        implementation, keep, cap = "default_v1", 10, 4096
    elif type(strategy) is DetailedCompaction and compact is _DETAILED_COMPACT_IMPL:
        implementation = "detailed_v1"
        keep, cap = strategy.keep_recent_turns, strategy.max_output_tokens
    else:
        return None
    return {
        "implementation": implementation,
        "keep_recent_turns": keep,
        "max_output_tokens": cap,
        "prompt_sha256": hashlib.sha256(strategy.prompt.encode("utf-8")).hexdigest(),
    }


def _builtin_paid_view(
    messages: list[Message],
    text: str,
    config: dict[str, Any],
) -> list[Message]:
    recent = last_n_turn_boundaries(messages, config["keep_recent_turns"])
    if len(recent) == len(messages):
        raise ConfigError("paid compaction source has no summarizable prefix")
    return _paid_view_from_recent(recent, text, config)


def _paid_view_from_recent(
    recent: list[Message], text: str, config: dict[str, Any]
) -> list[Message]:
    label = (
        "summary of earlier conversation"
        if config["implementation"] == "default_v1"
        else "detailed summary of earlier conversation"
    )
    return strip_response_chaining(
        [
            Message(role="user", content=[TextBlock(text=f"<{label}>\n\n{text}")]),
            *recent,
        ]
    )


def _paid_projection(
    receipt: Any,
    recipe: dict[str, Any],
    target: list[Message],
) -> dict[str, Any]:
    return _projection_record(
        projection_kind="compaction",
        source_receipt_ids=[receipt.delivery_attempt_id],
        strategy_id=receipt.strategy_id,
        strategy_config=recipe["strategy_config"],
        source_context_fingerprint=recipe["source_context_fingerprint"],
        target_context_fingerprint=_context_fingerprint(target),
        full_history_base_count=recipe["full_history_base_count"],
        full_history_base_fingerprint=recipe["full_history_base_fingerprint"],
        source_message_fingerprints=recipe["source_message_fingerprints"],
        reminder_fingerprints=recipe["reminder_fingerprints"],
    )


def _source_message_fingerprints(messages: Sequence[Message]) -> list[str]:
    return [_model_call_fingerprint(message_to_dict(message)) for message in messages]


def _skill_listing_fingerprint(
    text: str | None, *, session: Any = None, config: Any = None
) -> str | None:
    """Fingerprint the canonical provider-only reminder, including its wrapper."""
    if session is not None:
        if config is None or snapshot_enabled(session, config):
            snapshot = snapshot_for(session)
            if snapshot is not None:
                text = snapshot_skill_listing(snapshot)
    if not text:
        return None
    from .skills.system_reminder import wrap_in_system_reminder

    message = Message(role="user", content=[TextBlock(text=wrap_in_system_reminder(text))])
    return _source_message_fingerprints([message])[0]


def _skill_context_messages(session: Any, agent: Any, *, config: Any = None) -> list[Message]:
    """Content registry only; recipe digests determine historical reminder order."""
    from .skills.system_reminder import wrap_in_system_reminder

    texts = []
    snapshot = (
        snapshot_for(session) if config is None or snapshot_enabled(session, config) else None
    )
    listing = snapshot_skill_listing(snapshot) if snapshot is not None else None
    if snapshot is None:
        listing = getattr(agent, "skill_listing_text", "")
    if listing:
        texts.append(wrap_in_system_reminder(listing))
    for record in getattr(session, "invoked_skills", ()):
        texts.append(
            wrap_in_system_reminder(
                f"Below is the body of a previously invoked skill "
                f"named '{record.name}'.\n\n{record.substituted_body}"
            )
        )
    return [Message(role="user", content=[TextBlock(text=text)]) for text in dict.fromkeys(texts)]


async def _persist_compaction_skill_context(session: Any, ledger: _ModelCallLedger) -> None:
    """SkillTool persistence is best-effort; paid replay requires durable bodies."""
    records = getattr(session, "invoked_skills", ())
    if not records:
        return
    try:
        await durable_call(
            session,
            "session.set_invoked_skills",
            session.store.set_invoked_skills(
                session.id,
                [
                    {
                        "name": record.name,
                        "substituted_body": record.substituted_body,
                        "invoked_at": record.invoked_at,
                    }
                    for record in records
                ],
            ),
        )
    except LeaseLostError:
        raise
    except BaseException as exc:
        ledger._persistence_failed = True
        if not isinstance(exc, Exception):
            raise
        raise _ModelCallError(
            "persistence_failed", "compaction skill context durability failed"
        ) from exc


def _with_recorded_reminders(
    messages: list[Message],
    digests: Sequence[str],
    reminders: dict[str, Message],
    *,
    skip_digests: set[str] | frozenset[str] = frozenset(),
) -> list[Message]:
    result = list(messages)
    present = {
        block.text
        for message in result
        for block in message.content
        if isinstance(block, (TextBlock, CatalogChangeBlock))
    }
    for digest in digests:
        if digest not in reminders:
            raise ConfigError("paid compaction reminder body is not durably resolvable")
        if digest in skip_digests:
            continue
        reminder = reminders[digest]
        texts = {
            block.text
            for block in reminder.content
            if isinstance(block, (TextBlock, CatalogChangeBlock))
        }
        if not texts <= present:
            result.append(reminder)
            present.update(texts)
    return result


def _ordered_compaction_source(
    base: list[Message],
    digests: Sequence[str],
    reminders: dict[str, Message],
) -> list[Message] | None:
    """Insert only recipe-selected reminders between their original durable neighbors."""
    result = []
    base_digests = _source_message_fingerprints(base)
    index = 0
    for digest in digests:
        if index < len(base) and base_digests[index] == digest:
            result.append(base[index])
            index += 1
        elif digest in reminders:
            result.append(reminders[digest])
        else:
            return None
    return result if index == len(base) else None


def _paid_target_from_recipe(
    recipe: dict[str, Any],
    history: list[Message],
    reminders: dict[str, Message],
    text: str,
    ladder: CompactionLadder | None,
) -> list[Message] | None:
    """Resolve only the retained suffix when an earlier run's summary is unavailable."""
    candidates = [history, strip_response_chaining(history)]
    if ladder is not None and ladder.micro:
        micro, _ = micro_compact(history, keep_recent_turns=ladder.keep_recent_turns)
        candidates.extend([micro, strip_response_chaining(micro)])
    variants = [(_source_message_fingerprints(candidate), candidate) for candidate in candidates]
    selected_reminders = {}
    for digest in recipe["reminder_fingerprints"]:
        if digest not in reminders:
            raise ConfigError("paid compaction reminder body is not durably resolvable")
        selected_reminders[digest] = reminders[digest]
    recent: list[Message] = []
    history_index = len(history) - 1
    remaining = recipe["strategy_config"]["keep_recent_turns"]
    for digest in reversed(recipe["source_message_fingerprints"]):
        message = next(
            (
                candidate[history_index]
                for fingerprints, candidate in variants
                if history_index >= 0 and fingerprints[history_index] == digest
            ),
            None,
        )
        if message is not None:
            history_index -= 1
        else:
            message = selected_reminders.get(digest)
        if message is None:
            return None
        recent.append(message)
        if message.role == "assistant":
            remaining -= 1
            if remaining == 0:
                if len(recent) == len(recipe["source_message_fingerprints"]):
                    return None
                return _paid_view_from_recent(
                    list(reversed(recent)), text, recipe["strategy_config"]
                )
    return None


async def _reconcile_paid_compactions(
    session: Any,
    agent: Any,
    ledger: _ModelCallLedger,
    turn_index: int,
    *,
    config: Any = None,
) -> bool:
    """Rebuild built-in projections from receipt text and verified durable history.

    Consumption tracks UsageEvent delivery only. It is deliberately irrelevant
    to paid-result application, including when either store is ahead of the other.
    """
    strategy = getattr(agent, "compaction", None) or default_compaction
    strategy_config = _builtin_compaction_config(strategy)
    projections = ledger.projections("compaction")
    if strategy_config is None:
        if projections:
            raise ConfigError("paid compaction projection has no deterministic strategy seam")
        return False
    receipts = [receipt for receipt in ledger.receipts() if receipt.kind == "compaction"]
    completed_current = any(receipt.turn_index == turn_index for receipt in receipts)
    anchors = {record["source_receipt_ids"][0]: record for record in projections}
    sources = {
        record["delivery_attempt_id"]: record for record in ledger.projections("compaction_source")
    }
    reminder_messages = _skill_context_messages(session, agent, config=config)
    reminders = dict(
        zip(_source_message_fingerprints(reminder_messages), reminder_messages, strict=True)
    )
    history, current = list(session.full_history), list(session.provider_view)
    replay_fingerprints = {_context_fingerprint(history)}
    previous: list[Message] = []
    previous_count = 0
    last_record: dict[str, Any] | None = None
    for receipt in receipts:
        if receipt.disposition != "accepted":
            continue
        if receipt.strategy_id != strategy.id:
            raise ConfigError("paid compaction strategy does not match")
        record = anchors.get(receipt.delivery_attempt_id)
        recipe = record if record is not None else sources.get(receipt.delivery_attempt_id)
        if recipe is None:
            raise ConfigError("paid compaction is missing its ordered source recipe")
        count = recipe["full_history_base_count"]
        if count < previous_count or count > len(history):
            raise ConfigError("paid compaction durable source boundary does not match")
        if (
            recipe["strategy_config"] != strategy_config
            or _context_fingerprint(history[:count]) != recipe["full_history_base_fingerprint"]
        ):
            raise ConfigError("paid compaction durable source or configuration does not match")
        source = [*previous, *history[previous_count:count]]
        candidates = [source]
        ladder = getattr(agent, "compaction_ladder", None)
        if ladder is not None and ladder.micro:
            micro, elided = micro_compact(source, keep_recent_turns=ladder.keep_recent_turns)
            candidates.append(strip_response_chaining(micro) if elided else micro)
        tail_count = len(history) - count
        cached_source = current[:-tail_count] if tail_count else current
        candidates.append(cached_source)
        source = next(
            (
                ordered
                for candidate in candidates
                if (
                    ordered := _ordered_compaction_source(
                        candidate, recipe["source_message_fingerprints"], reminders
                    )
                )
                is not None
                and _context_fingerprint(ordered) == receipt.source_context_fingerprint
            ),
            None,
        )
        assert receipt.accepted_text is not None
        target = (
            _builtin_paid_view(source, receipt.accepted_text, strategy_config)
            if source is not None
            else _paid_target_from_recipe(
                recipe, history[:count], reminders, receipt.accepted_text, ladder
            )
        )
        if target is None:
            raise ConfigError("paid compaction source context does not match durable history")
        derived = _paid_projection(receipt, recipe, target)
        if record is not None and derived != record:
            raise ConfigError("paid compaction target or projection identity does not match")
        if record is None:
            await ledger.record_projection(derived)
        frozen_listing = _skill_listing_fingerprint(None, session=session, config=config)
        skip_digests = {frozen_listing} if frozen_listing is not None else set()
        previous = _with_recorded_reminders(
            target,
            recipe["reminder_fingerprints"],
            reminders,
            skip_digests=skip_digests,
        )
        for view in (source, target, previous):
            if view is not None:
                replay_fingerprints.add(_context_fingerprint([*view, *history[count:]]))
        previous_count = count
        last_record = derived
    if last_record is not None:
        rebuilt = [*previous, *history[previous_count:]]
        ladder = getattr(agent, "compaction_ladder", None)
        current_fingerprint = _context_fingerprint(current)
        if (
            ladder is not None
            and ladder.micro
            and current_fingerprint not in replay_fingerprints
            and len(current) == len(rebuilt)
        ):
            # The saved micro projection may precede later durable appends.
            # Replay at each possible boundary, leaving that later tail intact.
            for boundary in range(len(history), previous_count - 1, -1):
                prefix = [*previous, *history[previous_count:boundary]]
                micro, elided = micro_compact(prefix, keep_recent_turns=ladder.keep_recent_turns)
                if not elided:
                    continue
                candidate = [*strip_response_chaining(micro), *history[boundary:]]
                if current_fingerprint == _context_fingerprint(candidate):
                    rebuilt = candidate
                    replay_fingerprints.add(current_fingerprint)
                    break
        # A cache is either an exact verified source/target (with the durable
        # tail) or absent and reconstructed from full history. Other views are
        # not overwritten with a stale paid result.
        if current_fingerprint not in replay_fingerprints:
            raise ConfigError("paid compaction recovery has an unexpected provider-view suffix")
        session.session_log.record_projection(rebuilt, reason="paid-compaction-recovery")
        session.last_compaction_info = {"projection_id": last_record["projection_id"]}
    return completed_current


def _compaction_hook_dispatcher(agent: Any) -> Any:
    """A live :class:`HookDispatcher` for *agent* iff it has active hooks, else None.

    Built lazily (and imported lazily) so compaction stays zero-overhead and free
    of a hooks import when no hooks are configured — preserving byte-identical
    behavior for the no-hook path.
    """
    hooks = getattr(agent, "hooks", None)
    if not hooks:
        return None
    from .hooks import HookDispatcher

    dispatcher = HookDispatcher(hooks)
    return dispatcher if dispatcher.active else None


def strip_response_chaining(messages: list[Message]) -> list[Message]:
    """Drop ``provider_metadata.openai_responses.response_id`` from every message.

    A compacted ``provider_view`` that retains a ``response_id`` makes the OpenAI
    Responses provider chain the next request onto the server's stored,
    *uncompacted* history (see ``openai_responses.previous_response``) — the
    inserted summary at the head would never be sent and compaction is silently
    defeated.  Returns copies only for messages that needed a strip; untouched
    messages are returned by identity, since they are shared with
    ``full_history`` and the session store and must not be mutated.  Pure
    metadata surgery so :mod:`linch.compaction` stays free of an
    ``openai_responses`` import.
    """
    out: list[Message] = []
    for message in messages:
        metadata = message.provider_metadata
        if not metadata:
            out.append(message)
            continue
        responses = metadata.get("openai_responses")
        if not responses or "response_id" not in responses:
            out.append(message)
            continue

        new_responses = {k: v for k, v in responses.items() if k != "response_id"}
        new_meta = {k: v for k, v in metadata.items() if k != "openai_responses"}
        if new_responses:
            new_meta["openai_responses"] = new_responses
        out.append(
            Message(
                role=message.role,
                content=message.content,
                provider_metadata=new_meta or None,
            )
        )
    return out


@trace_compaction
async def _run_compaction_impl(
    session: Any,
    agent: Any,
    signal: AbortContext,
    strategy: Any,
    *,
    model: str | None = None,
    auxiliary_caller: _AccountedAuxiliaryCaller | None = None,
    logical_call_id: str | None = None,
    _run_config: Any = None,
) -> bool:
    """Run *strategy* against the model selected for the current provider call.

    ``model`` is intentionally supplied by the loop for per-turn overrides.  If
    omitted, run-level fallback state wins over the agent default.  The boolean
    return lets the loop distinguish a real provider-view mutation from a
    no-op strategy before persisting a derived-view snapshot.
    """
    resolved_model = _resolve_compaction_model(session, agent, model)
    # Single pre-compaction snapshot reused by the count, the estimate, and the
    # strategy context below (each projection read deep-copies the history).
    snapshot = list(session.provider_view)
    messages_before = len(snapshot)
    tokens_before = _estimate_tokens(agent, snapshot, model=resolved_model)

    dispatcher = _compaction_hook_dispatcher(_run_config or agent)
    run_id = getattr(session, "active_run_id", None) or "unknown"
    if dispatcher is not None:
        from .hooks import PreCompactContext
        from .lifecycle import PRE_COMPACT

        await dispatcher.dispatch(
            PRE_COMPACT,
            PreCompactContext(
                session=session,
                run_id=run_id,
                turn_index=None,
                deps=getattr(session, "run_deps", None),
                messages=messages_before,
                tokens=tokens_before,
                strategy=strategy.id,
            ),
        )

    config = _builtin_compaction_config(strategy)
    source_recipe = None
    if auxiliary_caller is not None and config is not None:
        await _persist_compaction_skill_context(session, auxiliary_caller._ledger)
        source_recipe = {
            "strategy_config": config,
            "source_context_fingerprint": _context_fingerprint(snapshot),
            "full_history_base_count": len(session.session_log.full_history),
            "full_history_base_fingerprint": _context_fingerprint(session.session_log.full_history),
            "source_message_fingerprints": _source_message_fingerprints(snapshot),
            "reminder_fingerprints": _source_message_fingerprints(
                _skill_context_messages(session, agent, config=_run_config)
            ),
        }
    caller = (
        auxiliary_caller._for_compaction(strategy.id, snapshot, source_recipe=source_recipe)
        if auxiliary_caller is not None
        else None
    )
    operation_id = logical_call_id or str(uuid4())
    ctx = CompactionContext(
        messages=snapshot,
        model=resolved_model,
        signal=signal,
        auxiliary_caller=caller,
        logical_call_id=operation_id if caller is not None else None,
    )
    if caller is None:
        # Standalone legacy execution retains its gate. Accounted deliveries
        # acquire inside the gateway, once for each physical provider stream.
        async with provider_slot(agent, resolved_model):
            compacted = await strategy.compact(ctx, _TelemetryProvider(session, agent.provider))
    else:
        facade = _AccountedCompactionProvider(caller, operation_id, signal)
        try:
            compacted = await strategy.compact(ctx, facade)
        finally:
            await facade.aclose()
        signal.throw_if_aborted()
        caller._ledger.assert_may_start()
    compacted = strip_response_chaining(compacted)
    projection_id = None
    config = _builtin_compaction_config(strategy)
    if caller is not None and config is not None:
        accepted = [
            receipt
            for receipt in caller._ledger.receipts()
            if receipt.logical_call_id == operation_id and receipt.disposition == "accepted"
        ]
        if accepted:
            receipt = accepted[-1]
            assert receipt.accepted_text is not None
            expected = _builtin_paid_view(snapshot, receipt.accepted_text, config)
            if _context_fingerprint(expected) != _context_fingerprint(compacted):
                raise ConfigError("built-in compaction result differs from its deterministic seam")
            assert source_recipe is not None
            record = _paid_projection(receipt, source_recipe, compacted)
            await caller._ledger.record_projection(record)
            projection_id = record["projection_id"]

    changed = session.session_log.record_projection(compacted, reason="compaction")

    session.last_compaction_info = {
        "projection_id": projection_id,
        "type": "compaction",
        "messages_before": messages_before,
        "messages_after": len(compacted),
        "tokens_before": tokens_before,
        # ``compacted`` is what the projection now holds, so estimate from it
        # rather than taking another whole-history snapshot.
        "tokens_after": _estimate_tokens(agent, compacted, model=resolved_model),
        "strategy": strategy.id,
        "model": resolved_model,
    }

    if dispatcher is not None:
        from .hooks import PostCompactContext
        from .lifecycle import POST_COMPACT

        info = session.last_compaction_info
        await dispatcher.dispatch(
            POST_COMPACT,
            PostCompactContext(
                session=session,
                run_id=run_id,
                turn_index=None,
                deps=getattr(session, "run_deps", None),
                messages_before=info["messages_before"],
                messages_after=info["messages_after"],
                tokens_before=info["tokens_before"],
                tokens_after=info["tokens_after"],
                strategy=strategy.id,
            ),
        )
    return changed


async def maybe_compact(
    session: Any,
    agent: Any,
    signal: AbortContext,
    *,
    model: str | None = None,
    auxiliary_caller: _AccountedAuxiliaryCaller | None = None,
    logical_call_id: str | None = None,
    _run_config: Any = None,
) -> bool:
    resolved_model = _resolve_compaction_model(session, agent, model)
    strategy = getattr(agent, "compaction", None) or default_compaction
    limit = agent.provider.context_window(resolved_model)
    # Keep the historical 32k default on normal large-context models, but do
    # not let the reserve alone make every request to a small/local context
    # appear over threshold.  An explicitly configured output cap remains the
    # host's requested reservation and is therefore not clamped.
    reserve = agent.max_output_tokens or min(32768, max(1, limit // 5))
    projected = _estimate_tokens(agent, session.provider_view, model=resolved_model) + reserve

    if projected < 0.8 * limit:
        return False

    # Ladder rung 1 — micro-compact (no LLM call).  If eliding old tool
    # results brings the projection back under threshold, skip summarization
    # entirely; otherwise keep the savings and fall through.
    micro_info: dict[str, Any] | None = None
    ladder = getattr(agent, "compaction_ladder", None)
    if ladder is not None and ladder.micro:
        if apply_micro_compaction(
            session,
            agent,
            keep_recent_turns=ladder.keep_recent_turns,
            model=resolved_model,
        ):
            projected = (
                _estimate_tokens(agent, session.provider_view, model=resolved_model) + reserve
            )
            if projected < 0.8 * limit:
                return True
            micro_info = session.last_compaction_info

    try:
        changed = await _run_compaction_impl(
            session,
            agent,
            signal,
            strategy,
            model=resolved_model,
            auxiliary_caller=auxiliary_caller,
            logical_call_id=logical_call_id,
            _run_config=_run_config,
        )
    except Exception:
        # Micro-compaction already recorded a provider-view projection above; if the
        # follow-up summarization now fails (and the caller degrades instead of
        # crashing, e.g. maybe_compact_resilient), the read tracker must still
        # be reset here or a file whose contents were just elided keeps passing
        # the Edit tool's has_read gate.
        if micro_info is not None:
            reset_read_tracker_after_compaction(session, agent)
        raise

    if not changed:
        # Full strategy was a no-op; if micro elided something, that is still a
        # reportable compaction.
        if micro_info is not None:
            session.last_compaction_info = micro_info
            return True
        session.last_compaction_info = None
        return False

    return True


def _resolve_compaction_model(session: Any, agent: Any, model: str | None) -> str:
    """Resolve the exact model whose context is being compacted.

    An explicit per-turn model override wins.  Otherwise retain a run-level
    fallback selected after provider overload, then fall back to the agent's
    configured primary model.
    """
    return model or getattr(session, "active_model", None) or agent.model


def _estimate_tokens(
    agent: Any,
    messages: Sequence[Message],
    *,
    model: str | None = None,
) -> int:
    resolved_model = model or agent.model
    estimator = getattr(agent, "token_estimator", None)
    estimate = custom_token_estimate(estimator, messages, resolved_model)
    if estimate is not None:
        return estimate

    # Count text, tool inputs/results, and metadata, but never tokenize encoded
    # images or opaque reasoning signatures as prose. A fixed per-image cost is
    # a bounded fallback; hosts can supply a model-aware estimator above.
    chars = images = 0
    for message in messages:
        raw = message_to_dict(message)
        images += sum(_strip_estimation_payloads(block) for block in raw["content"])
        chars += len(json.dumps(raw, ensure_ascii=False, default=str))
    return chars // 4 + images * 1000


def _strip_estimation_payloads(block: dict[str, Any]) -> int:
    """Remove opaque payloads from a freshly serialized block and count images."""
    kind = block["type"]
    if kind == "image":
        block.pop("source", None)
        return 1
    if kind == "thinking":
        block.pop("signature", None)
    elif kind == "redacted_thinking":
        block.pop("data", None)
    elif kind == "tool_result" and isinstance(block["content"], list):
        return sum(_strip_estimation_payloads(item) for item in block["content"])
    return 0


async def run_forced_compaction(
    session: Any,
    agent: Any,
    signal: AbortContext,
    *,
    model: str | None = None,
    auxiliary_caller: _AccountedAuxiliaryCaller | None = None,
    logical_call_id: str | None = None,
    _run_config: Any = None,
) -> bool:
    strategy = getattr(agent, "compaction", None) or default_compaction
    return await _run_compaction_impl(
        session,
        agent,
        signal,
        strategy,
        model=model,
        auxiliary_caller=auxiliary_caller,
        logical_call_id=logical_call_id,
        _run_config=_run_config,
    )


def build_compaction_event(session: Any) -> CompactionEvent:
    info = session.last_compaction_info
    if info is None:
        raise RuntimeError("last_compaction_info not set after compaction")
    return CompactionEvent(
        messages_before=info["messages_before"],
        messages_after=info["messages_after"],
        tokens_before=info["tokens_before"],
        tokens_after=info["tokens_after"],
        strategy=info["strategy"],
    )
