//! Native catalog changes: the tool blocks a notice turns into, on the
//! wire body.

use linch_core::{
    CatalogChangeBlock, ContentBlock, Message, ProviderError, ProviderRequest, Role, TextBlock,
    ToolDefinition,
};
use serde_json::{Value, json};
use tokio_util::sync::CancellationToken;

use super::{SECRET, request};
use crate::ApiKey;
use crate::anthropic::{AnthropicOptions, catalog, http, payload};
use crate::test_server::serve_once;

fn row(name: &str) -> ToolDefinition {
    let schema = json!({"type": "object", "properties": {"q": {"type": "string"}}});
    ToolDefinition {
        name: name.into(),
        description: format!("{name} tool."),
        input_schema: schema.as_object().unwrap().clone(),
    }
}

fn notice(added: &[&str], removed: &[&str], definitions: Vec<ToolDefinition>) -> ContentBlock {
    let names = |names: &[&str]| names.iter().map(|n| n.to_string()).collect();
    ContentBlock::CatalogChange(CatalogChangeBlock {
        text: "<notice>".into(),
        added_tools: names(added),
        removed_tools: names(removed),
        tool_definitions: definitions,
        skills: vec![],
        removed_skills: vec![],
    })
}

fn user(blocks: Vec<ContentBlock>) -> Message {
    Message::new(Role::User, blocks)
}

fn text(text: &str) -> ContentBlock {
    ContentBlock::Text(TextBlock { text: text.into() })
}

/// A request on `tools`, of which the core marked `appended` as added
/// after the prefix.
fn native(
    tools: Vec<ToolDefinition>,
    appended: &[&str],
    messages: Vec<Message>,
) -> ProviderRequest {
    let mut request = request();
    request.model = "claude-opus-5-5".into();
    request.tools = tools;
    request.appended_tools = appended.iter().map(|name| name.to_string()).collect();
    request.messages = messages;
    request
}

fn names(body: &Value) -> Vec<&str> {
    let tools = body["tools"].as_array().unwrap();
    tools.iter().map(|t| t["name"].as_str().unwrap()).collect()
}

fn wire_tool(name: &str) -> Value {
    let tool = row(name);
    json!({"name": tool.name, "description": tool.description, "input_schema": tool.input_schema})
}

#[test]
fn the_wire_prefix_keeps_its_bytes_across_an_addition() {
    let before = vec![Message::user_text("hi")];
    let mut after = before.clone();
    after.push(Message::assistant_text("ok"));
    after.push(user(vec![notice(&["Late"], &[], vec![row("Late")])]));
    let mut first = native(vec![row("Read")], &[], before);
    let mut later = native(vec![row("Read"), row("Late")], &["Late"], after);
    for request in [&mut first, &mut later] {
        request.system = vec![linch_core::SystemBlock {
            text: "You help.".into(),
            cacheable: true,
        }];
        request.cache_control = Some(linch_core::CacheControl { ttl: None });
    }
    let (first, later) = (
        payload::build(&first, true).unwrap(),
        payload::build(&later, true).unwrap(),
    );
    let bytes = |body: &Value, key: &str| serde_json::to_string(&body[key]).unwrap();
    assert_eq!(bytes(&later, "tools"), bytes(&first, "tools"));
    assert_eq!(bytes(&later, "system"), bytes(&first, "system"));
    // The first message moved its cache breakpoint to the new last user
    // message, as any turn does; its content is otherwise the same.
    let text = |body: &Value| body["messages"][0]["content"][0]["text"].clone();
    assert_eq!(text(&later), text(&first));
}

#[test]
fn a_tool_added_by_value_moves_from_tools_to_a_system_message_at_its_notice() {
    let tools = vec![row("Read"), row("Write"), row("Late")];
    let messages = vec![
        Message::user_text("hi"),
        Message::assistant_text("ok"),
        user(vec![
            text("more"),
            notice(&["Late"], &["Write"], vec![row("Late")]),
        ]),
        Message::assistant_text("ok again"),
        Message::user_text("again"),
    ];
    let request = native(tools, &["Late"], messages);
    let body = payload::build(&request, true).unwrap();
    assert_eq!(names(&body), ["Read", "Write"]);
    let messages = body["messages"].as_array().unwrap();
    let roles: Vec<&str> = messages
        .iter()
        .map(|m| m["role"].as_str().unwrap())
        .collect();
    assert_eq!(
        roles,
        ["user", "assistant", "user", "system", "assistant", "user"]
    );
    // The notice text stays in the user message.
    assert_eq!(messages[2]["content"][1]["text"], "<notice>");
    let definition = json!({"type": "tool_definition", "definition": wire_tool("Late")});
    let removal = json!({"type": "tool_reference", "name": "Write"});
    let blocks = json!([
        {"type": "tool_addition", "tool": definition},
        {"type": "tool_removal", "tool": removal},
    ]);
    assert_eq!(messages[3]["content"], blocks);
    // Off, the same request keeps every row and sends text only.
    let body = payload::build(&request, false).unwrap();
    assert_eq!(names(&body), ["Read", "Write", "Late"]);
    assert_eq!(body["messages"].as_array().unwrap().len(), 5);
}

#[test]
fn a_prefix_tool_added_back_is_a_reference_and_a_late_one_removed_stays() {
    let tools = vec![row("Read"), row("Late")];
    let messages = vec![
        Message::user_text("hi"),
        Message::assistant_text("a"),
        user(vec![notice(&["Late"], &["Read"], vec![row("Late")])]),
        Message::assistant_text("b"),
        user(vec![notice(&["Read"], &["Late"], vec![row("Read")])]),
    ];
    let body = payload::build(&native(tools, &["Late"], messages), true).unwrap();
    assert_eq!(names(&body), ["Read"]);
    let messages = body["messages"].as_array().unwrap();
    // The last notice's blocks close the conversation; removing Late is
    // text only, since the API removes only what `tools` holds.
    let last = messages.last().unwrap();
    assert_eq!(last["role"], "system");
    let reference = json!({"type": "tool_reference", "name": "Read"});
    let blocks = json!([{"type": "tool_addition", "tool": reference}]);
    assert_eq!(last["content"], blocks);
}

#[test]
fn a_late_tool_goes_at_its_notice_with_its_rows_definition() {
    // Late's schema changed after it was added: its row has the new one.
    let mut reshaped = row("Late");
    let schema = json!({"type": "object", "properties": {"r": {"type": "string"}}});
    reshaped.input_schema = schema.as_object().unwrap().clone();
    let messages = vec![
        Message::user_text("hi"),
        Message::assistant_text("a"),
        user(vec![notice(&["Late"], &[], vec![row("Late")])]),
        Message::assistant_text("b"),
        user(vec![notice(&[], &[], vec![reshaped.clone()])]),
    ];
    let request = native(vec![row("Read"), reshaped.clone()], &["Late"], messages);
    let body = payload::build(&request, true).unwrap();
    assert_eq!(names(&body), ["Read"]);
    let wire = json!({
        "name": "Late",
        "description": reshaped.description,
        "input_schema": reshaped.input_schema,
    });
    let definition = json!({"type": "tool_definition", "definition": wire});
    let blocks = json!([{"type": "tool_addition", "tool": definition}]);
    assert_eq!(body["messages"][3]["content"], blocks);
}

#[test]
fn native_definitions_past_four_mebibytes_fail_locally() {
    let mut huge = row("Huge");
    huge.description = "x".repeat(4_194_304);
    let messages = vec![
        Message::user_text("hi"),
        Message::assistant_text("a"),
        user(vec![notice(&["Huge"], &[], vec![huge.clone()])]),
    ];
    let request = native(vec![row("Read"), huge], &["Huge"], messages);
    let Err(ProviderError::InvalidRequest { message }) = payload::build(&request, true) else {
        panic!("the limit holds")
    };
    let want = "Native tool definitions exceed the 4,194,304-byte limit; start a new \
                session or disable native_catalog_changes.";
    assert_eq!(message, want);
}

#[test]
fn native_changes_need_the_option_anthropics_host_and_a_listed_model() {
    let mut options = AnthropicOptions::new(ApiKey::new(SECRET).unwrap());
    assert!(
        !catalog::enabled(&options, "claude-opus-5-5"),
        "off by default"
    );
    options.native_catalog_changes = true;
    assert!(catalog::enabled(&options, "claude-opus-5-5"));
    assert!(!catalog::enabled(&options, "claude-sonnet-4-6"));
    options.base_url = "https://proxy.example.com".into();
    assert!(!catalog::enabled(&options, "claude-opus-5-5"));
}

#[tokio::test]
async fn the_beta_header_goes_with_a_native_request() {
    let (url, server) = serve_once(
        "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n".into(),
    );
    let client = reqwest::Client::new();
    let key = ApiKey::new(SECRET).unwrap();
    let body = json!({});
    let cancel = CancellationToken::new();
    let beta = Some(catalog::BETA);
    let _ = http::post(&client, &url, &key, &body, beta, &cancel).await;
    let raw = server.join().unwrap();
    assert!(
        raw.contains("anthropic-beta: inline-tools-2026-09-15"),
        "{raw}"
    );
}
