use std::time::Duration;

use serde_json::json;

use super::*;
use crate::events::kinds::{AssistantEvent, ModelFallbackEvent, ProviderRetryEvent};
use crate::events::{Envelope, WIRE_VERSION};
use crate::ids::RunId;
use crate::json::JsonObject;
use crate::message::Message;
use crate::run::StopReason;

fn busy() -> ProviderError {
    ProviderError::Status {
        status: 503,
        retry_after: None,
        message: "busy".into(),
    }
}

fn exact(max_attempts: u32) -> ProviderRetry {
    ProviderRetry {
        max_attempts,
        base_delay_ms: 100,
        max_delay_ms: 1000,
        jitter: 0.0,
    }
}

fn ms(value: u64) -> Option<Duration> {
    Some(Duration::from_millis(value))
}

#[test]
fn the_defaults_are_python_retry_options() {
    let read: ProviderRetry = serde_json::from_value(json!({})).unwrap();
    assert_eq!(read, ProviderRetry::default());
    assert_eq!(
        (read.max_attempts, read.base_delay_ms, read.max_delay_ms),
        (5, 1000, 30000)
    );
    assert_eq!(read.jitter, 0.2);
    assert!(read.problem().is_none());
}

#[test]
fn each_retry_waits_twice_as_long_up_to_the_cap() {
    let policy = exact(10);
    let waits: Vec<_> = (1..=6).map(|r| policy.delay(r, &busy(), "s")).collect();
    assert_eq!(
        waits,
        [ms(100), ms(200), ms(400), ms(800), ms(1000), ms(1000)]
    );
    let far = policy.delay(9, &busy(), "s");
    assert_eq!(far, ms(1000), "a large retry number saturates");
}

#[test]
fn a_model_gets_at_most_max_attempts_calls() {
    let policy = exact(3);
    assert!(policy.delay(2, &busy(), "s").is_some());
    assert_eq!(policy.delay(3, &busy(), "s"), None, "the fourth call");
    assert_eq!(exact(1).delay(1, &busy(), "s"), None, "1 never retries");
}

#[test]
fn only_a_retryable_failure_is_retried() {
    let policy = exact(5);
    let refused = ProviderError::Status {
        status: 400,
        retry_after: None,
        message: "bad".into(),
    };
    assert_eq!(policy.delay(1, &refused, "s"), None);
    let invalid = ProviderError::InvalidRequest {
        message: "no".into(),
    };
    assert_eq!(policy.delay(1, &invalid, "s"), None);
    assert_eq!(policy.delay(1, &ProviderError::Cancelled, "s"), None);
    let reset = ProviderError::Transport {
        message: "reset".into(),
    };
    assert_eq!(policy.delay(1, &reset, "s"), ms(100));
}

#[test]
fn a_retry_after_is_waited_as_given_unless_past_the_cap() {
    let mut policy = exact(5);
    policy.jitter = 1.0;
    let after = |wait| ProviderError::Status {
        status: 429,
        retry_after: Some(Duration::from_millis(wait)),
        message: "slow down".into(),
    };
    assert_eq!(
        policy.delay(1, &after(30), "s"),
        ms(30),
        "shorter: as given"
    );
    assert_eq!(policy.delay(1, &after(1000), "s"), ms(1000), "no jitter");
    assert_eq!(policy.delay(1, &after(1001), "s"), None, "past the cap");
    assert_eq!(
        policy.delay(5, &after(30), "s"),
        None,
        "attempts still bind"
    );
}

#[test]
fn jitter_moves_a_wait_by_at_most_its_fraction_and_never_past_the_cap() {
    let policy = ProviderRetry {
        max_attempts: 10,
        base_delay_ms: 1000,
        max_delay_ms: 30000,
        jitter: 0.2,
    };
    let waits: Vec<_> = (0..64)
        .map(|i| policy.delay(1, &busy(), &format!("run-{i}:primary:0:1")))
        .map(Option::unwrap)
        .collect();
    assert!(
        (waits.iter()).all(|w| (800..=1200).contains(&w.as_millis())),
        "{waits:?}"
    );
    assert!(
        waits.iter().any(|w| *w != waits[0]),
        "seeds spread the waits"
    );
    let again = policy.delay(1, &busy(), "run-0:primary:0:1");
    assert_eq!(again, Some(waits[0]), "one seed, one wait");
    let capped = (0..64).map(|i| policy.delay(9, &busy(), &i.to_string()));
    assert!(capped.map(Option::unwrap).all(|w| w <= ms(30000).unwrap()));
}

#[test]
fn a_policy_that_cannot_run_is_refused() {
    assert!(exact(0).problem().is_some());
    let mut wide = exact(2);
    wide.jitter = 1.5;
    assert!(wide.problem().is_some());
    wide.jitter = f64::NAN;
    assert!(wide.problem().is_some());
    let unknown = json!({"max_attempts": 2, "backoff": "linear"});
    assert!(serde_json::from_value::<ProviderRetry>(unknown).is_err());
}

#[test]
fn fallback_models_must_be_distinct_and_not_blank() {
    let models = |names: &[&str]| names.iter().map(|n| n.to_string()).collect::<Vec<_>>();
    assert!(fallback_problem("a", &models(&["b", "c"])).is_none());
    assert!(fallback_problem("a", &[]).is_none());
    assert!(fallback_problem("a", &models(&[" "])).is_some());
    assert!(fallback_problem("a", &models(&["a"])).is_some());
    assert!(fallback_problem("a", &models(&["b", "b"])).is_some());
}

fn envelope(event: Event) -> Envelope {
    Envelope {
        v: WIRE_VERSION,
        seq: 1,
        run_id: RunId::new("r"),
        ts_ms: 0,
        ignorable: false,
        event,
    }
}

fn retried(model: &str) -> Envelope {
    envelope(Event::ProviderRetry(ProviderRetryEvent {
        model: model.into(),
        retry: 1,
        delay_ms: 0,
        error: JsonObject::new(),
    }))
}

fn fell_back(from: &str, to: &str) -> Envelope {
    envelope(Event::ModelFallback(ModelFallbackEvent {
        from_model: from.into(),
        to_model: to.into(),
        error: JsonObject::new(),
    }))
}

fn answered() -> Envelope {
    envelope(Event::Assistant(AssistantEvent {
        message: Message::assistant_text("ok"),
        stop_reason: StopReason::EndTurn,
    }))
}

#[test]
fn a_run_stands_where_its_durable_events_left_it() {
    let chain = vec!["b".to_owned(), "c".to_owned()];
    assert_eq!(Standing::of(&[], &chain), Standing::default());
    let turn = [retried("a"), retried("a")];
    let standing = Standing::of(&turn, &chain);
    assert_eq!((standing.fallbacks, standing.retries), (0, 2));
    assert_eq!(standing.model("a", &chain), "a");

    let over = [retried("a"), fell_back("a", "b"), retried("b")];
    let standing = Standing::of(&over, &chain);
    assert_eq!(
        (standing.fallbacks, standing.retries, standing.failed_over),
        (1, 1, true),
        "the fallback's own retries count afresh"
    );
    assert_eq!(standing.model("a", &chain), "b");

    let later = [retried("a"), fell_back("a", "b"), answered(), retried("b")];
    let standing = Standing::of(&later, &chain);
    assert_eq!(
        (standing.fallbacks, standing.retries, standing.failed_over),
        (1, 1, false),
        "a new turn keeps the fallback but not the turn's failover"
    );
    let last = [fell_back("a", "b"), fell_back("b", "c")];
    assert_eq!(Standing::of(&last, &chain).model("a", &chain), "c");
}
