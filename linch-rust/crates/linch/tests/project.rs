//! Loading `.linch/skills/*/SKILL.md` and `.linch/agents/*.md`: the
//! fields each frontmatter may hold, and every definition left out, with
//! its reason.

use std::collections::HashSet;
use std::fs;
use std::path::Path;

use linch::project::{SkipReason, Skipped, load_skills, load_subagents};

fn write(path: &Path, text: &str) {
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(path, text).unwrap();
}

fn skill(config: &Path, dir: &str, frontmatter: &str) {
    let text = format!("---\n{frontmatter}\n---\nBody of {dir}.\n");
    write(&config.join("skills").join(dir).join("SKILL.md"), &text);
}

/// Each skipped entry as (file name, reason, detail).
fn reasons(skipped: &[Skipped]) -> Vec<(String, SkipReason, String)> {
    (skipped.iter())
        .map(|s| {
            let name = s.path.file_name().unwrap().to_string_lossy().into_owned();
            (name, s.reason, s.detail.clone())
        })
        .collect()
}

#[test]
fn a_skill_reads_every_field_and_is_named_by_its_directory() {
    let dir = tempfile::tempdir().unwrap();
    let config = dir.path();
    skill(
        config,
        "deploy",
        "description: '  Ship it.  '\nwhen_to_use: Before a release.\n\
         argument_hint: '<env>'\narguments: [env, dry_run]\n\
         allowed_tools: [Bash, Read]\nmodel: fast\nversion: '2'\n\
         disable_model_invocation: true",
    );
    skill(config, "x-dir", "name: ' alpha '\ndescription: First.");
    write(&config.join("skills/notes.txt"), "a file, not a skill");
    let (skills, skipped) = load_skills(config, &HashSet::new());
    assert!(skipped.is_empty(), "{skipped:?}");
    let names: Vec<&str> = skills.iter().map(|s| s.name.as_str()).collect();
    assert_eq!(names, ["alpha", "deploy"]);
    let deploy = &skills[1];
    assert_eq!(deploy.description, "Ship it.");
    assert_eq!(deploy.when_to_use.as_deref(), Some("Before a release."));
    assert_eq!(deploy.argument_hint.as_deref(), Some("<env>"));
    assert_eq!(deploy.arguments, ["env", "dry_run"]);
    assert_eq!(
        deploy.allowed_tools.as_deref(),
        Some(&["Bash".into(), "Read".into()][..])
    );
    assert_eq!(deploy.model.as_deref(), Some("fast"));
    assert_eq!(deploy.version.as_deref(), Some("2"));
    assert!(deploy.disable_model_invocation);
    assert_eq!(deploy.body, "Body of deploy.\n");
    let expected = config.join("skills/deploy");
    assert_eq!(deploy.dir, expected.to_string_lossy());
    assert!(!skills[0].disable_model_invocation && skills[0].arguments.is_empty());
    assert!(load_skills(&config.join("nowhere"), &HashSet::new()) == (Vec::new(), Vec::new()));
}

#[test]
fn a_skill_that_is_not_usable_is_skipped_with_its_reason() {
    use SkipReason::*;
    let dir = tempfile::tempdir().unwrap();
    let config = dir.path();
    let skills = config.join("skills");
    fs::create_dir_all(skills.join("a-empty")).unwrap();
    write(&skills.join("b-plain/SKILL.md"), "No frontmatter.");
    skill(config, "c-yaml", "description: [unclosed");
    skill(config, "d-list", "- description");
    skill(config, "e-none", "");
    skill(config, "f-desc", "description: '  '");
    skill(config, "g-name", "name: Bad Name\ndescription: D.");
    skill(config, "h-args", "description: D.\narguments: [ARGUMENTS]");
    skill(config, "i-args", "description: D.\narguments: [a, a]");
    skill(config, "j-args", "description: D.\narguments: [1st]");
    skill(
        config,
        "k-dmi",
        "description: D.\ndisable_model_invocation: 'yes'",
    );
    skill(config, "l-when", "description: D.\nwhen_to_use: ''");
    skill(
        config,
        "m-tools",
        "description: D.\nallowed_tools: ['Bash git']",
    );
    skill(config, "n-tools", "description: D.\nallowed_tools: Bash");
    skill(config, "o-tool", "name: Read\ndescription: D.");
    skill(config, "p-first", "name: twin\ndescription: D.");
    skill(config, "q-second", "name: twin\ndescription: D.");
    skill(config, "real", "description: D.");
    std::os::unix::fs::symlink(skills.join("real"), skills.join("r-link")).unwrap();
    let tools = HashSet::from(["Read".to_owned()]);
    let (loaded, skipped) = load_skills(config, &tools);
    let names: Vec<&str> = loaded.iter().map(|s| s.name.as_str()).collect();
    assert_eq!(names, ["real", "twin"]);
    let frontmatter = |detail: &str| (InvalidFrontmatter, detail.to_owned());
    let expected = [
        ("a-empty", (MissingSkillMd, "SKILL.md not found".to_owned())),
        (
            "b-plain",
            frontmatter("missing frontmatter (file must start with `---`)"),
        ),
        ("c-yaml", (InvalidFrontmatter, String::new())),
        ("d-list", frontmatter("frontmatter must be a YAML mapping")),
        ("e-none", frontmatter("frontmatter is empty")),
        (
            "f-desc",
            frontmatter("frontmatter 'description' is required and must be a non-empty string"),
        ),
        (
            "g-name",
            frontmatter("skill name 'Bad Name' must match /^[a-z0-9][a-z0-9_-]*$/i"),
        ),
        (
            "h-args",
            frontmatter("frontmatter 'arguments' entry must not be 'ARGUMENTS' (reserved)"),
        ),
        (
            "i-args",
            frontmatter("frontmatter 'arguments' contains duplicate 'a'"),
        ),
        (
            "j-args",
            frontmatter("frontmatter 'arguments' entry '1st' must match /^[a-z_][a-z0-9_]*$/i"),
        ),
        (
            "k-dmi",
            frontmatter("frontmatter 'disable_model_invocation' must be a boolean"),
        ),
        (
            "l-when",
            frontmatter("frontmatter 'when_to_use' must be a non-empty string"),
        ),
        (
            "m-tools",
            frontmatter("frontmatter 'allowed_tools' entry 'Bash git' must look like tool names"),
        ),
        (
            "n-tools",
            frontmatter("frontmatter 'allowed_tools' must be an array of strings"),
        ),
        (
            "o-tool",
            (
                NameCollisionTool,
                "skill name 'Read' collides with a tool".into(),
            ),
        ),
        (
            "q-second",
            (
                NameCollisionSkill,
                "skill name 'twin' already loaded".into(),
            ),
        ),
        ("r-link", (IoError, "symlinks are not followed".into())),
    ];
    let got = reasons(&skipped);
    assert_eq!(got.len(), expected.len(), "{got:#?}");
    for ((name, reason, detail), (want_name, (want_reason, want_detail))) in
        got.iter().zip(expected)
    {
        assert_eq!(
            (name.as_str(), *reason),
            (want_name, want_reason),
            "{detail}"
        );
        match want_name {
            "c-yaml" => assert!(detail.starts_with("YAML parse error: "), "{detail}"),
            _ => assert_eq!(*detail, want_detail),
        }
    }
}

#[test]
fn a_subagent_reads_its_tools_in_either_form() {
    let dir = tempfile::tempdir().unwrap();
    let agents = dir.path().join("agents");
    write(
        &agents.join("scout.md"),
        "---\ndescription: Reads.\ntools: [Read, 'Bash(git *)', 7]\n---\nScout.\n",
    );
    write(
        &agents.join("Fixer.MD"),
        "---\r\ndescription: Fixes.\r\ntools: 'Read, Edit(src/**), ,'\r\n---\r\nFix.\r\n",
    );
    write(
        &agents.join("open.md"),
        "---\nname: wide\ndescription: Anything.\ntools: 5\n---\n",
    );
    write(
        &agents.join("none.md"),
        "---\ndescription: Thinks.\ntools: ''\n---\n",
    );
    write(
        &agents.join("notes.txt"),
        "---\ndescription: Not an agent.\n---\n",
    );
    fs::create_dir_all(agents.join("dir.md")).unwrap();
    let (definitions, skipped) = load_subagents(dir.path());
    assert!(skipped.is_empty(), "{skipped:?}");
    let named: Vec<(&str, Option<Vec<String>>)> = (definitions.iter())
        .map(|d| (d.name.as_str(), d.tools.clone()))
        .collect();
    let list = |names: &[&str]| Some(names.iter().map(|n| n.to_string()).collect());
    assert_eq!(
        named,
        [
            ("Fixer", list(&["Read", "Edit"])),
            ("none", list(&[])),
            ("scout", list(&["Read", "Bash"])),
            ("wide", None),
        ]
    );
    assert_eq!(definitions[0].description, "Fixes.");
    assert_eq!(definitions[0].body, "Fix.\r\n");
    assert_eq!(definitions[2].body, "Scout.\n");
    assert!(load_subagents(&dir.path().join("nowhere")) == (Vec::new(), Vec::new()));
}

#[test]
fn a_subagent_that_is_not_usable_is_skipped_with_its_reason() {
    use SkipReason::*;
    let dir = tempfile::tempdir().unwrap();
    let agents = dir.path().join("agents");
    write(&agents.join("a-plain.md"), "Just text.");
    write(&agents.join("b-desc.md"), "---\nname: b\n---\n");
    write(
        &agents.join("c-name.md"),
        "---\nname: ''\ndescription: D.\n---\n",
    );
    write(
        &agents.join("d-name.md"),
        "---\nname: -dash\ndescription: D.\n---\n",
    );
    write(&agents.join("e-yaml.md"), "---\ndescription: 'open\n---\n");
    write(
        &agents.join("f-twin.md"),
        "---\nname: Twin\ndescription: D.\n---\n",
    );
    write(
        &agents.join("g-twin.md"),
        "---\nname: twin\ndescription: D.\n---\n",
    );
    std::os::unix::fs::symlink(agents.join("f-twin.md"), agents.join("h-link.md")).unwrap();
    let (definitions, skipped) = load_subagents(dir.path());
    let names: Vec<&str> = definitions.iter().map(|d| d.name.as_str()).collect();
    assert_eq!(names, ["Twin"]);
    let got: Vec<(String, SkipReason)> = (reasons(&skipped).into_iter())
        .map(|(name, reason, _)| (name, reason))
        .collect();
    let want = [
        ("a-plain.md", MissingFrontmatter),
        ("b-desc.md", InvalidFrontmatter),
        ("c-name.md", InvalidFrontmatter),
        ("d-name.md", InvalidFrontmatter),
        ("e-yaml.md", InvalidFrontmatter),
        ("g-twin.md", NameCollision),
        ("h-link.md", IoError),
    ];
    let want: Vec<(String, SkipReason)> = want.iter().map(|(n, r)| (n.to_string(), *r)).collect();
    assert_eq!(got, want, "{skipped:#?}");
    assert_eq!(
        skipped[3].detail,
        "agent name '-dash' must match /^[a-z0-9][a-z0-9_-]*$/i"
    );
    assert_eq!(skipped[5].detail, "agent name 'twin' already loaded");
}
