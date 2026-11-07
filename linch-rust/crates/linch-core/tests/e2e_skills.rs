//! Skills in a run: off in a bare agent, the listing in the cached system
//! prefix, the `Skill` tool's substituted body, the contract, the tool's
//! registration, and the body put back after a compaction folds it away.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::{
    AgentBuilder, BuildError, CompactionSpec, ContentBlock, LogEntry, MemoryRunStore,
    MemorySessionStore, PermissionMode, RunOptions, ScriptedProvider, SessionId, SessionStore,
    Skill, ToolRegistry, ToolScope,
};
use serde_json::json;

fn fmt_skill() -> Skill {
    let mut skill = Skill::new(
        "fmt",
        "/repo/.linch/skills/fmt",
        "Format a file.",
        "fix $ARGUMENTS",
    );
    skill.argument_hint = Some("path".into());
    skill
}

fn skill_turn() -> linch_core::ScriptedTurn {
    let input = json!({"skill": "fmt", "args": "a.rs"});
    TurnBuilder::new(MODEL)
        .tool_call("c1", "Skill", &input)
        .usage(usage(10, 5))
        .build()
}

fn builder(provider: Arc<ScriptedProvider>, tools: Arc<ToolRegistry>) -> AgentBuilder {
    let stores = Arc::new(Stores::default());
    AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::Default))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
}

#[tokio::test]
async fn a_bare_agent_has_no_skills() {
    let provider = Arc::new(ScriptedProvider::new([text_turn("hi")]));
    let agent = builder(provider.clone(), Arc::new(ToolRegistry::new()))
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    let request = &provider.requests()[0];
    assert!(request.tools.is_empty());
    assert_eq!(request.system.len(), 1, "the system prompt only");
}

#[tokio::test]
async fn the_skill_tool_returns_the_body_and_the_listing_is_cached() {
    let provider = Arc::new(ScriptedProvider::new([skill_turn(), text_turn("done")]));
    let stores = Arc::new(Stores::default());
    let agent = AgentBuilder::new(spec())
        .provider(provider.clone())
        .permissions(policy(PermissionMode::Default))
        .run_store(stores.clone())
        .session_store(stores.clone())
        .namespace(NS)
        .skills(vec![fmt_skill()])
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let requests = provider.requests();
    let names: Vec<&str> = requests[0].tools.iter().map(|t| t.name.as_str()).collect();
    assert_eq!(names, ["Skill"]);
    let system = &requests[0].system;
    assert_eq!(system.len(), 2);
    assert_eq!(
        (system[0].text.as_str(), system[0].cacheable),
        ("You are a test agent.", false)
    );
    assert!(system[1].cacheable, "the listing is in the cached prefix");
    assert_eq!(
        system[1].text,
        "The following skills are available for use with the Skill tool:\n\n\
         - fmt: Format a file. (args: path)\n\
         - verify: Plan and run evidence-based verification for completed work, ending \
         with a verdict. \u{2014} Use after non-trivial changes or before claiming a task is \
         complete. (args: optional task, artifact, or risk focus)"
    );
    assert_eq!(
        requests[1].system, *system,
        "every request has the same prefix"
    );
    // A read-scope tool: no approval was asked in default mode.
    let log = messages(&*stores, "s1").await;
    assert_eq!(
        result_texts(&log[2]),
        ["Skill base directory: /repo/.linch/skills/fmt\n\nfix a.rs"]
    );
}

#[tokio::test]
async fn the_listing_fits_the_provider_s_context_window() {
    let provider = ScriptedProvider::new([text_turn("hi")]).with_context_window(1000);
    let provider = Arc::new(provider);
    let agent = builder(provider.clone(), Arc::new(ToolRegistry::new()))
        .skills(vec![fmt_skill()])
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;
    // 1000 tokens give the listing 40 characters: the names only.
    let listing = &provider.requests()[0].system[1].text;
    assert!(listing.ends_with(":\n\n- fmt\n- verify"), "{listing}");
}

#[tokio::test]
async fn skills_are_part_of_the_contract() {
    let provider = Arc::new(ScriptedProvider::new([]));
    let build = |skills: Option<Vec<Skill>>| {
        let builder = builder(provider.clone(), Arc::new(ToolRegistry::new()));
        match skills {
            Some(skills) => builder.skills(skills),
            None => builder,
        }
        .build()
        .unwrap()
        .contract()
    };
    let bare = build(None);
    let builtins = build(Some(Vec::new()));
    let fmt = build(Some(vec![fmt_skill()]));
    let mut edited = fmt_skill();
    edited.body = "fix $ARGUMENTS twice".into();
    assert_ne!(bare, builtins);
    assert_ne!(builtins, fmt);
    assert_ne!(fmt, build(Some(vec![edited])));
    assert_eq!(fmt, build(Some(vec![fmt_skill()])));
}

#[tokio::test]
async fn the_skill_tool_lives_as_long_as_its_agent() {
    let provider = Arc::new(ScriptedProvider::new([]));
    let tools = Arc::new(ToolRegistry::new());
    let agent = builder(provider.clone(), tools.clone())
        .skills(Vec::new())
        .build()
        .unwrap();
    assert!(tools.snapshot().resolve("Skill").is_some());
    let second = builder(provider.clone(), tools.clone())
        .skills(Vec::new())
        .build();
    assert!(
        matches!(second, Err(BuildError::Invalid(_))),
        "the name is taken"
    );
    assert!(
        tools.snapshot().resolve("Skill").is_some(),
        "the first one stays"
    );
    let clone = agent.clone();
    drop(agent);
    assert!(
        tools.snapshot().resolve("Skill").is_some(),
        "a clone holds it"
    );
    drop(clone);
    assert!(tools.snapshot().resolve("Skill").is_none());
    let again = builder(provider, tools.clone()).skills(Vec::new()).build();
    assert!(again.is_ok());
}

#[tokio::test]
async fn a_compaction_puts_the_loaded_body_back_in_the_view_only() {
    // Turn 0 loads the skill, turn 1 reads, turn 2 compacts turn 0 away.
    let summary = TurnBuilder::new(MODEL).text("they formatted a.rs").build();
    let turns = [
        skill_turn(),
        tool_turn(1, 1, "Read"),
        summary,
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let mut spec = spec();
    spec.compaction = Some(CompactionSpec {
        keep_recent_turns: 1,
        ..CompactionSpec::new(1)
    });
    let runs = Runs::default();
    let sessions = Arc::new(MemorySessionStore::default());
    let agent = AgentBuilder::new(spec)
        .provider(provider.clone())
        .tools(registry(vec![Echo::new("Read", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::Default))
        .run_store(Arc::new(MemoryRunStore::default()))
        .session_store(sessions.clone())
        .namespace(NS)
        .skills(vec![fmt_skill()])
        .build()
        .unwrap();
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let reminder = "<system-reminder>\nBelow is the body of a previously invoked skill named \
                    'fmt'.\n\nSkill base directory: /repo/.linch/skills/fmt\n\nfix a.rs\n\
                    </system-reminder>";
    let requests = provider.requests();
    let after = &requests[3].messages[1];
    assert!(
        after
            .text()
            .starts_with("<summary of earlier conversation>")
    );
    assert_eq!(after.content.last(), Some(&ContentBlock::text(reminder)));
    let entries = SessionStore::load(&*sessions, NS, &SessionId::new("s1"))
        .await
        .unwrap();
    let historical = entries.iter().filter_map(|entry| match entry {
        LogEntry::Message(entry) if entry.historical => Some(entry.message.text()),
        _ => None,
    });
    assert!(
        historical
            .into_iter()
            .all(|text| !text.contains("Below is the body"))
    );
}
