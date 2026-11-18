//! System prompt sections (`AgentSpec::system`): sent in order with their
//! cache flags, before the host blocks and the prompt, with `append` after
//! them and the skill listing; part of the contract and of a subagent's
//! prefix; unset, nothing changes.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::{
    Agent, AgentBuilder, AgentSpec, BuildError, Event, PermissionEngine, PermissionMode,
    PermissionPolicy, RunError, RunId, RunOptions, RunStatus, RunStore, ScriptedProvider, Skill,
    SubagentDefinition, SystemBlock, SystemSection, ToolScope,
};

fn sectioned() -> AgentSpec {
    let mut spec = spec();
    spec.system_blocks = vec![SystemBlock {
        text: "Host.".into(),
        cacheable: true,
    }];
    spec.system.sections = vec![
        SystemSection::new("tone", "Tone.").order(2),
        SystemSection::new("policy", "Policy.").order(1),
    ];
    spec.system.append = Some("Tail.".into());
    spec
}

fn builder(spec: AgentSpec, provider: Arc<ScriptedProvider>) -> AgentBuilder {
    let stores = Arc::new(Stores::default());
    AgentBuilder::new(spec)
        .provider(provider)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
}

fn flags(system: &[SystemBlock]) -> Vec<(&str, bool)> {
    (system.iter())
        .map(|b| (b.text.as_str(), b.cacheable))
        .collect()
}

#[tokio::test]
async fn sections_go_first_in_order_with_their_flags_and_append_goes_last() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("one"), text_turn("two")]));
    // With no host block, a section that is not cacheable may come last.
    let mut spec = sectioned();
    spec.system_blocks.clear();
    let volatile = SystemSection::new("volatile", "Today.").order(9);
    spec.system.sections.push(volatile.cacheable(false));
    let agent = builder(spec, provider.clone()).build().unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    finish(agent.run("again", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    assert_eq!(
        flags(&requests[0].system),
        [
            ("Policy.", true),
            ("Tone.", true),
            ("Today.", false),
            ("You are a test agent.", false),
            ("Tail.", false),
        ]
    );
    assert_eq!(requests[1].system, requests[0].system, "the frozen prefix");
}

#[tokio::test]
async fn the_skill_listing_goes_before_append() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("hi")]));
    let spec = sectioned();
    let skill = Skill::new("fmt", "/skills/fmt", "Format a file.", "fix it");
    let agent = builder(spec, provider.clone())
        .skills(vec![skill])
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let system = &provider.requests()[0].system;
    let texts: Vec<&str> = system.iter().map(|b| b.text.as_str()).collect();
    assert_eq!(
        texts[..4],
        ["Policy.", "Tone.", "Host.", "You are a test agent."]
    );
    assert!(texts[4].starts_with("The following skills are available"));
    assert_eq!(texts[5..], ["Tail."]);
}

#[tokio::test]
async fn a_subagent_takes_the_sections_and_append_before_its_definition() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("found")]));
    let scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    let agent = builder(sectioned(), provider.clone())
        .subagents(vec![scout])
        .build()
        .unwrap();
    let child = agent.subagent(Some("scout")).unwrap();
    let mut options = RunOptions::new("c1");
    options.run_id = Some(RunId::new("c1"));
    finish(child.run_child("look", options, RunId::new("p1"))).await;
    assert_eq!(
        flags(&provider.requests()[0].system),
        [
            ("Policy.", true),
            ("Tone.", true),
            ("Host.", true),
            ("You are a test agent.", false),
            ("Tail.", false),
            ("User-provided instructions:\n\nScout the code.", true),
        ]
    );
}

#[test]
fn unset_sections_keep_the_contract_and_set_ones_change_it() {
    let provider = || Arc::new(ScriptedProvider::new([]));
    let plain = builder(spec(), provider()).build().unwrap();
    let mut empty = spec();
    empty.system = Default::default();
    let empty = builder(empty, provider()).build().unwrap();
    assert_eq!(plain.contract(), empty.contract());
    let sectioned = builder(sectioned(), provider()).build().unwrap();
    assert_ne!(plain.contract(), sectioned.contract());
    let mut edited = self::sectioned();
    edited.system.sections[0].text = "Other.".into();
    let edited = builder(edited, provider()).build().unwrap();
    assert_ne!(edited.contract(), sectioned.contract(), "the text counts");
    let mut dropped = self::sectioned();
    dropped.system.sections.pop();
    let dropped = builder(dropped, provider()).build().unwrap();
    assert_ne!(dropped.contract(), sectioned.contract(), "the set counts");
}

#[test]
fn bad_sections_refuse_the_build() {
    let provider = || Arc::new(ScriptedProvider::new([]));
    let invalid = |change: &dyn Fn(&mut AgentSpec)| {
        let mut spec = sectioned();
        change(&mut spec);
        matches!(
            builder(spec, provider()).build(),
            Err(BuildError::Invalid(_))
        )
    };
    assert!(invalid(&|s| s.system.sections[0].name = " ".into()));
    assert!(invalid(&|s| s.system.sections[0].text = "".into()));
    assert!(invalid(&|s| s.system.sections[1].name = "tone".into()));
    assert!(invalid(&|s| s.system.append = Some("  ".into())));
    // A section that is not cacheable ahead of a cacheable block.
    let volatile = SystemSection::new("volatile", "Today.").cacheable(false);
    assert!(invalid(&|s| s.system.sections.push(volatile.clone())));
    assert!(!invalid(&|_| {}));
}

/// A run suspended under one set of sections is not resumed by an agent
/// with another: the sections are part of the contract.
#[tokio::test]
async fn a_run_is_not_resumed_under_other_sections() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let build = |spec: AgentSpec| -> Agent {
        let tools = registry(vec![Echo::new("save", ToolScope::Write, &runs)]);
        let provider = Arc::new(ScriptedProvider::new(script(1, 1, "save")));
        let policy = PermissionPolicy::new(PermissionMode::Default, "/");
        let engine = PermissionEngine::new(policy).unwrap();
        agent_with(spec, provider, tools, engine, stores.clone())
    };
    let first = build(sectioned());
    let stream = first.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert!(matches!(
        envelopes.last().unwrap().event,
        Event::Suspended(_)
    ));
    let mut other = sectioned();
    other.system.sections[1].text = "Changed policy.".into();
    for spec in [other, spec()] {
        let changed = build(spec);
        let (_, error) = drain(changed.resume(run_id.clone(), RunOptions::new(""))).await;
        assert!(
            matches!(error, Some(RunError::ContractMismatch { .. })),
            "{error:?}"
        );
    }
    let status = stores.runs.load(NS, &run_id).await.unwrap().status;
    assert_eq!(status, RunStatus::AwaitingApproval);
}
