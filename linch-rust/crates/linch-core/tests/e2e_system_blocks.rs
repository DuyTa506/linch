//! Host system blocks: sent in order, before the system prompt, with their
//! `cacheable` flags; part of the frozen prefix, the contract and a
//! subagent's prefix.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::{
    AgentBuilder, AgentSpec, BuildError, PermissionMode, RunId, RunOptions, ScriptedProvider,
    Skill, SubagentDefinition, SystemBlock,
};

fn block(text: &str, cacheable: bool) -> SystemBlock {
    SystemBlock {
        text: text.into(),
        cacheable,
    }
}

fn hosted() -> AgentSpec {
    let mut spec = spec();
    spec.system_blocks = vec![block("Company policy.", true), block("Style.", false)];
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
    system
        .iter()
        .map(|b| (b.text.as_str(), b.cacheable))
        .collect()
}

#[tokio::test]
async fn host_blocks_go_before_the_system_prompt_with_their_flags() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("one"), text_turn("two")]));
    let agent = builder(hosted(), provider.clone()).build().unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    finish(agent.run("again", RunOptions::new("s1"))).await;
    let requests = provider.requests();
    assert_eq!(
        flags(&requests[0].system),
        [
            ("Company policy.", true),
            ("Style.", false),
            ("You are a test agent.", false),
        ]
    );
    assert_eq!(requests[1].system, requests[0].system, "the frozen prefix");
}

#[tokio::test]
async fn the_skill_listing_and_the_builder_blocks_follow() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("hi")]));
    let skill = Skill::new("fmt", "/skills/fmt", "Format a file.", "fix it");
    let agent = builder(hosted(), provider.clone())
        .system_blocks([block("From code.", true)])
        .skills(vec![skill])
        .build()
        .unwrap();
    assert_eq!(agent.spec().system_blocks.len(), 3);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let system = &provider.requests()[0].system;
    let texts: Vec<&str> = system.iter().map(|b| b.text.as_str()).collect();
    assert_eq!(
        texts[..4],
        [
            "Company policy.",
            "Style.",
            "From code.",
            "You are a test agent."
        ]
    );
    assert!(texts[4].starts_with("The following skills are available"));
    assert_eq!(system.len(), 5);
}

#[tokio::test]
async fn a_subagent_takes_the_host_blocks_too() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("found")]));
    let scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    let agent = builder(hosted(), provider.clone())
        .subagents(vec![scout])
        .build()
        .unwrap();
    let child = agent.subagent(Some("scout")).unwrap();
    let mut options = RunOptions::new("c1");
    options.run_id = Some(RunId::new("c1"));
    finish(child.run_child("look", options, RunId::new("p1"))).await;
    let system = &provider.requests()[0].system;
    assert_eq!(
        flags(system),
        [
            ("Company policy.", true),
            ("Style.", false),
            ("You are a test agent.", false),
            ("User-provided instructions:\n\nScout the code.", true),
        ]
    );
}

#[test]
fn the_blocks_are_in_the_contract_and_none_is_blank() {
    let provider = || Arc::new(ScriptedProvider::new([]));
    let plain = builder(spec(), provider()).build().unwrap();
    let hosted = builder(hosted(), provider()).build().unwrap();
    assert_ne!(plain.contract(), hosted.contract());
    let mut flipped = self::hosted();
    flipped.system_blocks[1].cacheable = true;
    let flipped = builder(flipped, provider()).build().unwrap();
    assert_ne!(flipped.contract(), hosted.contract(), "the flag counts");
    let blank = builder(spec(), provider())
        .system_blocks([block(" ", true)])
        .build();
    assert!(matches!(blank, Err(BuildError::Invalid(_))));
}
