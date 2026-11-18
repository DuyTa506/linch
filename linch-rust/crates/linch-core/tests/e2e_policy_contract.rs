//! The permission policy in the run contract: a policy that differs from
//! the default changes the contract, so a run never resumes under a policy
//! that allows more; the default policy leaves it as it was.

mod common;

use std::collections::HashMap;
use std::sync::Arc;

use common::*;
use linch_core::hooks::ApprovalResponse;
use linch_core::permissions::{Rule, RuleDecision, ToolRule};
use linch_core::provider::scripted::ScriptedProvider;
use linch_core::{
    Agent, Event, PermissionEngine, PermissionLayer, PermissionMode, PermissionPolicy, RunError,
    RunOptions, RunStatus, RunStore, ToolScope,
};

fn agent_under(policy: PermissionPolicy) -> Agent {
    let runs = Runs::default();
    let tools = registry(vec![Echo::new("save", ToolScope::Write, &runs)]);
    let provider = Arc::new(ScriptedProvider::new(vec![text_turn("done")]));
    let engine = PermissionEngine::new(policy).unwrap();
    agent(provider, tools, engine, Arc::new(Stores::default()))
}

/// Taken on the base before the policy joined the contract: an agent under
/// the default policy keeps the digest its runs were stored with, whatever
/// its project root.
#[test]
fn the_default_policy_keeps_the_contract_it_had() {
    let golden = "89eab5bf7b2c1aa44a4afb50f3e2b32b3d863e685a8b08e9de6b4dc11b4a61a0";
    let root = agent_under(PermissionPolicy::new(PermissionMode::Default, "/"));
    assert_eq!(root.contract().to_string(), golden);
    let elsewhere = agent_under(PermissionPolicy::new(PermissionMode::Default, "/srv/app"));
    assert_eq!(elsewhere.contract().to_string(), golden);
}

/// A policy that differs from the default in anything that changes what is
/// allowed gives another contract; the root of a non-default policy counts.
#[test]
fn a_policy_that_allows_more_is_another_contract() {
    let base = agent_under(PermissionPolicy::new(PermissionMode::Default, "/")).contract();
    let mut wider = Vec::new();
    for mode in [PermissionMode::AcceptEdits, PermissionMode::SkipDangerous] {
        wider.push(PermissionPolicy::new(mode, "/"));
    }
    let mut auto = PermissionPolicy::new(PermissionMode::Default, "/");
    auto.auto_allow.insert("save".into());
    wider.push(auto);
    let mut ruled = PermissionPolicy::new(PermissionMode::Default, "/");
    let allow = ToolRule::new("save", RuleDecision::Allow);
    ruled
        .layers
        .push(PermissionLayer::new("app", [Rule::from(allow)]));
    wider.push(ruled);
    let mut fields = PermissionPolicy::new(PermissionMode::Default, "/");
    fields.command_fields.insert("save".into(), "value".into());
    wider.push(fields);
    let mut seen = vec![base];
    for policy in wider {
        let contract = agent_under(policy.clone()).contract();
        assert!(
            !seen.contains(&contract),
            "{policy:?} kept a known contract"
        );
        seen.push(contract);
    }
    let here = agent_under(PermissionPolicy::new(PermissionMode::AcceptEdits, "/srv/a"));
    let there = agent_under(PermissionPolicy::new(PermissionMode::AcceptEdits, "/srv/b"));
    assert_ne!(
        here.contract(),
        there.contract(),
        "a non-default root counts"
    );
}

/// A run started under the default policy is not resumed or approved by an
/// agent whose policy skips the approval.
#[tokio::test]
async fn a_run_never_continues_under_a_wider_policy() {
    let stores = Arc::new(Stores::default());
    let runs = Runs::default();
    let build = |policy: PermissionPolicy| {
        let tools = registry(vec![Echo::new("save", ToolScope::Write, &runs)]);
        let provider = Arc::new(ScriptedProvider::new(script(1, 1, "save")));
        let engine = PermissionEngine::new(policy).unwrap();
        agent(provider, tools, engine, stores.clone())
    };
    let strict = build(PermissionPolicy::new(PermissionMode::Default, "/"));
    let stream = strict.run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    assert!(matches!(
        envelopes.last().unwrap().event,
        Event::Suspended(_)
    ));
    let mut auto = PermissionPolicy::new(PermissionMode::Default, "/");
    auto.auto_allow.insert("save".into());
    for policy in [
        PermissionPolicy::new(PermissionMode::SkipDangerous, "/"),
        auto,
    ] {
        let loose = build(policy);
        let (_, error) = drain(loose.resume(run_id.clone(), RunOptions::new(""))).await;
        assert!(
            matches!(error, Some(RunError::ContractMismatch { .. })),
            "{error:?}"
        );
        let allow = HashMap::from([("call_0_0".to_string(), ApprovalResponse::Allow)]);
        let approve = loose.approve(run_id.clone(), allow, RunOptions::new(""));
        let (_, error) = drain(approve).await;
        assert!(
            matches!(error, Some(RunError::ContractMismatch { .. })),
            "{error:?}"
        );
    }
    assert!(runs.lock().unwrap().is_empty(), "the tool never ran");
    let status = stores.runs.load(NS, &run_id).await.unwrap().status;
    assert_eq!(status, RunStatus::AwaitingApproval);
}
