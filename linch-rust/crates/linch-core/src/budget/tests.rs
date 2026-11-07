use super::*;
use crate::events::model_call::ModelCallKind;
use crate::ids::RunId;

fn receipt(
    usage: Option<Usage>,
    cost_usd: Option<f64>,
    disposition: CallDisposition,
) -> ModelCallReceiptEvent {
    ModelCallReceiptEvent {
        run_id: RunId::new("r"),
        logical_call_id: "r:primary:0".into(),
        delivery_attempt_id: "r:primary:0:1".into(),
        kind: ModelCallKind::Primary,
        actual_model: None,
        disposition,
        usage,
        cost_usd,
        duration_ms: 0,
    }
}

fn used(input: u64, output: u64, read: u64, created: u64) -> Option<Usage> {
    Some(Usage {
        input_tokens: input,
        output_tokens: output,
        cache_read_tokens: read,
        cache_creation_tokens: created,
    })
}

fn meter(max_tokens: Option<u64>, max_cost_usd: Option<f64>) -> Meter {
    let limits = RunBudget {
        max_tokens,
        max_cost_usd,
        ..RunBudget::default()
    };
    Meter::new(limits, Spent::default())
}

#[test]
fn a_charge_sums_every_bucket_and_an_unpriced_call_costs_nothing() {
    let meter = meter(None, None);
    meter.charge(&receipt(
        used(1, 2, 3, 4),
        Some(0.5),
        CallDisposition::Accepted,
    ));
    meter.charge(&receipt(used(10, 0, 0, 0), None, CallDisposition::Accepted));
    let spent = *meter.spent();
    assert_eq!((spent.tokens, spent.usd, spent.unknown), (20, 0.5, false));
    // Without a cap it only counts.
    assert!(meter.refusal().is_none());
    assert!(meter.take_warning().is_none());
}

#[test]
fn a_cap_is_reached_at_its_value() {
    let meter = meter(Some(10), None);
    meter.charge(&receipt(used(9, 0, 0, 0), None, CallDisposition::Accepted));
    assert!(meter.refusal().is_none());
    meter.charge(&receipt(used(1, 0, 0, 0), None, CallDisposition::Accepted));
    let Some(Refusal::Exceeded(event)) = meter.refusal() else {
        panic!("the cap is reached");
    };
    assert_eq!(event.kind, BudgetKind::Exceeded);
    assert_eq!((event.spent_tokens, event.max_tokens), (10, Some(10)));
}

#[test]
fn unknown_use_refuses_a_capped_budget_only() {
    let unknown = receipt(None, None, CallDisposition::Cancelled);
    let open = meter(None, None);
    open.charge(&unknown);
    assert!(open.refusal().is_none());
    let capped = meter(None, Some(1.0));
    capped.charge(&receipt(None, None, CallDisposition::NotDispatched));
    assert!(capped.refusal().is_none(), "a call never sent cost nothing");
    capped.charge(&unknown);
    let Some(refusal @ Refusal::Unknown) = capped.refusal() else {
        panic!("unknown use refuses");
    };
    assert_eq!(refusal.error().error["name"], "BudgetAccountingError");
}

#[test]
fn the_warning_fires_once_at_its_ratio_of_either_cap() {
    let meter = meter(Some(1000), Some(1.0));
    meter.charge(&receipt(
        used(1, 0, 0, 0),
        Some(0.89),
        CallDisposition::Accepted,
    ));
    assert!(meter.take_warning().is_none());
    meter.charge(&receipt(
        used(1, 0, 0, 0),
        Some(0.01),
        CallDisposition::Accepted,
    ));
    let warning = meter.take_warning().unwrap();
    assert_eq!(
        (warning.kind, warning.spent_tokens),
        (BudgetKind::Warning, 2)
    );
    assert!(meter.take_warning().is_none());
}

#[test]
fn the_spend_of_a_log_counts_its_receipts_and_its_warning() {
    let envelope = |event| Envelope {
        v: 1,
        seq: 0,
        run_id: RunId::new("r"),
        ts_ms: 0,
        ignorable: false,
        event,
    };
    let warning = BudgetEvent {
        kind: BudgetKind::Warning,
        spent_tokens: 5,
        spent_usd: 0.0,
        max_tokens: Some(5),
        max_cost_usd: None,
    };
    let events = [
        envelope(Event::ModelCallReceipt(receipt(
            used(3, 2, 0, 0),
            Some(0.25),
            CallDisposition::Accepted,
        ))),
        envelope(Event::Budget(warning)),
    ];
    let spent = Spent::of(&events);
    assert_eq!((spent.tokens, spent.usd, spent.warned), (5, 0.25, true));
    let both = spent.and(Spent::of(&events[..1]));
    assert_eq!((both.tokens, both.usd, both.warned), (10, 0.5, true));
}

#[test]
fn the_exceeded_error_reads_as_python_s() {
    let meter = meter(Some(1), None);
    meter.charge(&receipt(
        used(1, 0, 0, 0),
        Some(0.123456),
        CallDisposition::Accepted,
    ));
    let error = meter.refusal().unwrap().error().error;
    assert_eq!(error["name"], "BudgetExceededError");
    assert_eq!(
        error["message"],
        "run budget exhausted (1 tokens, $0.1235 spent)"
    );
    assert_eq!(error["retryable"], false);
}

#[test]
fn a_budget_reads_with_python_s_default_ratio_and_refuses_unknown_fields() {
    let budget: RunBudget = serde_json::from_str(r#"{"max_tokens": 5}"#).unwrap();
    assert_eq!(budget.warn_ratio, 0.9);
    assert!(serde_json::from_str::<RunBudget>(r#"{"max_token": 5}"#).is_err());
    let infinite = RunBudget {
        warn_ratio: f64::INFINITY,
        ..RunBudget::default()
    };
    assert!(infinite.problem().is_some());
    assert!(RunBudget::default().problem().is_none());
}
