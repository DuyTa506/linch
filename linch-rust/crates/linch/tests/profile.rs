//! Domain profiles: a versioned role as data, layered, inspected and
//! hashed, and applied to an agent when it opens.

use futures::StreamExt;
use linch::provider::scripted::TurnBuilder;
use linch::{AgentConfig, Event, OpenError, Profile, ProfileError, RunOptions};
use serde_json::{Value, json};

const MODEL: &str = "scripted-model";

fn profile(value: Value) -> Profile {
    serde_json::from_value(value).unwrap()
}

#[test]
fn a_profile_needs_an_identity_and_sane_limits() {
    let base = Profile::new("support", "2");
    assert_eq!(base.validate(), Ok(()));
    for (bad, field) in [
        (json!({"id": " ", "version": "1"}), "id"),
        (json!({"id": "a", "version": ""}), "version"),
        (
            json!({"id": "a", "version": "1", "max_turns": 0}),
            "max_turns",
        ),
        (
            json!({"id": "a", "version": "1", "tool_deny": ["Read", " "]}),
            "tool_deny",
        ),
    ] {
        let error = profile(bad).validate().unwrap_err();
        assert!(
            matches!(&error, ProfileError::Invalid { field: f, .. } if *f == field),
            "{error}"
        );
    }
    let unknown = json!({"id": "a", "version": "1", "plugins": {}});
    assert!(serde_json::from_value::<Profile>(unknown).is_err());
}

#[test]
fn a_later_layer_replaces_what_it_sets_and_inherits_the_rest() {
    let base = profile(json!({
        "id": "base", "version": "1", "max_turns": 8,
        "system_prompt": "Be kind.", "tool_deny": ["Bash"],
    }));
    let support = base.layer(&profile(json!({
        "id": "support", "version": "2", "system_prompt": "Help customers.",
    })));
    assert_eq!(
        support,
        profile(json!({
            "id": "support", "version": "2", "max_turns": 8,
            "system_prompt": "Help customers.", "tool_deny": ["Bash"],
        }))
    );
    let open = support.layer(&profile(
        json!({"id": "t", "version": "3", "tool_deny": []}),
    ));
    assert_eq!(open.tool_deny, Some(Vec::new()), "an empty list replaces");
    assert_eq!(open.max_turns, Some(8));
}

#[test]
fn resolve_shows_every_field_and_the_hash_covers_them() {
    let support = profile(json!({"id": "support", "version": "2", "max_turns": 8}));
    assert_eq!(
        support.resolve(),
        json!({
            "id": "support", "version": "2", "system_prompt": null,
            "tool_deny": null, "max_turns": 8,
        })
    );
    let hash = support.content_hash();
    assert!(
        hash.starts_with("sha256:") && hash.len() == 7 + 64,
        "{hash}"
    );
    let same = Profile::new("x", "0").layer(&support);
    assert_eq!(same.content_hash(), hash, "equal content, equal hash");
    let mut changed = support.clone();
    changed.max_turns = Some(9);
    assert_ne!(changed.content_hash(), hash);
    let mut bumped = support.clone();
    bumped.version = "3".into();
    assert_ne!(bumped.content_hash(), hash);
    assert_eq!(
        support.reference(),
        json!({"id": "support", "version": "2", "content_hash": hash})
    );
}

fn config(turns: Vec<Value>, extra: Value) -> AgentConfig {
    let mut value = json!({
        "namespace": "t",
        "spec": {"model": MODEL, "system_prompt": "You are Acme's assistant."},
        "provider": {"id": "scripted", "turns": turns},
        "permissions": {"mode": "skip_dangerous", "project_root": "/"},
    });
    let fields = value.as_object_mut().unwrap();
    fields.extend(extra.as_object().unwrap().clone());
    serde_json::from_value(value).unwrap()
}

#[tokio::test]
async fn an_agent_opened_with_a_profile_takes_its_role() {
    let off = config(Vec::new(), json!({}));
    assert!(serde_json::to_value(&off).unwrap().get("profile").is_none());
    let plain = off.open().await.unwrap();
    assert_eq!(plain.agent().spec().max_turns, None);

    let dir = tempfile::tempdir().unwrap();
    let input = json!({"file_path": "notes.txt", "content": "x"});
    let turns = vec![
        json!(
            TurnBuilder::new(MODEL)
                .tool_call("call_1", "Write", &input)
                .build()
        ),
        json!(TurnBuilder::new(MODEL).text("Could not write.").build()),
    ];
    let support = json!({
        "id": "support", "version": "2", "max_turns": 5,
        "system_prompt": "Help customers resolve product issues.",
        "tool_deny": ["Write", "Bash"],
    });
    let extra = json!({"workspace": dir.path(), "profile": support});
    let opened = config(turns, extra).open().await.unwrap();
    let spec = opened.agent().spec();
    let prompt = "You are Acme's assistant.\n\nHelp customers resolve product issues.";
    assert_eq!(spec.system_prompt, prompt);
    assert_eq!(spec.max_turns, Some(5));
    assert_ne!(opened.agent().contract(), plain.agent().contract());

    let mut stream = opened.agent().run("go", RunOptions::new("s1"));
    let mut denied = None;
    while let Some(item) = stream.next().await {
        if let Event::ToolCallEnd(end) = item.unwrap().event {
            denied = Some(end.result.is_error);
        }
    }
    assert_eq!(denied, Some(true), "a denied tool never runs");
    assert!(!dir.path().join("notes.txt").exists());
}

#[tokio::test]
async fn an_invalid_profile_does_not_open() {
    let bad = config(Vec::new(), json!({"profile": {"id": "", "version": "1"}}));
    let error = bad.open().await.unwrap_err();
    assert!(matches!(error, OpenError::Profile(_)), "{error}");
}
