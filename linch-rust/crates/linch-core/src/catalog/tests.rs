use serde_json::json;

use super::*;
use crate::canonical::digest;
use crate::message::Role;

fn tool(name: &str, description: &str, field: &str) -> ToolDefinition {
    let schema = json!({"type": "object", "properties": {field: {"type": "string"}}});
    ToolDefinition {
        name: name.into(),
        description: description.into(),
        input_schema: schema.as_object().unwrap().clone(),
    }
}

fn stamp(name: &str, version: u32) -> SkillStamp {
    SkillStamp {
        name: name.into(),
        digest: digest(&json!([name, version])).unwrap(),
    }
}

fn skill(name: &str, version: u32) -> LiveSkill {
    LiveSkill {
        stamp: stamp(name, version),
        description: format!("{name} v{version}"),
    }
}

fn catalog(tools: Vec<ToolDefinition>, skills: Vec<SkillStamp>) -> Catalog {
    let prefix = PrefixEntry {
        system: Vec::new(),
        tools,
        skills,
    };
    Catalog::new(prefix)
}

fn told(catalog: &mut Catalog, change: CatalogChangeBlock) {
    let message = Message::new(Role::User, vec![ContentBlock::CatalogChange(change)]);
    catalog.apply(&message);
}

/// Tell `catalog` what changed in `now`, and return the notice.
fn tell(catalog: &mut Catalog, now: &Live) -> CatalogChangeBlock {
    let change = catalog.change(now).expect("a change");
    told(catalog, change.clone());
    change
}

fn live(tools: Vec<ToolDefinition>, skills: Vec<LiveSkill>) -> Live {
    Live {
        tools,
        skills,
        deferred_skills: None,
    }
}

fn names(rows: &[ToolDefinition]) -> Vec<&str> {
    rows.iter().map(|row| row.name.as_str()).collect()
}

#[test]
fn nothing_changed_means_no_notice() {
    let a = tool("A", "a", "x");
    let catalog = catalog(vec![a.clone()], vec![stamp("s", 1)]);
    assert!(
        catalog
            .change(&live(vec![a], vec![skill("s", 1)]))
            .is_none()
    );
}

#[test]
fn a_notice_lists_every_kind_of_change_in_python_order() {
    let (a, b) = (tool("A", "a", "x"), tool("B", "b", "x"));
    let base = catalog(vec![a.clone(), b], vec![stamp("old", 1), stamp("same", 1)]);
    let a2 = tool("A", "a, louder", "x");
    let c = tool("C", "c", "x");
    let now = live(
        vec![c.clone(), a2.clone()],
        vec![skill("new", 1), skill("same", 2)],
    );
    let change = base.change(&now).unwrap();
    let text = "<system-reminder>\nThe available catalog changed during this session.\n\
        Added tools: C\nRemoved tools: B\nUpdated tools: A: a, louder\n\
        Added skills: new: new v1\nRemoved skills: old\nUpdated skills: same: same v2\n\
        </system-reminder>";
    assert_eq!(change.text, text);
    assert_eq!(change.added_tools, ["C"]);
    assert_eq!(change.removed_tools, ["B"]);
    assert_eq!(change.tool_definitions, [c, a2]);
    assert_eq!(change.skills, [stamp("new", 1), stamp("same", 2)]);
    assert_eq!(change.removed_skills, ["old"]);
}

#[test]
fn the_rows_keep_removed_tools_and_change_a_row_only_for_a_new_schema() {
    let (a, b) = (tool("A", "a", "x"), tool("B", "b", "x"));
    let mut catalog = catalog(vec![a.clone(), b.clone()], Vec::new());
    let c = tool("C", "c", "x");
    let first = catalog.change(&live(vec![a.clone(), c.clone()], Vec::new()));
    told(&mut catalog, first.unwrap());
    assert_eq!(names(&catalog.tools(None)), ["A", "B", "C"]);
    assert!(
        catalog
            .change(&live(vec![a.clone(), c.clone()], Vec::new()))
            .is_none()
    );

    // A description alone keeps the row; a schema replaces it in place.
    let described = tool("A", "a, louder", "x");
    let reshaped = tool("C", "c", "y");
    let now = live(vec![described.clone(), reshaped.clone()], Vec::new());
    tell(&mut catalog, &now);
    assert_eq!(catalog.tools(None), [a, b.clone(), reshaped.clone()]);
    assert!(catalog.change(&now).is_none(), "told");

    // A tool that comes back with its old schema needs no new row.
    let back = live(vec![described, b.clone(), reshaped], Vec::new());
    let change = catalog.change(&back).unwrap();
    assert_eq!(change.added_tools, ["B"]);
    told(&mut catalog, change);
    assert_eq!(names(&catalog.tools(None)), ["A", "B", "C"]);
    let keep = |name: &str| name != "B";
    assert_eq!(names(&catalog.tools(Some(&keep))), ["A", "C"]);
}

#[test]
fn the_restatement_is_every_change_since_the_prefix() {
    let (a, b) = (tool("A", "a", "x"), tool("B", "b", "x"));
    let mut catalog = catalog(vec![a.clone(), b.clone()], vec![stamp("s", 1)]);
    assert!(catalog.restatement(&[]).is_none());
    let c = tool("C", "c", "x");
    let now = live(vec![a.clone(), c.clone()], vec![skill("s", 2)]);
    tell(&mut catalog, &now);
    let back = live(vec![a, b, c.clone()], vec![skill("s", 2)]);
    tell(&mut catalog, &back);
    let restated = catalog.restatement(&[skill("s", 2)]).unwrap();
    let text = "<system-reminder>\nThe available catalog changed during this session.\n\
        Added tools: C\nUpdated skills: s: s v2\n</system-reminder>";
    assert_eq!(restated.text, text);
    assert_eq!(restated.tool_definitions, [c]);
    assert_eq!(restated.skills, [stamp("s", 2)]);
}

#[test]
fn the_rows_after_the_prefix_are_marked_appended() {
    let (a, b) = (tool("A", "a", "x"), tool("B", "b", "x"));
    let mut catalog = catalog(vec![a.clone(), b], Vec::new());
    assert!(catalog.appended(None).is_empty());
    let (c, d) = (tool("C", "c", "x"), tool("D", "d", "x"));
    tell(&mut catalog, &live(vec![a, c, d], Vec::new()));
    assert_eq!(catalog.appended(None), ["C", "D"]);

    // A new schema replaces a row where it is, and a removal keeps it:
    // the marks stay with the rows.
    let now = live(vec![tool("A", "a", "y"), tool("C", "c", "y")], Vec::new());
    tell(&mut catalog, &now);
    assert_eq!(names(&catalog.tools(None)), ["A", "B", "C", "D"]);
    assert_eq!(catalog.appended(None), ["C", "D"]);
    let keep = |name: &str| name != "C";
    assert_eq!(catalog.appended(Some(&keep)), ["D"]);
}
