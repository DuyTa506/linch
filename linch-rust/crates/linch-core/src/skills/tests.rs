use std::sync::Arc;

use serde_json::json;
use tokio_util::sync::CancellationToken;

use super::*;
use crate::message::{ToolResultBlock, ToolUseBlock};
use crate::tool::{Tool, ToolContext};

fn skill(name: &str, description: &str) -> Skill {
    Skill::new(name, format!("/skills/{name}"), description, "body")
}

#[test]
fn the_listing_sorts_visible_skills_and_adds_hints() {
    let mut review = skill("Review", "Review a change.");
    review.when_to_use = Some("before a merge".into());
    review.argument_hint = Some("path".into());
    let mut hidden = skill("hidden", "Not for the model.");
    hidden.disable_model_invocation = true;
    let text = listing(&[skill("zeta", "Last."), hidden, review], None);
    assert_eq!(
        text,
        "- Review: Review a change. \u{2014} before a merge (args: path)\n- zeta: Last."
    );
    assert_eq!(listing(&[], None), "");
}

#[test]
fn the_listing_cuts_descriptions_to_fit_its_budget() {
    let long = "x".repeat(300);
    let one = listing(&[skill("a", &long)], None);
    assert_eq!(one.chars().count(), "- a: ".len() + 250);
    assert!(one.ends_with('\u{2026}'));
    let many: Vec<Skill> = (0..100)
        .map(|i| skill(&format!("s{i:03}"), &long))
        .collect();
    let text = listing(&many, None);
    assert!(text.chars().count() <= 8000, "{}", text.chars().count());
    assert!(text.lines().all(|line| line.ends_with('\u{2026}')));
    let crowd: Vec<Skill> = (0..1000)
        .map(|i| skill(&format!("s{i:04}"), &long))
        .collect();
    assert!(
        listing(&crowd, None)
            .lines()
            .all(|line| !line.contains(':'))
    );
}

#[test]
fn the_budget_is_one_percent_of_the_context_window_at_four_chars_a_token() {
    let long = "x".repeat(300);
    let many: Vec<Skill> = (0..30).map(|i| skill(&format!("s{i:02}"), &long)).collect();
    // 30 lines of 250-character descriptions: 7680 characters in all.
    let full = listing(&many, None);
    assert_eq!(full, listing(&many, Some(200_000)), "8000 characters");
    // 128000 tokens give 5120 characters: each description gets a share.
    let window = listing(&many, Some(128_000));
    assert!(window.chars().count() <= 5120, "{}", window.chars().count());
    assert!(window.chars().count() > 5000);
    assert!(window.len() < full.len());
    // 4096 tokens give 163 characters: names only.
    let small = listing(&many, Some(4096));
    assert!(small.lines().all(|line| !line.contains(':')), "{small}");
}

#[test]
fn substitution_fills_every_placeholder_once() {
    let mut skill = skill("fmt", "Format.");
    skill.arguments = vec!["file".into(), "mode".into(), "extra".into()];
    skill.body = "run ${SKILL_DIR}/x.py \"$file\" $mode [$extra] in ${LINCH_SKILL_DIR} \
                  for ${LINCH_SESSION_ID}: $ARGUMENTS $files $5"
        .into();
    let text = substitute(&skill, "'a b.rs' fast", "s1");
    assert_eq!(
        text,
        "Skill base directory: /skills/fmt\n\nrun /skills/fmt/x.py \"a b.rs\" fast [] \
         in /skills/fmt for s1: 'a b.rs' fast $files $5"
    );
    // A value that looks like a placeholder is not expanded again.
    assert!(substitute(&skill, "$ARGUMENTS", "s").ends_with("$ARGUMENTS $files $5"));
}

#[test]
fn args_split_like_a_shell() {
    assert_eq!(
        split_args(r#"a 'b c' "d \"e\"" f\ g "#),
        ["a", "b c", "d \"e\"", "f g"]
    );
    assert_eq!(split_args("'open"), ["open"]);
    assert_eq!(split_args("''"), [""]);
    assert!(split_args("   ").is_empty());
}

#[test]
fn a_host_skill_overrides_the_builtin_of_the_same_name() {
    let names = |skills: Vec<Skill>| -> Vec<(String, String)> {
        skills.into_iter().map(|s| (s.name, s.dir)).collect()
    };
    let merged = names(with_builtins(vec![skill("zeta", "z"), skill("Alpha", "a")]));
    assert_eq!(merged.len(), 3);
    assert_eq!(merged[0].0, "Alpha");
    assert_eq!(merged[1], ("verify".into(), "<built-in>/verify".into()));
    let own = names(with_builtins(vec![skill("Verify", "mine")]));
    assert_eq!(own, [("Verify".into(), "/skills/Verify".into())]);
    assert!(verify().body.contains("VERDICT: PARTIAL"));
}

fn context() -> ToolContext {
    let (session, run) = ("s1".into(), "r1".into());
    ToolContext::new(session, run, "t", None, CancellationToken::new(), None)
}

fn input(value: serde_json::Value) -> crate::json::JsonObject {
    value.as_object().unwrap().clone()
}

#[tokio::test]
async fn the_tool_returns_the_substituted_body() {
    let mut fmt = skill("fmt", "Format.");
    fmt.body = "fix $ARGUMENTS in ${LINCH_SESSION_ID}".into();
    let mut off = skill("off", "Hidden.");
    off.disable_model_invocation = true;
    let set = SkillSet::new(vec![fmt, off], 128_000).unwrap();
    let tool = SkillTool::new(crate::agent::Versions::new(Arc::new(set)));
    let out = tool
        .execute(input(json!({"skill": "/fmt", "args": "a.rs"})), context())
        .await
        .unwrap();
    assert_eq!(
        out.value,
        "Skill base directory: /skills/fmt\n\nfix a.rs in s1"
    );
    let unknown = tool.execute(input(json!({"skill": "nope"})), context());
    assert_eq!(unknown.await.unwrap_err().message, "Unknown skill: nope");
    let refused = tool.execute(input(json!({"skill": "off"})), context());
    let message = refused.await.unwrap_err().message;
    assert_eq!(message, "Skill is not invokable by the model: off");
    assert!(tool.validate(input(json!({"skill": " / "}))).is_ok());
    assert!(tool.validate(input(json!({"skill": "  "}))).is_err());
    assert_eq!(
        tool.summarize(&input(json!({"skill": "fmt"}))),
        "Invoke skill: fmt"
    );
    assert_eq!(tool.scope(), crate::ToolScope::Read);
}

fn call(id: &str, skill: &str) -> ContentBlock {
    ContentBlock::ToolUse(ToolUseBlock {
        id: id.into(),
        name: SKILL_TOOL.into(),
        input: input(json!({"skill": skill})),
    })
}

fn results(blocks: Vec<ToolResultBlock>) -> Arc<Message> {
    let blocks = blocks.into_iter().map(ContentBlock::ToolResult).collect();
    Arc::new(Message::new(Role::User, blocks))
}

#[test]
fn reminders_come_from_successful_loads_once_each() {
    let history = vec![
        Arc::new(Message::user_text("hi")),
        Arc::new(Message::new(
            Role::Assistant,
            vec![call("a", "/fmt"), call("b", "gone"), call("c", "fmt")],
        )),
        results(vec![
            ToolResultBlock::text("a", "BODY", false),
            ToolResultBlock::text("b", "Unknown skill: gone", true),
            ToolResultBlock::text("c", "BODY", false),
        ]),
        Arc::new(Message::new(Role::Assistant, vec![call("d", "lint")])),
    ];
    let expected = "<system-reminder>\nBelow is the body of a previously invoked skill \
                    named 'fmt'.\n\nBODY\n</system-reminder>";
    assert_eq!(reminders(&history), [expected]);
}
