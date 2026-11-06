use linch_core::{AgentSpec, SubagentDefinition, Workers};

use super::*;

mod memory;

fn names(definitions: &[SubagentDefinition]) -> Vec<&str> {
    definitions.iter().map(|d| d.name.as_str()).collect()
}

#[test]
fn the_built_in_profiles_are_pythons() {
    let profiles = DeepAgentProfile::built_in();
    let listed: Vec<&str> = profiles.iter().map(|p| p.name.as_str()).collect();
    assert_eq!(listed, PROFILE_NAMES);
    let balanced = DeepAgentProfile::named("balanced").unwrap();
    assert_eq!(
        balanced,
        DeepAgentProfile {
            name: "balanced".into(),
            durable: true,
            coordinator: false,
            max_turns: Some(64),
            max_tokens: Some(1_000_000),
            retain_subagents: true,
            enable_background_subagents: true,
        }
    );
    let coordinator = DeepAgentProfile::named("coordinator").unwrap();
    assert!(coordinator.coordinator);
    assert_eq!(
        (coordinator.max_turns, coordinator.max_tokens),
        (Some(64), Some(1_000_000))
    );
    let unbounded = DeepAgentProfile::named("unbounded").unwrap();
    assert_eq!((unbounded.max_turns, unbounded.max_tokens), (None, None));
    assert!(unbounded.durable && !unbounded.coordinator);
    let workers = Workers {
        background: true,
        retain: true,
    };
    assert!(profiles.iter().all(|p| p.workers() == workers));
}

#[test]
fn an_unknown_profile_names_the_choices() {
    let error = DeepAgentProfile::named("fast").unwrap_err();
    assert_eq!(
        error.to_string(),
        "unknown deep-agent profile \"fast\"; choose one of: balanced, coordinator, unbounded"
    );
    assert!(DeepAgent::named("Balanced").is_err(), "names are exact");
}

#[test]
fn a_profile_reads_strictly() {
    let value = serde_json::json!({
        "name": "mine", "durable": false, "coordinator": false,
        "retain_subagents": false, "enable_background_subagents": true,
    });
    let profile: DeepAgentProfile = serde_json::from_value(value.clone()).unwrap();
    assert_eq!((profile.max_turns, profile.max_tokens), (None, None));
    let workers = profile.workers();
    assert!(workers.background && !workers.retain);
    let mut extra = value;
    extra["budget"] = 1.into();
    assert!(serde_json::from_value::<DeepAgentProfile>(extra).is_err());
}

#[test]
fn the_preset_takes_its_settings_from_the_profile_unless_overridden() {
    let deep = DeepAgent::named("coordinator").unwrap();
    assert!(deep.durable && deep.coordinator);
    let mut deep = DeepAgent::named("balanced").unwrap();
    assert!(deep.durable && !deep.coordinator);
    deep.durable = false;
    deep.coordinator = true;
    assert_eq!(deep.profile.name, "balanced");
    assert_eq!(deep.system_prompt(), COORDINATOR_SYSTEM_PROMPT);
    deep.coordinator = false;
    assert_eq!(deep.system_prompt(), DEEP_AGENT_SYSTEM_PROMPT);
    assert_eq!(deep.workers(), deep.profile.workers());
}

#[test]
fn the_spec_gets_the_preset_prompt_first_and_the_profiles_turn_limit() {
    let deep = DeepAgent::named("balanced").unwrap();
    let mut spec = AgentSpec::new("m");
    spec.system_prompt = "Answer in French.".into();
    let spec = deep.spec(spec);
    let expected = format!("{DEEP_AGENT_SYSTEM_PROMPT}\n\nAnswer in French.");
    assert_eq!(spec.system_prompt, expected);
    assert_eq!(spec.max_turns, Some(64));

    let bare = deep.spec(AgentSpec::new("m"));
    assert_eq!(bare.system_prompt, DEEP_AGENT_SYSTEM_PROMPT);
    let mut capped = AgentSpec::new("m");
    capped.max_turns = Some(5);
    assert_eq!(
        deep.spec(capped).max_turns,
        Some(5),
        "the spec's own limit wins"
    );
    let unbounded = DeepAgent::named("unbounded").unwrap();
    assert_eq!(unbounded.spec(AgentSpec::new("m")).max_turns, None);
    let coordinator = DeepAgent::named("coordinator").unwrap();
    let spec = coordinator.spec(AgentSpec::new("m"));
    assert!(
        spec.system_prompt
            .starts_with("Coordinator operating policy:")
    );
}

#[test]
fn the_roster_is_pythons() {
    let roster = deep_agent_subagents();
    assert_eq!(
        names(&roster),
        ["researcher", "planner", "implementer", "verification"]
    );
    let tools = |name: &str| {
        let definition = roster.iter().find(|d| d.name == name).unwrap();
        definition.tools.clone().unwrap()
    };
    let read = [
        "Read",
        "Glob",
        "Grep",
        "TaskList",
        "TaskGet",
        "ls",
        "read_file",
    ];
    for name in ["researcher", "planner", "verification"] {
        assert_eq!(tools(name), read, "{name}");
    }
    let implementer = [
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "Bash",
        "TaskList",
        "TaskGet",
        "TaskUpdate",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
    ];
    assert_eq!(tools("implementer"), implementer);
    let verification = roster.iter().find(|d| d.name == "verification").unwrap();
    assert!(
        verification
            .body
            .ends_with("unless you observed its result.")
    );
    assert!(
        verification
            .body
            .contains("VERDICT: PASS, VERDICT: FAIL, or VERDICT: PARTIAL")
    );
}

#[test]
fn project_subagents_override_the_roster_by_name() {
    let deep = DeepAgent::named("balanced").unwrap();
    let mine = SubagentDefinition::new("Researcher", "mine", "my body");
    let extra = SubagentDefinition::new("reviewer", "review", "body");
    let roster = deep.subagents(vec![mine.clone(), extra.clone()]);
    assert_eq!(
        names(&roster),
        [
            "Researcher",
            "reviewer",
            "planner",
            "implementer",
            "verification"
        ]
    );
    assert_eq!(roster[0], mine);
    assert_eq!(
        names(&deep.subagents(Vec::new())),
        names(&deep_agent_subagents())
    );
}

#[test]
fn the_prompts_are_pythons_word_for_word() {
    // Python's `len()` of each prompt counts characters.
    assert_eq!(DEEP_AGENT_SYSTEM_PROMPT.chars().count(), 3469);
    assert!(DEEP_AGENT_SYSTEM_PROMPT.starts_with("Deep agent operating policy:\n\nYou are"));
    assert!(DEEP_AGENT_SYSTEM_PROMPT.ends_with("remaining\n  risks or blockers."));
    assert_eq!(COORDINATOR_SYSTEM_PROMPT.chars().count(), 3020);
    assert!(COORDINATOR_SYSTEM_PROMPT.starts_with("Coordinator operating policy:\n\nYou are"));
    assert!(COORDINATOR_SYSTEM_PROMPT.ends_with("Do not repeat tool output verbatim."));
}

#[test]
fn the_coordinator_tool_list_is_pythons() {
    let mut excluded = COORDINATOR_EXCLUDED_TOOLS.to_vec();
    excluded.sort_unstable();
    assert_eq!(excluded, ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]);
}
