//! A skill call in a run: its `skill_invoked` and `skill_completed`
//! events, and the overlay its frontmatter sets for the next model turn
//! (the tools offered and the model), kept across a crash.

mod common;

use std::sync::Arc;

use common::subagents::Dying;
use common::*;
use linch_core::events::kinds::{SkillCompletedEvent, SkillInvokedEvent};
use linch_core::journal::INTERRUPTED_TOOL_TEXT;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::FaultTiming;
use linch_core::{
    Agent, AgentBuilder, Event, PermissionMode, Provider, RunId, RunOptions, RunStatus, RunStore,
    ScriptedProvider, ScriptedTurn, SessionStore, Skill, StoreError, SubagentDefinition, ToolScope,
};
use serde_json::{Value, json};

fn call(id: &str, tool: &str, input: Value) -> ScriptedTurn {
    TurnBuilder::new(MODEL)
        .tool_call(id, tool, &input)
        .usage(usage(10, 5))
        .build()
}

fn skill_call(skill: &str) -> ScriptedTurn {
    call("k1", "Skill", json!({"skill": skill, "args": "a.rs"}))
}

fn review() -> Skill {
    let mut skill = Skill::new("review", "/skills/review", "Review.", "review $ARGUMENTS");
    skill.allowed_tools = Some(vec!["Echo".into()]);
    skill.model = Some("other-model".into());
    skill
}

fn agent<S: RunStore + SessionStore + 'static>(
    provider: Arc<dyn Provider>,
    runs: &Runs,
    stores: Arc<S>,
) -> Agent {
    let tools = registry(vec![
        Echo::new("Echo", ToolScope::Read, runs),
        Echo::new("Write", ToolScope::Write, runs),
    ]);
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .skills(vec![
            review(),
            Skill::new("plain", "/skills/plain", "Plain.", "go"),
        ])
        .build()
        .unwrap()
}

fn names(request: &linch_core::ProviderRequest) -> Vec<&str> {
    request.tools.iter().map(|t| t.name.as_str()).collect()
}

#[tokio::test]
async fn a_skill_call_reports_its_start_and_end_with_its_call_end() {
    let turns = [skill_call("plain"), skill_call("nope"), text_turn("done")];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let agent = agent(provider, &Runs::default(), stores.clone());
    let stream = agent.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    finish(stream).await;

    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let skills: Vec<&Event> = (events.iter().map(|e| &e.event))
        .filter(|e| matches!(e, Event::SkillInvoked(_) | Event::SkillCompleted(_)))
        .collect();
    let invoked = |name: &str| {
        Event::SkillInvoked(SkillInvokedEvent {
            name: name.into(),
            args: Some("a.rs".into()),
            model_override: None,
            allowed_tools: None,
        })
    };
    let completed = |name: &str, is_error| {
        Event::SkillCompleted(SkillCompletedEvent {
            name: name.into(),
            is_error,
        })
    };
    let expected = [
        invoked("plain"),
        completed("plain", false),
        invoked("nope"),
        completed("nope", true),
    ];
    assert_eq!(skills, expected.iter().collect::<Vec<_>>());
    let run = stores.runs.writes();
    assert_eq!(run.commit, 2 * 5 + 3, "the events ride the call ends");
}

#[tokio::test]
async fn a_skill_narrows_the_next_turn_to_its_tools_and_model() {
    let turns = [
        skill_call("review"),
        call("w1", "Write", json!({"value": "x"})),
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let runs = Runs::default();
    let stores = Arc::new(Stores::default());
    let agent = agent(provider.clone(), &runs, stores.clone());
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let requests = provider.requests();
    assert_eq!(names(&requests[0]), ["Echo", "Write", "Skill"]);
    assert_eq!(requests[0].model, MODEL);
    assert_eq!(names(&requests[1]), ["Echo"]);
    assert_eq!(requests[1].model, "other-model");
    assert_eq!(names(&requests[2]), ["Echo", "Write", "Skill"]);
    assert_eq!(requests[2].model, MODEL);
    let log = messages(&*stores, "s1").await;
    assert_eq!(result_texts(&log[4]), [r#"Tool "Write" is not registered"#]);
    assert!(runs.lock().unwrap().is_empty(), "Write never ran");
}

#[tokio::test]
async fn a_sessions_first_run_lists_its_skills_after_init() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("one"), text_turn("two")]));
    let stores = Arc::new(Stores::default());
    let agent = agent(provider, &Runs::default(), stores.clone());
    let first = finish(agent.run("go", RunOptions::new("s1"))).await;
    let second = finish(agent.run("again", RunOptions::new("s1"))).await;

    let kinds: Vec<&str> = first.iter().map(|e| e.event.type_name()).collect();
    assert_eq!(kinds[..3], ["init", "skills_loaded", "user"]);
    let Event::SkillsLoaded(loaded) = &first[1].event else {
        unreachable!()
    };
    let verify = linch_core::skills::verify();
    let expected = [
        json!({"name": "plain", "description": "Plain."}),
        json!({"name": "review", "description": "Review."}),
        json!({
            "name": "verify", "description": verify.description,
            "when_to_use": verify.when_to_use, "argument_hint": verify.argument_hint,
        }),
    ];
    let skills: Vec<Value> = (loaded.skills.iter())
        .map(|s| Value::Object(s.clone()))
        .collect();
    assert_eq!(skills, expected, "every skill, sorted by name");
    let listed = |envelopes: &[linch_core::Envelope]| {
        (envelopes.iter()).any(|e| matches!(e.event, Event::SkillsLoaded(_)))
    };
    assert!(!listed(&second), "once per session");

    let plain = AgentBuilder::new(spec())
        .provider(Arc::new(ScriptedProvider::new([text_turn("hi")])))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let envelopes = finish(plain.run("go", RunOptions::new("s2"))).await;
    assert!(!listed(&envelopes), "an agent without skills lists none");
}

#[tokio::test]
async fn a_run_without_the_skill_tool_lists_no_skills() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("a"), text_turn("b")]));
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]);
    let mut scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    scout.tools = Some(vec!["Echo".into()]);
    let mut reader = SubagentDefinition::new("reader", "Reads skills.", "Read.");
    reader.tools = Some(vec!["Echo".into(), "Skill".into()]);
    let agent = AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .skills(vec![review()])
        .subagents(vec![scout, reader])
        .build()
        .unwrap();
    let listed = |envelopes: &[linch_core::Envelope]| {
        (envelopes.iter()).any(|e| matches!(e.event, Event::SkillsLoaded(_)))
    };
    let parent = RunId::new("wf-1");
    let scout = agent.subagent(Some("scout")).unwrap();
    let envelopes = finish(scout.run_child("look", RunOptions::new("c1"), parent.clone())).await;
    assert!(!listed(&envelopes), "no Skill tool, no listing");
    let reader = agent.subagent(Some("reader")).unwrap();
    let envelopes = finish(reader.run_child("look", RunOptions::new("c2"), parent)).await;
    assert!(listed(&envelopes), "a child with Skill lists them");
}

#[tokio::test]
async fn each_call_names_the_model_it_requests_on_its_context_build() {
    let turns = [
        skill_call("review"),
        text_turn("checked"),
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let agent = agent(provider, &Runs::default(), stores.clone());
    let envelopes = finish(agent.run("go", RunOptions::new("s1"))).await;

    let models: Vec<&str> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::ContextBuild(build) => Some(build.model.as_str()),
            _ => None,
        })
        .collect();
    assert_eq!(models, [MODEL, "other-model"]);
}

#[test]
fn a_skill_model_keeps_the_one_million_token_suffix() {
    use linch_core::skills::model_override;
    assert_eq!(model_override("m2", "m1[1m]"), "m2[1m]");
    assert_eq!(model_override("m2[1m]", "m1[1m]"), "m2[1m]");
    assert_eq!(model_override("m2", "m1"), "m2");
}

#[tokio::test]
async fn a_crash_keeps_the_narrowed_turn_narrowed() {
    // A crash after the skill call started leaves it interrupted: the skill
    // did not load, so the next turn is not narrowed.
    let mut narrowed_resumes = 0;
    let turns = || {
        let turns = vec![
            skill_call("review"),
            call("w1", "Write", json!({"value": "x"})),
            text_turn("done"),
        ];
        Arc::new(Replay::new(turns))
    };
    let writes = {
        let stores = Arc::new(Stores::default());
        let agent = agent(turns(), &Runs::default(), stores.clone());
        finish(agent.run("go", RunOptions::new("s1"))).await;
        stores.log.lock().unwrap().len() as u64
    };
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let (stores, runs, provider) = (Arc::new(Stores::default()), Runs::default(), turns());
            let dying = Arc::new(Dying::new(stores.clone(), n, timing));
            let mut options = RunOptions::new("s1");
            options.run_id = Some(RunId::new("r1"));
            let _ = drain(agent(provider.clone(), &runs, dying).run("go", options)).await;
            let clean = agent(provider, &runs, stores.clone());
            match stores.runs.load(NS, &RunId::new("r1")).await {
                Ok(record) if record.status.is_terminal() => {}
                Ok(_) => {
                    finish(clean.resume(RunId::new("r1"), RunOptions::new("s1"))).await;
                }
                Err(StoreError::NotFound { .. }) => {
                    let mut options = RunOptions::new("s1");
                    options.run_id = Some(RunId::new("r1"));
                    finish(clean.run("go", options)).await;
                }
                Err(error) => panic!("{context}: {error}"),
            }
            let record = stores.runs.load(NS, &RunId::new("r1")).await.unwrap();
            assert_eq!(record.status, RunStatus::Completed, "{context}");
            let log = messages(&*stores, "s1").await;
            let ran = runs.lock().unwrap().get("w1").copied().unwrap_or(0);
            if result_texts(&log[2]) == [INTERRUPTED_TOOL_TEXT] {
                assert!(ran <= 1, "{context}: Write ran {ran} times");
                continue;
            }
            let body = "Skill base directory: /skills/review\n\nreview a.rs";
            assert_eq!(result_texts(&log[2]), [body], "{context}");
            assert_eq!(ran, 0, "{context}: Write ran");
            assert_eq!(result_texts(&log[4]), [r#"Tool "Write" is not registered"#]);
            // Crashes from the narrowed turn's dispatch to its call's end.
            narrowed_resumes += usize::from((12..=15).contains(&n));
        }
    }
    assert_eq!(narrowed_resumes, 8);
}
