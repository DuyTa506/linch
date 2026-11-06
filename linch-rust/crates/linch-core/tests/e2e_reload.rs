//! Reloading an agent's skills and subagents mid-session: each reload is
//! a reversible registration, the sessions that have a prefix keep it
//! byte for byte and are told the change at their tail, and a new session
//! takes the new catalog.

mod common;

use std::sync::Arc;

use common::subagents::*;
use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    Agent, AgentBuilder, BuildError, CatalogChangeBlock, ContentBlock, Message, PermissionMode,
    ProviderRequest, RunOptions, ScriptedProvider, ScriptedTurn, Skill, SubagentDefinition,
};
use serde_json::json;

fn skill(name: &str, description: &str) -> Skill {
    Skill::new(name, format!("/skills/{name}"), description, "do it")
}

fn agent_with_skills(turns: Vec<ScriptedTurn>) -> (Agent, Arc<ScriptedProvider>) {
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(provider.clone())
        .permissions(policy(PermissionMode::Default))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .skills(vec![skill("alpha", "Alpha.")])
        .build()
        .unwrap();
    (agent, provider)
}

fn notices(message: &Message) -> Vec<&CatalogChangeBlock> {
    (message.content.iter())
        .filter_map(|block| match block {
            ContentBlock::CatalogChange(change) => Some(change),
            _ => None,
        })
        .collect()
}

/// The notice texts of the last message of `request`.
fn tail(request: &ProviderRequest) -> Vec<String> {
    let last = request.messages.last().unwrap();
    notices(last).iter().map(|n| n.text.clone()).collect()
}

fn reminder(lines: &str) -> String {
    let header = "The available catalog changed during this session.";
    format!("<system-reminder>\n{header}\n{lines}\n</system-reminder>")
}

fn bytes(request: &ProviderRequest) -> (String, String) {
    let system = serde_json::to_string(&request.system).unwrap();
    let tools = serde_json::to_string(&request.tools).unwrap();
    (system, tools)
}

fn listing(request: &ProviderRequest) -> &str {
    &request.system.last().unwrap().text
}

fn skill_turn(name: &str) -> ScriptedTurn {
    let input = json!({"skill": name});
    TurnBuilder::new(MODEL)
        .tool_call("c1", "Skill", &input)
        .usage(usage(10, 5))
        .build()
}

#[tokio::test]
async fn a_skill_reload_is_told_and_leaves_the_listing_bytes_alone() {
    let turns = vec![
        text_turn("one"),
        skill_turn("beta"),
        text_turn("two"),
        text_turn("fresh"),
        text_turn("three"),
    ];
    let (agent, provider) = agent_with_skills(turns);
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let reload = vec![skill("alpha", "Alpha, renamed."), skill("beta", "Beta.")];
    let registration = agent.reload_skills(reload).unwrap();
    finish(agent.run("more", RunOptions::new("s1"))).await;
    finish(agent.run("hi", RunOptions::new("s2"))).await;
    registration.dispose();
    registration.dispose();
    finish(agent.run("back", RunOptions::new("s1"))).await;

    let requests = provider.requests();
    let [first, told, loaded, fresh, back] = &requests[..] else {
        panic!("five requests")
    };
    for later in [told, loaded, back] {
        assert_eq!(bytes(later), bytes(first), "the frozen prefix moved");
    }
    assert!(!listing(first).contains("beta"));
    let lines = "Added skills: beta: Beta.\nUpdated skills: alpha: Alpha, renamed.";
    assert_eq!(tail(told), [reminder(lines)]);
    let [change] = notices(told.messages.last().unwrap())[..] else {
        panic!("one notice")
    };
    let names: Vec<&str> = change.skills.iter().map(|s| s.name.as_str()).collect();
    assert_eq!(names, ["alpha", "beta"]);
    // The Skill tool loads from the new set.
    let result = result_texts(loaded.messages.last().unwrap());
    assert_eq!(result, ["Skill base directory: /skills/beta\n\ndo it"]);
    // A new session lists the new set; disposing the reload brings back
    // the old one, which the first session is told.
    assert!(listing(fresh).contains("- beta: Beta."));
    assert!(tail(fresh).is_empty());
    let lines = "Removed skills: beta\nUpdated skills: alpha: Alpha.";
    assert_eq!(tail(back), [reminder(lines)]);
}

/// The `Subagent` tool's description in `request`'s tools.
fn roster_listing(request: &ProviderRequest) -> &str {
    let tool = request.tools.iter().find(|t| t.name == "Subagent").unwrap();
    &tool.description
}

#[tokio::test]
async fn a_subagent_reload_runs_the_new_roster_and_tells_the_tool_update() {
    let parent = vec![
        text_turn("one"),
        spawn_turn(Some("scout"), &["check x"]),
        text_turn("two"),
        text_turn("three"),
    ];
    let family = Arc::new(Family::new(parent, vec![text_turn("x holds")]));
    let stores = Arc::new(Stores::default());
    let old = SubagentDefinition::new("elder", "Remembers.", "Recall.");
    let agent = AgentBuilder::new(spec())
        .provider(family.clone())
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .subagents(vec![old])
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let scout = SubagentDefinition::new("scout", "Reads around.", "Scout the code.");
    let registration = agent.reload_subagents(vec![scout]).unwrap();
    assert!(agent.subagent(Some("scout")).is_ok());
    finish(agent.run("more", RunOptions::new("s1"))).await;
    registration.dispose();
    registration.dispose();
    assert!(agent.subagent(Some("scout")).is_err());
    finish(agent.run("back", RunOptions::new("s1"))).await;

    let requests = family.requests_of("parent");
    let [first, told, answered, back] = &requests[..] else {
        panic!("four parent requests")
    };
    for later in [told, answered, back] {
        assert_eq!(bytes(later), bytes(first), "the frozen prefix moved");
    }
    assert!(roster_listing(first).contains("- elder: Remembers."));
    let [change] = notices(told.messages.last().unwrap())[..] else {
        panic!("one notice")
    };
    let [definition] = &change.tool_definitions[..] else {
        panic!("one tool update")
    };
    assert!(definition.description.contains("- scout: Reads around."));
    assert!(!definition.description.contains("elder"));
    let lines = format!("Updated tools: Subagent: {}", definition.description);
    assert_eq!(change.text, reminder(&lines));
    // The child ran the reloaded definition.
    let [child] = &family.requests_of("check x")[..] else {
        panic!("one child request")
    };
    assert!(
        child
            .system
            .iter()
            .any(|b| b.text.ends_with("Scout the code."))
    );
    assert_eq!(result_texts(answered.messages.last().unwrap()), ["x holds"]);
    // Disposing brings the old roster back, which the session is told.
    let [change] = notices(back.messages.last().unwrap())[..] else {
        panic!("one notice")
    };
    assert!(change.text.contains("- elder: Remembers."));
}

#[tokio::test]
async fn a_reload_needs_the_feature_at_build() {
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(Arc::new(ScriptedProvider::new([])))
        .permissions(policy(PermissionMode::Default))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .build()
        .unwrap();
    let skills = agent.reload_skills(Vec::new());
    assert!(matches!(skills, Err(BuildError::Invalid(_))), "{skills:?}");
    let definition = SubagentDefinition::new("scout", "Scouts.", "Look.");
    let subagents = agent.reload_subagents(vec![definition]);
    assert!(matches!(subagents, Err(BuildError::Invalid(_))));
}

/// R3-core 5: an agent's reloads go with it, so the registry it shared is
/// clean again and takes a new agent.
#[tokio::test]
async fn a_subagent_reload_goes_with_its_agent() {
    let stores = Arc::new(Stores::default());
    let tools = registry(Vec::new());
    let build = || {
        AgentBuilder::new(spec())
            .provider(Arc::new(ScriptedProvider::new([])))
            .tools(tools.clone())
            .permissions(policy(PermissionMode::Default))
            .run_store(stores.clone())
            .session_store(stores.clone())
            .namespace(NS)
            .subagents(Vec::new())
            .build()
    };
    let agent = build().unwrap();
    let definition = SubagentDefinition::new("scout", "Scouts.", "Look.");
    let reload = agent.reload_subagents(vec![definition]).unwrap();
    drop(agent);
    assert!(
        tools.snapshot().resolve("Subagent").is_none(),
        "no tool left"
    );
    let again = build();
    assert!(again.is_ok(), "{:?}", again.err());
    reload.dispose();
    let tool = tools.snapshot().resolve("Subagent").is_some();
    assert!(tool, "the old handle leaves the new agent's tool alone");
    drop(again);
}
