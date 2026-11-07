//! Native catalog changes: `additional_tools` at the notice and an
//! `allowed_tools` choice, on the wire body.

use linch_core::{
    CatalogChangeBlock, ContentBlock, Message, ProviderRequest, Role, TextBlock, ToolChoice,
    ToolDefinition,
};
use serde_json::{Value, json};

use super::{MODEL, options};
use crate::openai::test_support::request;
use crate::responses::{ResponsesOptions, catalog, payload};

fn row(name: &str) -> ToolDefinition {
    ToolDefinition {
        name: name.into(),
        description: format!("{name} tool."),
        input_schema: json!({"type": "object"}).as_object().unwrap().clone(),
    }
}

fn function(name: &str) -> Value {
    json!({
        "type": "function",
        "name": name,
        "description": format!("{name} tool."),
        "parameters": {"type": "object"},
        "strict": false,
    })
}

fn text(text: &str) -> ContentBlock {
    ContentBlock::Text(TextBlock { text: text.into() })
}

fn native_options() -> ResponsesOptions {
    ResponsesOptions {
        native_catalog_changes: true,
        ..options()
    }
}

fn changed(model: &str) -> ProviderRequest {
    let notice = ContentBlock::CatalogChange(CatalogChangeBlock {
        text: "<notice>".into(),
        added_tools: vec!["Late".into()],
        removed_tools: vec!["Write".into()],
        tool_definitions: vec![row("Late")],
        skills: vec![],
        removed_skills: vec![],
    });
    let mut request = request(model);
    request.tools = vec![row("Read"), row("Write"), row("Late")];
    request.appended_tools = vec!["Late".into()];
    request.messages = vec![
        Message::user_text("hi"),
        Message::assistant_text("ok"),
        Message::new(Role::User, vec![text("more"), notice, text("after")]),
    ];
    request
}

#[test]
fn an_addition_goes_at_its_notice_and_the_offered_tools_in_allowed_tools() {
    let body = payload::build_payload(&changed(MODEL), &native_options()).unwrap();
    assert_eq!(body["tools"], json!([function("Read"), function("Write")]));
    let input = body["input"].as_array().unwrap();
    let user = |texts: &[&str]| {
        let parts: Vec<Value> = (texts.iter())
            .map(|t| json!({"type": "input_text", "text": t}))
            .collect();
        json!({"type": "message", "role": "user", "content": parts})
    };
    let additional = json!({
        "type": "additional_tools",
        "role": "developer",
        "tools": [function("Late")],
    });
    assert_eq!(
        input[2..],
        [user(&["more", "<notice>"]), additional, user(&["after"])]
    );
    let allowed = json!({
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [{"type": "function", "name": "Read"}, {"type": "function", "name": "Late"}],
    });
    assert_eq!(body["tool_choice"], allowed);

    // A choice the host forced stays.
    let mut forced = changed(MODEL);
    forced.tool_choice = Some(ToolChoice::Required {});
    let body = payload::build_payload(&forced, &native_options()).unwrap();
    assert_eq!(body["tool_choice"], "required");
    // Before gpt-5.4 the request goes as it is, the notice text only.
    let body = payload::build_payload(&changed("gpt-5.3"), &native_options()).unwrap();
    let all = json!([function("Read"), function("Write"), function("Late")]);
    assert_eq!(body["tools"], all);
    assert!(body.get("tool_choice").is_none());
}

#[test]
fn a_late_tool_goes_at_its_notice_as_its_row() {
    // Late's schema changed after it was added: its row has the new one.
    let mut request = changed(MODEL);
    let schema = json!({"type": "object", "properties": {"r": {"type": "string"}}});
    request.tools[2].input_schema = schema.as_object().unwrap().clone();
    let body = payload::build_payload(&request, &native_options()).unwrap();
    assert_eq!(body["tools"], json!([function("Read"), function("Write")]));
    let mut late = function("Late");
    late["parameters"] = schema;
    assert_eq!(body["input"][3]["type"], "additional_tools");
    assert_eq!(body["input"][3]["tools"], json!([late]));
}

#[test]
fn native_changes_need_the_option_and_gpt_5_4_or_later() {
    assert!(!catalog::enabled(&options(), "gpt-5.5"), "off by default");
    let options = native_options();
    for model in ["gpt-5.4", "gpt-5.4-mini", "gpt-5.10", "gpt-6"] {
        assert!(catalog::enabled(&options, model), "{model}");
    }
    for model in ["gpt-5", "gpt-5.3", "gpt-4o", "o3", "gpt-5.4x"] {
        assert!(!catalog::enabled(&options, model), "{model}");
    }
}
