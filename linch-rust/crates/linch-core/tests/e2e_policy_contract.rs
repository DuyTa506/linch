//! The permission policy in the run contract: a policy that differs from
//! the default changes the contract, so a run never resumes under a policy
//! that allows more; the default policy leaves it as it was.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::provider::scripted::ScriptedProvider;
use linch_core::{Agent, PermissionEngine, PermissionMode, PermissionPolicy, ToolScope};

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
