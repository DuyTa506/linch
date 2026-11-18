use serde_json::json;

use super::*;

fn spec() -> AgentSpec {
    AgentSpec::new("m")
}

fn texts(blocks: &[SystemBlock]) -> Vec<&str> {
    blocks.iter().map(|block| block.text.as_str()).collect()
}

#[test]
fn a_spec_without_sections_renders_its_flat_prompt_as_before() {
    let mut spec = spec();
    spec.system_blocks = vec![SystemBlock {
        text: "host".into(),
        cacheable: true,
    }];
    spec.system_prompt = "prompt".into();
    let layout = layout(&spec);
    assert_eq!(texts(&layout.blocks), ["host", "prompt"]);
    assert_eq!(layout.listing_at, 2);
    assert!(!layout.blocks[1].cacheable);
}

#[test]
fn sections_render_by_order_then_declaration_before_the_flat_blocks() {
    let mut spec = spec();
    spec.system_prompt = "prompt".into();
    spec.system.sections = vec![
        SystemSection::new("late", "c").order(5),
        SystemSection::new("first", "a").order(-1),
        SystemSection::new("tie one", "b1"),
        SystemSection::new("tie two", "b2"),
    ];
    spec.system.append = Some("tail".into());
    let layout = layout(&spec);
    assert_eq!(
        texts(&layout.blocks),
        ["a", "b1", "b2", "c", "prompt", "tail"]
    );
    assert_eq!(layout.listing_at, 5, "the listing goes before append");
    assert!(
        layout.blocks[0].cacheable,
        "a section is cacheable by default"
    );
    assert!(!layout.blocks[5].cacheable, "append is not cacheable");
}

#[test]
fn a_section_carries_its_cache_flag() {
    let mut spec = spec();
    spec.system.sections = vec![
        SystemSection::new("policy", "p"),
        SystemSection::new("volatile", "v")
            .order(1)
            .cacheable(false),
    ];
    let blocks = layout(&spec).blocks;
    assert_eq!((blocks[0].cacheable, blocks[1].cacheable), (true, false));
}

#[test]
fn the_wire_form_leaves_defaults_out_and_refuses_unknown_keys() {
    assert!(SystemConfig::default().is_default());
    assert_eq!(
        serde_json::to_value(SystemConfig::default()).unwrap(),
        json!({})
    );
    let section = SystemSection::new("n", "t");
    assert_eq!(
        serde_json::to_value(&section).unwrap(),
        json!({"name": "n", "text": "t"})
    );
    let wire = json!({
        "sections": [{"name": "n", "text": "t", "order": 2, "cacheable": false}],
        "append": "x",
        "replace_defaults": true
    });
    let config: SystemConfig = serde_json::from_value(wire.clone()).unwrap();
    assert_eq!(serde_json::to_value(&config).unwrap(), wire);
    let unknown = json!({"sections": [{"name": "n", "text": "t", "placement": "x"}]});
    assert!(serde_json::from_value::<SystemConfig>(unknown).is_err());
}

#[test]
fn bad_sections_are_named_by_the_problem() {
    let with = |sections: Vec<SystemSection>| SystemConfig {
        sections,
        ..SystemConfig::default()
    };
    let none: &[SystemBlock] = &[];
    assert!(
        with(vec![SystemSection::new(" ", "t")])
            .problem(none)
            .is_some()
    );
    assert!(
        with(vec![SystemSection::new("n", "  ")])
            .problem(none)
            .is_some()
    );
    let twice = with(vec![
        SystemSection::new("n", "a"),
        SystemSection::new("n", "b"),
    ]);
    assert!(twice.problem(none).unwrap().contains("twice"));
    let blank = SystemConfig {
        append: Some(" ".into()),
        ..SystemConfig::default()
    };
    assert!(blank.problem(none).unwrap().contains("append"));
    assert_eq!(SystemConfig::default().problem(none), None);
}

#[test]
fn a_section_that_is_not_cacheable_may_not_precede_a_cacheable_block() {
    let none: &[SystemBlock] = &[];
    let volatile = SystemSection::new("volatile", "v").cacheable(false);
    let stable = SystemSection::new("stable", "s").order(1);
    let wrong = SystemConfig {
        sections: vec![volatile.clone(), stable.clone()],
        ..SystemConfig::default()
    };
    assert!(wrong.problem(none).unwrap().contains("volatile"));
    let right = SystemConfig {
        sections: vec![volatile.clone().order(2), stable],
        ..SystemConfig::default()
    };
    assert_eq!(right.problem(none), None);
    let host = [SystemBlock {
        text: "h".into(),
        cacheable: true,
    }];
    let alone = SystemConfig {
        sections: vec![volatile],
        ..SystemConfig::default()
    };
    assert!(alone.problem(&host).is_some(), "the host's blocks follow");
}
