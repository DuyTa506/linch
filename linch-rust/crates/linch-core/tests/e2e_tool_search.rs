//! Tool search: past a threshold the session's prefix offers only the
//! always-loaded tools and `tool_search`; a search loads what it found for
//! the next request, told at the tail, and the prefix stays byte for byte.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::provider::scripted::TurnBuilder;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, CatalogChangeBlock, ContentBlock, JsonValue, Message, PermissionMode,
    ProviderRequest, RunOptions, RunStore, ScriptedProvider, ScriptedTurn, SessionStore, Skill,
    ToolScope, ToolSearch,
};
use serde_json::json;

const DESCRIPTION: &str = "Search deferred tools and skills by keywords or select:a,b. \
                           Returns tool schemas and skill descriptions; tools load for the \
                           next request.";

fn searching<S: RunStore + SessionStore + 'static>(
    provider: Arc<dyn linch_core::Provider>,
    search: ToolSearch,
    skills: Option<Vec<Skill>>,
    stores: Arc<S>,
) -> Agent {
    let runs = Runs::default();
    let tools = registry(vec![
        Echo::new("Read", ToolScope::Read, &runs),
        Echo::new("Write", ToolScope::Write, &runs),
    ]);
    let mut builder = AgentBuilder::new(spec())
        .provider(provider)
        .tools(tools)
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS)
        .tool_search(search);
    if let Some(skills) = skills {
        builder = builder.skills(skills);
    }
    builder.build().unwrap()
}

fn tools_over(max_tools: usize) -> ToolSearch {
    ToolSearch {
        max_tools,
        ..ToolSearch::default()
    }
}

fn search(id: &str, query: &str) -> TurnBuilder {
    let input = json!({"query": query});
    TurnBuilder::new(MODEL).tool_call(id, "tool_search", &input)
}

fn read(builder: TurnBuilder, id: &str) -> ScriptedTurn {
    let input = json!({"value": "x"});
    builder
        .tool_call(id, "Read", &input)
        .usage(usage(10, 5))
        .build()
}

fn names(request: &ProviderRequest) -> Vec<&str> {
    request.tools.iter().map(|t| t.name.as_str()).collect()
}

fn notices(message: &Message) -> Vec<&CatalogChangeBlock> {
    (message.content.iter())
        .filter_map(|block| match block {
            ContentBlock::CatalogChange(change) => Some(change),
            _ => None,
        })
        .collect()
}

fn reminder(lines: &str) -> String {
    let header = "The available catalog changed during this session.";
    format!("<system-reminder>\n{header}\n{lines}\n</system-reminder>")
}

fn system_bytes(request: &ProviderRequest) -> String {
    serde_json::to_string(&request.system).unwrap()
}

fn deferred(name: &str) -> String {
    format!("Tool '{name}' is deferred; call tool_search with select:{name} first")
}

#[tokio::test]
async fn a_search_loads_a_deferred_tool_for_the_next_request() {
    let turns = vec![
        read(TurnBuilder::new(MODEL), "r0"),
        read(search("s1", "select:Read"), "r1"),
        read(TurnBuilder::new(MODEL), "r2"),
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let agent = searching(provider.clone(), tools_over(1), None, stores);
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let requests = provider.requests();
    let [first, refused, loaded, ran] = &requests[..] else {
        panic!("four requests")
    };
    assert_eq!(names(first), ["tool_search"]);
    let lines = "\nDeferred tools: Read, Write\nDeferred skills: ";
    assert_eq!(first.tools[0].description, format!("{DESCRIPTION}{lines}"));
    assert_eq!(
        result_texts(refused.messages.last().unwrap()),
        [deferred("Read")]
    );
    assert_eq!(names(refused), ["tool_search"]);
    // The search loads Read for the next request, not its own.
    let results = result_texts(loaded.messages.last().unwrap());
    let found: JsonValue = serde_json::from_str(&results[0]).unwrap();
    assert_eq!(found["tools"][0]["name"], "Read");
    assert_eq!(found["skills"], json!([]));
    assert_eq!(results[1], deferred("Read"));
    let [change] = notices(loaded.messages.last().unwrap())[..] else {
        panic!("one notice")
    };
    assert_eq!(change.text, reminder("Added tools: Read"));
    assert_eq!(names(loaded), ["tool_search", "Read"]);
    for later in [refused, loaded, ran] {
        assert_eq!(system_bytes(later), system_bytes(first));
        assert_eq!(later.tools[0], first.tools[0], "the frozen tools moved");
    }
    assert_eq!(result_texts(ran.messages.last().unwrap()), ["x"]);
    assert_eq!(names(ran), ["tool_search", "Read"]);
}

#[tokio::test]
async fn below_the_thresholds_every_tool_is_offered_and_search_is_not() {
    let turns = vec![search("s1", "read").build(), text_turn("done")];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let agent = searching(provider.clone(), ToolSearch::default(), None, stores);
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let requests = provider.requests();
    assert_eq!(names(&requests[0]), ["Read", "Write"]);
    let results = result_texts(requests[1].messages.last().unwrap());
    assert_eq!(results, ["No deferred catalog is offered on this turn."]);
}

#[tokio::test]
async fn a_keyword_search_loads_a_deferred_skill() {
    let skills = vec![
        Skill::new("alpha", "/skills/alpha", "Alpha things.", "do alpha"),
        Skill::new("beta", "/skills/beta", "Beta stuff.", "do beta"),
    ];
    let skill = json!({"skill": "beta"});
    let turns = vec![
        search("s1", "beta STUFF").usage(usage(10, 5)).build(),
        (TurnBuilder::new(MODEL).tool_call("k1", "Skill", &skill))
            .usage(usage(10, 5))
            .build(),
        text_turn("done"),
    ];
    let provider = Arc::new(ScriptedProvider::new(turns));
    let stores = Arc::new(Stores::default());
    let search_skills = ToolSearch {
        max_skills: 1,
        ..ToolSearch::default()
    };
    let agent = searching(provider.clone(), search_skills, Some(skills), stores);
    finish(agent.run("go", RunOptions::new("s1"))).await;

    let requests = provider.requests();
    let [first, loaded, ran] = &requests[..] else {
        panic!("three requests")
    };
    // Tools stay inline; only the skills are deferred.
    assert_eq!(names(first), ["Read", "Write", "Skill", "tool_search"]);
    let listing = &first.system.last().unwrap().text;
    assert!(
        listing.ends_with("Deferred skills (use tool_search first): alpha, beta, verify"),
        "{listing}"
    );
    let search = first
        .tools
        .iter()
        .find(|t| t.name == "tool_search")
        .unwrap();
    let lines = "\nDeferred tools: \nDeferred skills: alpha, beta, verify";
    assert_eq!(search.description, format!("{DESCRIPTION}{lines}"));
    let results = result_texts(loaded.messages.last().unwrap());
    let found: JsonValue = serde_json::from_str(&results[0]).unwrap();
    let beta = json!([{"name": "beta", "description": "Beta stuff."}]);
    assert_eq!(found["skills"], beta);
    let [change] = notices(loaded.messages.last().unwrap())[..] else {
        panic!("one notice")
    };
    assert_eq!(change.text, reminder("Added skills: beta: Beta stuff."));
    assert_eq!(system_bytes(loaded), system_bytes(first));
    let skill = result_texts(ran.messages.last().unwrap());
    assert_eq!(skill, ["Skill base directory: /skills/beta\n\ndo beta"]);
}

#[tokio::test]
async fn a_crash_at_any_write_loads_a_found_tool_once() {
    let turns = || {
        vec![
            search("s1", "select:Read").usage(usage(10, 5)).build(),
            read(TurnBuilder::new(MODEL), "r1"),
            text_turn("done"),
        ]
    };
    let baseline = Arc::new(Stores::default());
    let clean = searching(
        Arc::new(Replay::new(turns())),
        tools_over(1),
        None,
        baseline.clone(),
    );
    finish(clean.run("go", RunOptions::new("s1"))).await;
    let writes = baseline.log.lock().unwrap().len();
    for n in 1..=writes as u64 {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let provider = Arc::new(Replay::new(turns()));
            let crashing = searching(provider.clone(), tools_over(1), None, faulty);
            let clean = searching(provider, tools_over(1), None, stores.clone());
            let stream = crashing.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            let (_, error) = drain(stream).await;
            assert!(error.is_some(), "{context}: the run must stop");
            match stores.runs.load(NS, &run_id).await {
                Ok(record) if record.status.is_terminal() => {}
                Ok(_) => drop(finish(clean.resume(run_id, RunOptions::new("s1"))).await),
                Err(_) => drop(finish(clean.run("go", RunOptions::new("s1"))).await),
            }
            let logged = messages(&*stores, "s1").await;
            let texts: Vec<&str> = (logged.iter().flat_map(notices))
                .map(|n| n.text.as_str())
                .collect();
            // A crash right after the search's start leaves it interrupted:
            // it found nothing, so nothing loaded and Read is refused.
            let found = result_texts(&logged[2])[0].starts_with('{');
            let read = &result_texts(&logged[4])[0];
            if found {
                assert_eq!(texts, [reminder("Added tools: Read")], "{context}");
                // Offered: it ran, unless a crash interrupted it.
                assert_ne!(*read, deferred("Read"), "{context}");
            } else {
                assert!(texts.is_empty(), "{context}");
                assert_eq!(*read, deferred("Read"), "{context}");
            }
        }
    }
}
