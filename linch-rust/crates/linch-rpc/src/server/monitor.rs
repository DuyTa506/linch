//! `eval.run` and `run.report`.

use linch::JsonValue;
use linch::ext::evals::{EvalSuite, run_eval_as};
use linch::ext::reports::build_run_report;
use serde_json::json;

use super::{Server, State, lock};
use crate::error::{ErrorObject, code, invalid_params};
use crate::protocol::{EvalRunParams, ReportParams};

/// Most cases one `eval.run` of a tenant takes: they run one after another
/// under one run place and one call, so the count is what bounds the time and
/// the provider spend of that place.
const MAX_EVAL_CASES: usize = 256;

/// Keeps `agent.close` away from an agent while an eval runs on it.
struct Busy<'a> {
    state: &'a State,
    agent: String,
}

impl Drop for Busy<'_> {
    fn drop(&mut self) {
        let mut tables = lock(&self.state.tables);
        if let Some(count) = tables.evals.get_mut(&self.agent) {
            *count -= 1;
            if *count == 0 {
                tables.evals.remove(&self.agent);
            }
        }
    }
}

impl Server {
    /// Run the cases on the agent and answer with the scored result. The
    /// eval takes a place among the tenant's live runs for its whole
    /// duration, refuses `agent.close` meanwhile, and stops where it stands
    /// when the connection ends.
    pub(super) async fn eval_run(&self, params: EvalRunParams) -> Result<JsonValue, ErrorObject> {
        if self.0.tenant.is_some() && params.cases.len() > MAX_EVAL_CASES {
            let message = format!(
                "eval.run takes at most {MAX_EVAL_CASES} cases, got {}",
                params.cases.len()
            );
            return Err(invalid_params(&message));
        }
        let suite = EvalSuite {
            name: String::new(),
            cases: params.cases,
            scorers: params.scorers,
            metadata: Default::default(),
        };
        suite
            .validate()
            .map_err(|error| invalid_params(&error.to_string()))?;
        let (agent, _place, _busy) = {
            let mut tables = lock(&self.0.tables);
            let agent = tables.agent(&params.agent)?;
            let place = (self.0.tenant.as_ref())
                .map(|tenant| tenant.run_place())
                .transpose()?;
            *tables.evals.entry(params.agent.clone()).or_default() += 1;
            let busy = Busy {
                state: &self.0,
                agent: params.agent,
            };
            (agent, place, busy)
        };
        let result = tokio::select! {
            biased;
            () = self.0.shutdown.cancelled() => {
                return Err(ErrorObject::new(code::INTERNAL_ERROR, "the connection is closing"));
            }
            result = run_eval_as(&agent, &suite.cases, &suite.scorers, &self.0.owner) => result,
        };
        let mut value = result.to_value(params.include_events);
        value["markdown"] = JsonValue::String(result.to_markdown());
        Ok(value)
    }

    /// The report of one stored run, folded from its record and events.
    pub(super) async fn run_report(&self, params: ReportParams) -> Result<JsonValue, ErrorObject> {
        let agent = lock(&self.0.tables).agent(&params.agent)?;
        let record =
            (agent.run_record(&params.run_id).await).map_err(|error| ErrorObject::from(&error))?;
        let events =
            (agent.events(&params.run_id, 0).await).map_err(|error| ErrorObject::from(&error))?;
        let mut report = build_run_report(&events, Some(&record));
        let markdown = report.to_markdown();
        if !params.timeline {
            report.timeline.clear();
        }
        let report = serde_json::to_value(&report).map_err(|error| {
            ErrorObject::new(
                code::INTERNAL_ERROR,
                format!("report not encodable: {error}"),
            )
        })?;
        Ok(json!({"report": report, "markdown": markdown}))
    }
}
