//! The deep agent preset an `AgentConfig` turns on: off by default; its
//! profile, durability check, tools, roster, workers, instructions, turn
//! limit and file memory; and what the config sets itself winning.

use std::sync::Arc;

use futures::StreamExt;
use linch::ext::deep_agent::{COORDINATOR_SYSTEM_PROMPT, DEEP_AGENT_SYSTEM_PROMPT, DeepAgentError};
use linch::provider::scripted::TurnBuilder;
use linch::tools::vfs::PROMPT;
use linch::{
    AgentConfig, Event, Hooks, OpenError, OpenedAgent, PgPools, RunOptions, ToolRegistry, Workers,
};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";
const MEMORY_POLICY: &str = "linch.deep-agent.file-memory-context";

fn call(id: &str, tool: &str, input: Value) -> Value {
    json!(TurnBuilder::new(MODEL).tool_call(id, tool, &input).build())
}

fn text(text: &str) -> Value {
    json!(TurnBuilder::new(MODEL).text(text).build())
}

/// A scripted config allowing every call, with `extra` fields on top.
fn config(turns: Vec<Value>, extra: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

/// An ephemeral deep agent: the balanced profile on memory stores.
fn ephemeral() -> Value {
    json!({"deep_agent": {"durable": false}})
}

fn with(mut base: Value, extra: Value) -> Value {
    let fields = base.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    base
}

/// Open `config` on a registry the test keeps, to read tool descriptions.
async fn open(config: AgentConfig) -> (OpenedAgent, Arc<ToolRegistry>) {
    let tools = Arc::new(ToolRegistry::new());
    let hooks = Arc::new(Hooks::default());
    let opened = (config.open_with(Arc::clone(&tools), hooks, &PgPools::new()))
        .await
        .unwrap();
    (opened, tools)
}

fn description(tools: &ToolRegistry, name: &str) -> String {
    let definitions = tools.snapshot().definitions();
    let definition = definitions.into_iter().find(|d| d.name == name).unwrap();
    definition.description
}

/// Every envelope of a run of `prompt` in `session`, as events.
async fn run(opened: &OpenedAgent, session: &str) -> Vec<Event> {
    let mut stream = opened.agent().run("go", RunOptions::new(session));
    let mut events = Vec::new();
    while let Some(item) = stream.next().await {
        events.push(item.unwrap().event);
    }
    events
}

fn offered(events: &[Event]) -> Vec<String> {
    let init = events.iter().find_map(|event| match event {
        Event::Init(init) => Some(init.tools.clone()),
        _ => None,
    });
    let mut tools = init.unwrap();
    tools.sort();
    tools
}

#[tokio::test]
async fn the_preset_is_off_unless_the_config_turns_it_on() {
    let off = config(vec![text("hi")], json!({}));
    assert!(off.deep_agent.is_none());
    let echoed = serde_json::to_value(&off).unwrap();
    assert!(echoed.get("deep_agent").is_none(), "{echoed}");
    let opened = off.open().await.unwrap();
    assert_eq!(opened.agent().spec().system_prompt, "");
    assert_eq!(opened.agent().spec().max_turns, None);
    assert!(offered(&run(&opened, "s1").await).is_empty());

    let on = config(vec![], json!({"deep_agent": {}}));
    let echoed = serde_json::to_value(&on).unwrap();
    assert_eq!(echoed["deep_agent"], json!({"profile": "balanced"}));
    let mut unknown = echoed;
    unknown["deep_agent"] = json!({"budget": 1});
    assert!(serde_json::from_value::<AgentConfig>(unknown).is_err());
}

#[tokio::test]
async fn a_durable_deep_agent_needs_postgres_stores() {
    for preset in [
        json!({}),
        json!({"profile": "coordinator"}),
        json!({"durable": true}),
    ] {
        let config = config(vec![], json!({"deep_agent": preset}));
        let error = config.open().await.unwrap_err();
        assert!(
            matches!(error, OpenError::DeepAgent(DeepAgentError::NotDurable)),
            "{error}"
        );
        assert!(error.to_string().contains("Postgres stores"), "{error}");
    }
    let unknown = config(vec![], json!({"deep_agent": {"profile": "fast"}}));
    let error = unknown.open().await.unwrap_err();
    assert!(
        error
            .to_string()
            .contains("choose one of: balanced, coordinator, unbounded"),
        "{error}"
    );
}

#[tokio::test]
async fn a_deep_agent_has_the_presets_tools_instructions_and_limit() {
    let dir = tempfile::tempdir().unwrap();
    let extra = with(
        ephemeral(),
        json!({"workspace": dir.path(), "spec": {
            "model": MODEL, "system_prompt": "Answer in French.",
        }}),
    );
    let (opened, tools) = open(config(vec![text("hi")], extra)).await;
    let spec = opened.agent().spec();
    let prompt = format!("{DEEP_AGENT_SYSTEM_PROMPT}\n\nAnswer in French.\n\n{PROMPT}");
    assert_eq!(spec.system_prompt, prompt);
    assert_eq!(spec.max_turns, Some(64));
    let mut expected = [
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "Read",
        "Write",
        "TaskCreate",
        "TaskList",
        "TaskGet",
        "TaskUpdate",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "Skill",
        "Subagent",
        "SubagentContinue",
        "TaskStop",
    ];
    expected.sort_unstable();
    let events = run(&opened, "s1").await;
    assert_eq!(offered(&events), expected);
    let listing = description(&tools, "Subagent");
    for name in ["researcher", "planner", "implementer", "verification"] {
        let line = format!("\n- {name}: ");
        assert_eq!(
            listing.matches(&line).count(),
            1,
            "{name} once in:\n{listing}"
        );
    }
    assert!(listing.contains("- verification: Read-only adversarial reviewer"));
    let memory = events
        .iter()
        .any(|event| matches!(event, Event::Hook(hook) if hook.policy_id == MEMORY_POLICY));
    assert!(memory, "the file memory hook runs for each request");
}

#[tokio::test]
async fn what_the_config_sets_itself_wins() {
    let extra = with(
        ephemeral(),
        json!({
            "spec": {"model": MODEL, "max_turns": 3},
            "workers": {"background": false, "retain": true},
            "filesystem": {"offload_threshold_tokens": 500},
        }),
    );
    let config = config(vec![text("hi")], extra);
    let expected_workers = Workers {
        background: false,
        retain: true,
    };
    assert_eq!(config.workers, Some(expected_workers));
    let (opened, _) = open(config).await;
    assert_eq!(opened.agent().spec().max_turns, Some(3));
    let tools = offered(&run(&opened, "s1").await);
    assert!(tools.contains(&"SubagentContinue".to_owned()));
    assert!(
        !tools.contains(&"TaskStop".to_owned()),
        "no background workers"
    );

    let unbounded = json!({"deep_agent": {"profile": "unbounded", "durable": false}});
    let (opened, _) = open(config_of(unbounded)).await;
    assert_eq!(opened.agent().spec().max_turns, None);
    let custom = json!({"deep_agent": {"profile": {
        "name": "mine", "durable": false, "coordinator": false, "max_turns": 9,
        "retain_subagents": false, "enable_background_subagents": false,
    }}});
    let (opened, _) = open(config_of(custom)).await;
    assert_eq!(opened.agent().spec().max_turns, Some(9));
    let tools = offered(&run(&opened, "s1").await);
    assert!(
        !tools.contains(&"SubagentContinue".to_owned()),
        "workers off"
    );
}

fn config_of(extra: Value) -> AgentConfig {
    config(vec![text("hi")], extra)
}

#[tokio::test]
async fn a_project_subagent_overrides_the_roster_entry_of_its_name() {
    let dir = tempfile::tempdir().unwrap();
    let agents = dir.path().join(".linch/agents");
    std::fs::create_dir_all(&agents).unwrap();
    let file = "---\ndescription: My own researcher.\ntools: [Read]\n---\nResearch.";
    std::fs::write(agents.join("researcher.md"), file).unwrap();
    let base = with(ephemeral(), json!({"workspace": dir.path()}));
    let (_opened, tools) = open(config(vec![], base.clone())).await;
    let listing = description(&tools, "Subagent");
    assert!(
        listing.contains("- researcher: Read-heavy specialist"),
        "not loaded unless on"
    );

    let (_opened, tools) = open(config(vec![], with(base, json!({"subagents": true})))).await;
    let listing = description(&tools, "Subagent");
    assert!(
        listing.contains("- researcher: My own researcher."),
        "{listing}"
    );
    assert_eq!(listing.matches("\n- researcher: ").count(), 1);
    assert!(listing.contains("- planner: "));
}

#[tokio::test]
async fn a_reload_keeps_the_roster_and_the_skill_tool() {
    let dir = tempfile::tempdir().unwrap();
    let agents = dir.path().join(".linch/agents");
    std::fs::create_dir_all(&agents).unwrap();
    let file = "---\ndescription: My own researcher.\ntools: [Read]\n---\nResearch.";
    std::fs::write(agents.join("researcher.md"), file).unwrap();
    let extra = json!({"workspace": dir.path(), "skills": true, "subagents": true});
    let (opened, tools) = open(config(vec![], with(ephemeral(), extra))).await;
    let (_subagents, skipped) = opened.reload_subagents().await.unwrap();
    assert!(skipped.is_empty(), "{skipped:?}");
    let listing = description(&tools, "Subagent");
    for name in ["researcher", "planner", "implementer", "verification"] {
        let line = format!("\n- {name}: ");
        assert_eq!(
            listing.matches(&line).count(),
            1,
            "{name} once in:\n{listing}"
        );
    }
    assert!(
        listing.contains("- researcher: My own researcher."),
        "{listing}"
    );
    let (_skills, _) = opened.reload_skills().await.unwrap();
    assert!(
        tools.snapshot().resolve("Skill").is_some(),
        "Skill is still offered"
    );
}

#[tokio::test]
async fn a_retained_worker_answers_with_its_id() {
    let turns = vec![
        call(
            "call_1",
            "Subagent",
            json!({"description": "look", "prompt": "Find the answer.", "subagent_type": "researcher"}),
        ),
        text("Found it."),
        text("The researcher found it."),
    ];
    let (opened, _) = open(config(turns, ephemeral())).await;
    let events = run(&opened, "s1").await;
    let result = events.iter().find_map(|event| match event {
        Event::ToolCallEnd(end) if end.tool_name == "Subagent" => {
            Some(serde_json::to_value(&end.result).unwrap())
        }
        _ => None,
    });
    let text = result.unwrap()["content"][0]["text"]
        .as_str()
        .unwrap()
        .to_owned();
    assert!(text.starts_with("Found it."), "{text}");
    assert!(
        text.contains("[Worker ID: "),
        "balanced retains workers: {text}"
    );
}

#[tokio::test]
async fn a_coordinator_gets_its_instructions_but_keeps_the_workspace_tools() {
    let dir = tempfile::tempdir().unwrap();
    let preset = json!({"deep_agent": {"profile": "coordinator", "durable": false}});
    let extra = with(preset, json!({"workspace": dir.path()}));
    let (opened, _) = open(config(vec![text("hi")], extra)).await;
    let prompt = &opened.agent().spec().system_prompt;
    assert!(prompt.starts_with(COORDINATOR_SYSTEM_PROMPT), "{prompt}");
    assert!(!prompt.contains(DEEP_AGENT_SYSTEM_PROMPT));
    // Python hides these from a coordinator; the kernel cannot hide a tool
    // from a parent while its workers keep it, so they stay offered.
    let tools = offered(&run(&opened, "s1").await);
    assert!(tools.contains(&"Read".to_owned()) && tools.contains(&"Bash".to_owned()));

    let overridden = json!({"deep_agent": {"coordinator": true, "durable": false}});
    let (opened, _) = open(config_of(overridden)).await;
    let prompt = &opened.agent().spec().system_prompt;
    assert!(prompt.starts_with(COORDINATOR_SYSTEM_PROMPT));
}

#[tokio::test]
async fn a_failed_open_takes_the_memory_hook_back() {
    let hooks = Arc::new(Hooks::default());
    let mut value = serde_json::to_value(config(vec![], ephemeral())).unwrap();
    value["namespace"] = json!(" ");
    let blank: AgentConfig = serde_json::from_value(value).unwrap();
    let (tools, pools) = (Arc::new(ToolRegistry::new()), PgPools::new());
    let opened = blank.open_with(tools, Arc::clone(&hooks), &pools);
    assert!(matches!(opened.await, Err(OpenError::Build(_))));
    assert!(hooks.context_build.is_empty());

    let tools = Arc::new(ToolRegistry::new());
    let opened = config(vec![], ephemeral()).open_with(tools, Arc::clone(&hooks), &pools);
    let _opened = opened.await.unwrap();
    assert!(!hooks.context_build.is_empty(), "an open agent keeps it");
}
