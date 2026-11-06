//! The cost of a run (17.2): each paid call's receipt is priced through the
//! agent's pricing function, `usage` events carry the call's and the run's
//! cost, and the result the run's total, which survives a crash and resume.

mod common;

use std::sync::Arc;

use common::*;
use linch_core::events::model_call::ModelCallReceiptEvent;
use linch_core::store::faulty::{FaultTiming, Faulty};
use linch_core::{
    Agent, AgentBuilder, Envelope, Event, PermissionMode, RunId, RunOptions, RunStore,
    ScriptedProvider, SessionStore, ToolScope, Usage,
};

/// USD per token for `MODEL`: a thousandth per input token, two per output
/// token. Other models have no price.
fn price(model: &str, usage: &Usage) -> Option<f64> {
    let cost = usage.input_tokens as f64 * 0.001 + usage.output_tokens as f64 * 0.002;
    (model == MODEL).then_some(cost)
}

fn priced<S: RunStore + SessionStore + 'static>(stores: Arc<S>, pricing: bool) -> Agent {
    let runs = Runs::default();
    let builder = AgentBuilder::new(spec())
        .provider(Arc::new(ScriptedProvider::new(script(2, 1, "Echo"))))
        .tools(registry(vec![Echo::new("Echo", ToolScope::Read, &runs)]))
        .permissions(policy(PermissionMode::SkipDangerous))
        .run_store(stores.clone())
        .session_store(stores)
        .namespace(NS);
    let builder = if pricing {
        builder.pricing(price)
    } else {
        builder
    };
    builder.build().unwrap()
}

fn receipts(events: &[Envelope]) -> Vec<&ModelCallReceiptEvent> {
    (events.iter())
        .filter_map(|e| match &e.event {
            Event::ModelCallReceipt(receipt) => Some(receipt),
            _ => None,
        })
        .collect()
}

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

#[tokio::test]
async fn paid_calls_are_priced_and_the_run_reports_its_cost() {
    let stores = Arc::new(Stores::default());
    let stream = priced(stores.clone(), true).run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    // Three calls of 10 in and 5 out: 0.02 USD each.
    let one = 10.0 * 0.001 + 5.0 * 0.002;
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    let costs: Vec<Option<f64>> = receipts(&events).iter().map(|r| r.cost_usd).collect();
    assert_eq!(costs, [Some(one); 3]);
    let usage: Vec<(Option<f64>, Option<f64>)> = (envelopes.iter())
        .filter_map(|e| match &e.event {
            Event::Usage(usage) => Some((usage.cost_usd, usage.cumulative_cost_usd)),
            _ => None,
        })
        .collect();
    assert_eq!(usage.len(), 3);
    for (turn, (cost, cumulative)) in usage.into_iter().enumerate() {
        assert!(close(cost.unwrap(), one), "turn {turn}");
        assert!(
            close(cumulative.unwrap(), one * (turn + 1) as f64),
            "turn {turn}"
        );
    }
    let Some(Event::Result(result)) = envelopes.last().map(|e| &e.event) else {
        panic!("no result");
    };
    assert!(close(result.total_cost_usd.unwrap(), one * 3.0));
}

#[tokio::test]
async fn without_pricing_no_cost_is_reported() {
    let stores = Arc::new(Stores::default());
    let stream = priced(stores.clone(), false).run("go", RunOptions::new("s1"));
    let run_id = stream.run_id().clone();
    let envelopes = finish(stream).await;
    let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
    assert!(receipts(&events).iter().all(|r| r.cost_usd.is_none()));
    for envelope in &envelopes {
        match &envelope.event {
            Event::Usage(usage) => {
                assert_eq!((usage.cost_usd, usage.cumulative_cost_usd), (None, None));
            }
            Event::Result(result) => assert_eq!(result.total_cost_usd, None),
            _ => {}
        }
    }
}

/// Continue whatever a crash left: resume, or run again if never created.
async fn recover(agent: &Agent, stores: &Stores, run_id: RunId) -> RunId {
    match stores.runs.load(NS, &run_id).await {
        Ok(record) if record.status.is_terminal() => run_id,
        Ok(_) => {
            finish(agent.resume(run_id.clone(), RunOptions::new("s1"))).await;
            run_id
        }
        Err(_) => {
            let stream = agent.run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            finish(stream).await;
            run_id
        }
    }
}

/// A crash at any write, then a resume: the total is the sum of the
/// durable receipts' costs, so the checkpoint carried what came before.
#[tokio::test]
async fn the_total_cost_survives_a_crash_at_any_write() {
    let clean = Arc::new(Stores::default());
    finish(priced(clean.clone(), true).run("go", RunOptions::new("s1"))).await;
    let writes = clean.log.lock().unwrap().len() as u64;
    for n in 1..=writes {
        for timing in [FaultTiming::Before, FaultTiming::After] {
            let context = format!("write {n} failing {timing:?}");
            let stores = Arc::new(Stores::default());
            let faulty = Arc::new(Faulty::new(stores.clone(), n, timing));
            let stream = priced(faulty, true).run("go", RunOptions::new("s1"));
            let run_id = stream.run_id().clone();
            assert!(drain(stream).await.1.is_some(), "{context}");
            let agent = priced(stores.clone(), true);
            let run_id = recover(&agent, &stores, run_id).await;
            let events = stores.runs.events(NS, &run_id, 0).await.unwrap();
            let paid: f64 = receipts(&events).iter().filter_map(|r| r.cost_usd).sum();
            let Some(Event::Result(result)) = events.last().map(|e| &e.event) else {
                panic!("{context}: the run has no result");
            };
            let total = result.total_cost_usd.unwrap_or_default();
            assert!(close(total, paid), "{context}: {total} != {paid}");
        }
    }
}
