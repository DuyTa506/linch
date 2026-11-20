//! `eval.run` and `run.report`.

use linch::JsonValue;
use linch::ext::evals::{EvalSuite, run_eval};
use linch::ext::reports::build_run_report;
use serde_json::json;

use super::MAX_LINE_BYTES;
use super::{Server, State, lock};
use crate::error::{ErrorObject, code, invalid_params};
use crate::protocol::{EvalRunParams, ReportParams};

/// The longest string a `run.report` answer keeps; a longer one is cut and
/// marked.
const REPORT_STRING_BYTES: usize = 64 * 1024;
/// The bounds a tool call's text is shortened to, one after the other, while
/// the report is still over [`REPORT_MAX_BYTES`].
const REPORT_TOOL_TEXT_BYTES: [usize; 2] = [8 * 1024, 1024];
/// The most bytes of a `run.report` answer: half of [`MAX_LINE_BYTES`].
/// Past it the timeline loses its last events.
const REPORT_MAX_BYTES: usize = MAX_LINE_BYTES / 2;

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
            result = run_eval(&agent, &suite.cases, &suite.scorers) => result,
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
        let mut report = serde_json::to_value(&report).map_err(|error| {
            ErrorObject::new(
                code::INTERNAL_ERROR,
                format!("report not encodable: {error}"),
            )
        })?;
        let mut markdown = JsonValue::String(markdown);
        let strings = cut_strings(&mut report, REPORT_STRING_BYTES)
            + cut_strings(&mut markdown, REPORT_STRING_BYTES);
        let mut answer = json!({"report": report, "markdown": markdown});
        let omitted = fit_timeline(&mut answer);
        let shortened = fit_tool_calls(&mut answer)?;
        if strings > 0 || omitted > 0 || shortened > 0 {
            answer["truncated"] = json!({"strings": strings});
            if omitted > 0 {
                answer["truncated"]["timeline_omitted"] = json!(omitted);
            }
            if shortened > 0 {
                answer["truncated"]["tool_call_text"] = json!(shortened);
            }
        }
        Ok(answer)
    }
}

/// Cut every string in `value` longer than `bound`, leaving a marker with
/// the bytes dropped; how many were cut.
fn cut_strings(value: &mut JsonValue, bound: usize) -> u64 {
    match value {
        JsonValue::String(text) if text.len() > bound => {
            let mut keep = bound;
            while !text.is_char_boundary(keep) {
                keep -= 1;
            }
            let dropped = text.len() - keep;
            text.truncate(keep);
            text.push_str(&format!("... [truncated: {dropped} more bytes]"));
            1
        }
        JsonValue::Array(items) => items.iter_mut().map(|v| cut_strings(v, bound)).sum(),
        JsonValue::Object(members) => members.values_mut().map(|v| cut_strings(v, bound)).sum(),
        _ => 0,
    }
}

/// Drop the last timeline events until the answer is within
/// [`REPORT_MAX_BYTES`] or the timeline is empty; how many were dropped.
fn fit_timeline(answer: &mut JsonValue) -> u64 {
    let mut over = answer.to_string().len().saturating_sub(REPORT_MAX_BYTES);
    let mut omitted = 0;
    if let Some(timeline) = answer["report"]["timeline"].as_array_mut() {
        while over > 0 {
            let Some(last) = timeline.pop() else { break };
            over = over.saturating_sub(last.to_string().len() + 1);
            omitted += 1;
        }
    }
    omitted
}

/// Shorten the text of the tool calls to each bound of
/// [`REPORT_TOOL_TEXT_BYTES`] in turn until the answer is within
/// [`REPORT_MAX_BYTES`]; how many tool calls were shortened. When even the
/// shortest form is too long the answer is refused.
fn fit_tool_calls(answer: &mut JsonValue) -> Result<u64, ErrorObject> {
    let mut shortened = 0;
    for bound in REPORT_TOOL_TEXT_BYTES {
        if answer.to_string().len() <= REPORT_MAX_BYTES {
            return Ok(shortened);
        }
        if let Some(calls) = answer["report"]["tool_calls"].as_array_mut() {
            // A call cut at one bound is cut again at the next, so the last
            // count is the whole.
            shortened = calls
                .iter_mut()
                .map(|call| cut_strings(call, bound))
                .filter(|&cut| cut > 0)
                .count() as u64;
        }
    }
    if answer.to_string().len() <= REPORT_MAX_BYTES {
        return Ok(shortened);
    }
    let message = format!(
        "the report is longer than {REPORT_MAX_BYTES} bytes even with its timeline dropped and \
         the text of every tool call cut to {} byte(s)",
        REPORT_TOOL_TEXT_BYTES[REPORT_TOOL_TEXT_BYTES.len() - 1]
    );
    Err(ErrorObject::new(code::INTERNAL_ERROR, message))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_long_string_is_cut_on_a_char_boundary_and_counted() {
        // 3-byte characters: the cut at 64 KiB falls inside one.
        let text = "\u{20ac}".repeat(REPORT_STRING_BYTES);
        let long = "x".repeat(REPORT_STRING_BYTES + 1);
        let mut value = json!({"a": [text, "short"], "b": {"c": long}});
        assert_eq!(cut_strings(&mut value, REPORT_STRING_BYTES), 2);
        let kept = value["a"][0].as_str().unwrap();
        assert!(kept.starts_with('\u{20ac}') && kept.ends_with(" more bytes]"));
        assert!(kept.len() < REPORT_STRING_BYTES + 64);
        assert_eq!(value["a"][1], "short");
        let cut = value["b"]["c"].as_str().unwrap();
        assert!(cut.ends_with("[truncated: 1 more bytes]"));
    }

    #[test]
    fn a_timeline_over_the_bound_loses_its_last_events_only() {
        let event = json!({"text": "x".repeat(60 * 1024)});
        let timeline = vec![event; 200];
        let mut answer = json!({"report": {"timeline": timeline}, "markdown": "m"});
        let omitted = fit_timeline(&mut answer);
        assert!(omitted > 0);
        let kept = answer["report"]["timeline"].as_array().unwrap().len() as u64;
        assert_eq!(kept + omitted, 200);
        assert!(answer.to_string().len() <= REPORT_MAX_BYTES);
        let mut small = json!({"report": {"timeline": [{"a": 1}]}});
        assert_eq!(fit_timeline(&mut small), 0);
    }

    #[test]
    fn tool_call_text_is_shortened_by_steps_until_the_report_fits() {
        // 150 calls of 100 KiB: over the bound whole, within it at 8 KiB.
        let call = json!({"input": {"q": "q".repeat(40 * 1024)}, "result": "r".repeat(60 * 1024)});
        let mut answer = json!({"report": {"tool_calls": vec![call; 150]}, "markdown": "m"});
        assert_eq!(fit_tool_calls(&mut answer).unwrap(), 150);
        let calls = answer["report"]["tool_calls"].as_array().unwrap();
        assert_eq!(calls.len(), 150);
        let result = calls[0]["result"].as_str().unwrap();
        assert!(result.len() > REPORT_TOOL_TEXT_BYTES[1] && result.len() < 9 * 1024);
        // A report within the bound is left alone.
        let mut small = json!({"report": {"tool_calls": [{"result": "r".repeat(60 * 1024)}]}});
        assert_eq!(fit_tool_calls(&mut small).unwrap(), 0);
        assert_eq!(
            small["report"]["tool_calls"][0]["result"]
                .as_str()
                .unwrap()
                .len(),
            60 * 1024
        );
    }

    #[test]
    fn a_report_that_cannot_be_shortened_enough_is_refused() {
        let call = json!({"result": "r".repeat(2048)});
        let mut answer = json!({"report": {"tool_calls": vec![call; 9000]}, "markdown": "m"});
        let error = fit_tool_calls(&mut answer).unwrap_err();
        assert_eq!(error.code, code::INTERNAL_ERROR);
    }
}
