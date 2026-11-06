use super::*;

#[test]
fn the_roster_lists_the_builtin_unless_overridden_and_has_a_default() {
    let roster = Roster::new(vec![SubagentDefinition::new("zeta", "Z.", "z")]);
    let names: Vec<&str> = roster.listed().iter().map(|d| d.name.as_str()).collect();
    assert_eq!(names, ["verification", "zeta"]);
    for requested in [
        None,
        Some(""),
        Some("  "),
        Some("general-purpose"),
        Some("_default"),
    ] {
        let general = roster.get(requested).unwrap();
        assert_eq!(general.name, "_default", "{requested:?}");
        assert_eq!(general.tools, None);
    }
    assert_eq!(roster.get(Some(" ZETA ")).unwrap().name, "zeta");
    assert!(roster.get(Some("nope")).is_none());
    assert!(
        roster.named("_default").is_none(),
        "the default is not listed"
    );

    let own = SubagentDefinition::new("Verification", "Mine.", "mine");
    let roster = Roster::new(vec![own.clone()]);
    assert_eq!(roster.listed(), [own]);
    let builtin = verification();
    assert_eq!(builtin.tools.unwrap(), ["Read", "Glob", "Grep", "Bash"]);
    assert!(builtin.body.ends_with("VERDICT: PARTIAL"));
}
