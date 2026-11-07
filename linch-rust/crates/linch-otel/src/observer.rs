//! [`RunObserver`]: one run's spans and metrics, built from its envelopes.
//!
//! This file holds the run and its turns; `calls` the model calls and
//! compaction, `tools` the tool calls.

mod calls;
mod tools;

use linch_core::events::kinds::{
    ErrorEvent, ResultEvent, ResultSubtype, UserEvent, UserEventSubtype,
};
use linch_core::{Envelope, Event, JsonValue};
use opentelemetry::trace::SpanKind;
use opentelemetry::{Context, KeyValue};

use self::calls::{Chat, Summary};
use crate::span::{Open, count, json, now_ms, wire_name};
use crate::{Telemetry, keys, provider_name, span_trace_id};

/// Builds the spans and metrics of one run, or of one resume or approve of
/// it, from its envelopes as they stream.
///
/// Feed it every envelope of the stream with [`RunObserver::observe`], in
/// order. Each span starts at its opening event's `ts_ms` and ends at its
/// closing event's; the run's spans end on its `result` or `suspended`
/// event. If the stream ends any other way, call [`RunObserver::fail`] with
/// the error's type name, or drop the observer: either ends every open
/// span at the wall clock.
///
/// Every call starts with `init`: a resume or approve's is `resumed`
/// (`linch.resumed` is true) and names the turn it continues in. A stream
/// that starts with anything else is traced as resumed too; give its
/// session, model and turn with [`RunObserver::session_id`],
/// [`RunObserver::model`] and [`RunObserver::turn`] when the host knows
/// them, otherwise the attributes are left out. Events in an order no run
/// produces are traced as far as they make sense and never panic; an
/// envelope of another run, or anything after the run's end, is ignored.
pub struct RunObserver {
    telemetry: Telemetry,
    parent: Context,
    capture: bool,
    provider: Option<String>,
    model: Option<String>,
    session_id: Option<String>,
    turn: Option<u64>,
    run_id: Option<String>,
    trace_id: Option<String>,
    run: Option<Open>,
    step: Option<Open>,
    /// When the next turn begins: the prompt, or the end of the last turn.
    turn_opened: Option<u64>,
    chat: Option<Chat>,
    /// The turn's summary call, until its `compaction` event (or the
    /// turn's primary call, when none follows) places its span.
    summary: Option<Summary>,
    tools: Vec<(String, Open)>,
    /// Whether an `error` event already marked the run.
    errored: bool,
    prompt_captured: bool,
    ended: bool,
}

impl RunObserver {
    pub(crate) fn new(telemetry: Telemetry, capture: bool) -> Self {
        Self {
            telemetry,
            parent: Context::new(),
            capture,
            provider: None,
            model: None,
            session_id: None,
            turn: None,
            run_id: None,
            trace_id: None,
            run: None,
            step: None,
            turn_opened: None,
            chat: None,
            summary: None,
            tools: Vec::new(),
            errored: false,
            prompt_captured: false,
            ended: false,
        }
    }

    /// The Linch provider id (`openai-chat`, `anthropic`, ...), reported as
    /// `gen_ai.provider.name` through [`provider_name`]; a run's `init`
    /// event overrides it.
    pub fn provider(mut self, id: &str) -> Self {
        self.provider = Some(provider_name(id).to_owned());
        self
    }

    /// The model, for a stream without `init`; an `init` event overrides
    /// it.
    pub fn model(mut self, model: impl Into<String>) -> Self {
        self.model = Some(model.into());
        self
    }

    /// The session id, for a stream without `init`; an `init` event
    /// overrides it.
    pub fn session_id(mut self, session_id: impl Into<String>) -> Self {
        self.session_id = Some(session_id.into());
        self
    }

    /// The turn a stream without `init` starts in (the run checkpoint's
    /// turn); an `init` event names its own.
    pub fn turn(mut self, turn: u64) -> Self {
        self.turn = Some(turn);
        self
    }

    /// The context the run span is a child of, such as the host request's
    /// span. Without one the run span is a trace root.
    pub fn parent(mut self, parent: Context) -> Self {
        self.parent = parent;
        self
    }

    /// Whether to keep content on the spans: the prompt and final text on
    /// the run, each answer on its chat, and tool input and result text.
    pub fn capture_content(mut self, on: bool) -> Self {
        self.capture = on;
        self
    }

    /// The trace id of the run span, as 32 hex characters, once the first
    /// envelope started it; before that, the parent's, if it has one.
    pub fn trace_id(&self) -> Option<String> {
        (self.trace_id.clone()).or_else(|| span_trace_id(&self.parent))
    }

    /// Trace one envelope of the run.
    pub fn observe(&mut self, envelope: &Envelope) {
        if self.ended {
            return;
        }
        let run_id = envelope.run_id.as_str();
        match &self.run_id {
            Some(id) if id != run_id => return,
            Some(_) => {}
            None => self.run_id = Some(run_id.to_owned()),
        }
        let (event, at) = (&envelope.event, envelope.ts_ms);
        if !matches!(event, Event::Usage(_)) {
            self.close_answered_chat();
        }
        if self.run.is_none() {
            self.start_run(event, at);
        }
        match event {
            Event::User(user) => self.user(user, at),
            Event::ContextBuild(build) => self.context_build(build, at),
            Event::Assistant(response) => self.assistant(response, at),
            Event::Usage(usage) => self.usage(usage, at),
            Event::Compaction(compaction) => self.compaction(compaction, at),
            Event::ToolCallStart(start) => self.tool_start(start, at),
            Event::ToolCallEnd(end) => self.tool_end(end, at),
            Event::Error(error) => self.error(error, at),
            Event::Result(result) => self.result(result, at),
            Event::Suspended(_) => self.close(at, None),
            _ => {}
        }
    }

    /// End the run for a stream that failed with an error of `error_type`
    /// (a type name, never a message): every open span ends now, marked
    /// with it.
    pub fn fail(&mut self, error_type: &str) {
        if !self.ended {
            self.close_answered_chat();
            self.close(now_ms(), Some(error_type));
        }
    }

    fn start(
        &self,
        parent: &Context,
        name: impl Into<std::borrow::Cow<'static, str>>,
        kind: SpanKind,
        at: u64,
        attributes: Vec<KeyValue>,
    ) -> Open {
        Open::start(self.telemetry.tracer(), parent, name, kind, at, attributes)
    }

    /// The attributes Python's `attributes()` gives an operation's span.
    fn attributes(&self, operation: &'static str, with_turn: bool) -> Vec<KeyValue> {
        let mut attributes = vec![KeyValue::new(keys::OPERATION, operation)];
        for (key, value) in [
            (keys::PROVIDER, &self.provider),
            (keys::MODEL, &self.model),
            (keys::CONVERSATION_ID, &self.session_id),
            (keys::RUN_ID, &self.run_id),
        ] {
            if let Some(value) = value {
                attributes.push(KeyValue::new(key, value.clone()));
            }
        }
        if let (true, Some(turn)) = (with_turn, self.turn) {
            attributes.push(KeyValue::new(keys::TURN_INDEX, count(turn)));
        }
        attributes
    }

    fn start_run(&mut self, event: &Event, at: u64) {
        let resumed = match event {
            Event::Init(init) => {
                self.session_id = Some(init.session_id.as_str().to_owned());
                self.model = Some(init.model.clone());
                self.provider = Some(provider_name(&init.provider).to_owned());
                self.turn = Some(init.turn);
                init.resumed
            }
            _ => true,
        };
        let mut attributes = self.attributes(keys::INVOKE_AGENT, false);
        attributes.push(KeyValue::new(keys::RESUMED, resumed));
        let run = self.start(
            &self.parent,
            keys::INVOKE_AGENT,
            SpanKind::Internal,
            at,
            attributes,
        );
        self.trace_id = span_trace_id(run.context());
        self.run = Some(run);
    }

    /// The open step's context and start, opening it if the turn has none.
    fn step(&mut self, at: u64) -> (Context, u64) {
        if let Some(step) = &self.step {
            return (step.context().clone(), step.start_ms());
        }
        let start = self.turn_opened.unwrap_or(at);
        let parent =
            (self.run.as_ref()).map_or_else(|| self.parent.clone(), |run| run.context().clone());
        let attributes = self.attributes(keys::STEP, true);
        let step = self.start(&parent, keys::STEP, SpanKind::Internal, start, attributes);
        let opened = (step.context().clone(), start);
        self.step = Some(step);
        opened
    }

    fn user(&mut self, user: &UserEvent, at: u64) {
        if user.subtype == UserEventSubtype::Prompt {
            let run = self.run.as_ref().filter(|run| run.recording());
            if let (true, false, Some(run)) = (self.capture, self.prompt_captured, run) {
                run.set(KeyValue::new(
                    keys::INPUT_MESSAGES,
                    json(&user.message.text()),
                ));
                self.prompt_captured = true;
            }
        } else if self.step.is_some() {
            // Tool results (or stop feedback) end the turn.
            self.end_step(at, None);
            self.turn = self.turn.map(|turn| turn.saturating_add(1));
        }
        self.turn_opened = Some(at);
    }

    fn error(&mut self, event: &ErrorEvent, at: u64) {
        let name = event.error.get("name").and_then(JsonValue::as_str);
        let name = name.unwrap_or("run_error");
        self.errored = true;
        if let Some(chat) = self.chat.take() {
            self.finish_chat(chat, at, Some(name));
        }
        for open in [&self.step, &self.run].into_iter().flatten() {
            open.set_error(name);
        }
    }

    fn result(&mut self, result: &ResultEvent, at: u64) {
        let failed = result.subtype != ResultSubtype::Success && !self.errored;
        let failed = failed.then(|| wire_name(&result.subtype));
        if let Some(run) = &self.run {
            run.set_usage(&result.total_usage);
            run.set(KeyValue::new(
                keys::STOP_REASON,
                wire_name(&result.stop_reason),
            ));
            if let Some(cost) = result.total_cost_usd {
                run.set(KeyValue::new(keys::COST_USD, cost));
            }
            if self.capture && run.recording() {
                run.set(KeyValue::new(
                    keys::OUTPUT_MESSAGES,
                    json(&result.final_text),
                ));
            }
        }
        self.close(at, failed.as_deref());
    }

    /// End the turn's open spans, marked with `error` if set, then the step.
    fn end_step(&mut self, at: u64, error: Option<&str>) {
        if let Some(chat) = self.chat.take() {
            self.finish_chat(chat, at, error);
        }
        self.flush_summary(error);
        let tools = self.tools.drain(..).map(|(_, open)| (open, at));
        let step = self.step.take().map(|open| (open, at));
        for (open, end) in tools.chain(step) {
            if let Some(error) = error {
                open.set_error(error);
            }
            open.end(end);
        }
    }

    /// End every open span at `at`, marked with `error` if set.
    fn close(&mut self, at: u64, error: Option<&str>) {
        self.end_step(at, error);
        if let Some(run) = self.run.take() {
            if let Some(error) = error {
                run.set_error(error);
            }
            run.end(at);
        }
        self.ended = true;
    }
}

impl Drop for RunObserver {
    fn drop(&mut self) {
        if !self.ended {
            self.close_answered_chat();
            self.close(now_ms(), None);
        }
    }
}

impl std::fmt::Debug for RunObserver {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("RunObserver")
            .field("run_id", &self.run_id)
            .field("turn", &self.turn)
            .field("ended", &self.ended)
            .finish_non_exhaustive()
    }
}
