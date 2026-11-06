//! Property tests: the `pre_tool_use` chain against a reference fold (the
//! #102 invariant), and digest independence from registration order.

use proptest::prelude::*;

use super::*;

/// What one hook in a generated `pre_tool_use` chain does.
#[derive(Debug, Clone, Copy)]
enum Spec {
    Continue,
    Rewrite(u8),
    Block,
    Resolve,
    Fail(FailMode),
    Hang(FailMode),
    /// Filtered out by its matcher, so never called.
    Unmatched,
}

fn spec() -> impl Strategy<Value = Spec> {
    let mode = prop_oneof![Just(FailMode::Open), Just(FailMode::Closed)];
    prop_oneof![
        Just(Spec::Continue),
        any::<u8>().prop_map(Spec::Rewrite),
        Just(Spec::Block),
        Just(Spec::Resolve),
        mode.clone().prop_map(Spec::Fail),
        mode.prop_map(Spec::Hang),
        Just(Spec::Unmatched),
    ]
}

fn input(n: u8) -> JsonObject {
    obj(json!({ "n": n }))
}

/// Outcome kind and final input, without the reason or result text.
#[derive(Debug, PartialEq)]
enum Kind {
    Run(JsonObject),
    Blocked(JsonObject),
    Resolved(JsonObject),
}

/// The spec'd semantics, written as a plain fold: which hooks get called
/// and what the chain decides.
fn reference(specs: &[Spec]) -> (Vec<String>, Kind) {
    let mut called = Vec::new();
    let mut current = input(0);
    for (i, spec) in specs.iter().enumerate() {
        if matches!(spec, Spec::Unmatched) {
            continue;
        }
        called.push(format!("h{i}"));
        match spec {
            Spec::Continue | Spec::Fail(FailMode::Open) | Spec::Hang(FailMode::Open) => {}
            Spec::Rewrite(n) => current = input(*n),
            Spec::Block | Spec::Fail(FailMode::Closed) | Spec::Hang(FailMode::Closed) => {
                return (called, Kind::Blocked(current));
            }
            Spec::Resolve => return (called, Kind::Resolved(current)),
            Spec::Unmatched => unreachable!(),
        }
    }
    (called, Kind::Run(current))
}

fn hook(i: usize, spec: Spec, log: &Log) -> Scripted<PreToolUse> {
    let id = format!("h{i}");
    let base =
        |act: fn(&PreToolUseRequest) -> Act<PreToolUseResponse>| Scripted::new(&id, log, act);
    match spec {
        Spec::Continue => base(|_| Act::Answer(PreToolUseResponse::Continue)),
        Spec::Rewrite(n) => Scripted::new(&id, log, move |_| {
            Act::Answer(PreToolUseResponse::Rewrite { input: input(n) })
        }),
        Spec::Block => base(|_| {
            Act::Answer(PreToolUseResponse::Block {
                reason: "no".into(),
            })
        }),
        Spec::Resolve => base(|_| {
            Act::Answer(PreToolUseResponse::Resolve {
                result: Ok(ToolOutput::new("cached")),
            })
        }),
        Spec::Fail(mode) => base(|_| Act::Fail).fail_mode(mode),
        Spec::Hang(mode) => base(|_| Act::Hang)
            .timeout(Duration::from_millis(1))
            .fail_mode(mode),
        Spec::Unmatched => base(|_| Act::Answer(PreToolUseResponse::Continue)).matcher(&["Other"]),
    }
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap()
}

/// Registers a hook with policy `id`/`version` at the point numbered `point`.
fn register_at(hooks: &Hooks, point: u8, id: &str, version: u32, log: &Log) -> Registration {
    match point % 7 {
        0 => register(
            &hooks.user_prompt_submit,
            Scripted::new(id, log, |_| Act::Answer(UserPromptResponse::Continue)).version(version),
        ),
        1 => register(
            &hooks.context_build,
            Scripted::new(id, log, |_| Act::Answer(ContextBuildResponse::default()))
                .version(version),
        ),
        2 => register(
            &hooks.pre_tool_use,
            Scripted::new(id, log, |_| Act::Answer(PreToolUseResponse::Continue)).version(version),
        ),
        3 => register(
            &hooks.permission_request,
            Scripted::new(id, log, |_| Act::Answer(ApprovalResponse::Allow)).version(version),
        ),
        4 => register(
            &hooks.post_tool_use,
            Scripted::new(id, log, |_| Act::Answer(PostToolUseResponse::Continue)).version(version),
        ),
        5 => register(
            &hooks.stop,
            Scripted::new(id, log, |_| Act::Answer(StopResponse::Allow)).version(version),
        ),
        _ => register(
            &hooks.post_turn,
            Scripted::new(id, log, |_| Act::Answer(())).version(version),
        ),
    }
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(128))]

    /// #102: the chain matches the reference fold, and in particular only a
    /// chain that called every matching hook lets the tool run.
    #[test]
    fn pre_tool_use_matches_the_reference(specs in prop::collection::vec(spec(), 0..8)) {
        let hooks = Hooks::default();
        let log = new_log();
        for (i, spec) in specs.iter().enumerate() {
            let _ = register(&hooks.pre_tool_use, hook(i, *spec, &log));
        }
        let done = runtime().block_on(hooks.pre_tool_use.run(pre_request("Write", input(0))));
        let (expected_calls, expected) = reference(&specs);
        let kind = match done.outcome {
            PreToolUseOutcome::Run { input } => {
                let matching = specs.iter().filter(|s| !matches!(s, Spec::Unmatched)).count();
                prop_assert_eq!(calls(&log).len(), matching, "Run skipped a matching hook");
                Kind::Run(input)
            }
            PreToolUseOutcome::Blocked { input, .. } => Kind::Blocked(input),
            PreToolUseOutcome::Resolved { input, .. } => Kind::Resolved(input),
        };
        prop_assert_eq!(kind, expected);
        prop_assert_eq!(calls(&log), expected_calls.clone());
        prop_assert_eq!(done.records.len(), expected_calls.len());
    }

    #[test]
    fn the_digest_ignores_registration_order(
        regs in prop::collection::vec((0u8..7, 0u8..3, 1u32..3), 0..10)
            .prop_flat_map(|regs| (Just(regs.clone()), Just(regs).prop_shuffle())),
    ) {
        let (ordered, shuffled) = regs;
        let log = new_log();
        let a = Hooks::default();
        let b = Hooks::default();
        for (point, id, version) in &ordered {
            let _ = register_at(&a, *point, &format!("p{id}"), *version, &log);
        }
        for (point, id, version) in &shuffled {
            let _ = register_at(&b, *point, &format!("p{id}"), *version, &log);
        }
        prop_assert_eq!(a.policy_digest(), b.policy_digest());
    }
}
