//! Context windows: the known model tables, and the probes a self-hosted
//! server answers (Python's `providers/_context_probe.py` and the llama.cpp
//! `/props` probe).
//!
//! vLLM and SGLang list each served model's `max_model_len` on
//! `GET /v1/models`; llama.cpp reports its context size as
//! `default_generation_settings.n_ctx` (or a top-level `n_ctx`) on
//! `GET /props`. A probe runs once, in [`linch_core::Provider::prepare`],
//! within [`PROBE_TIMEOUT`]; one that fails or finds no positive value
//! leaves the default.

use std::collections::HashMap;
use std::time::Duration;

use linch_core::{DEFAULT_CONTEXT_WINDOW, JsonValue};
use reqwest::header::ACCEPT;

use crate::ApiKey;

/// How long a probe may take in all (Python's `context_window_timeout`).
pub(crate) const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// The key under which a server that serves one model files its window,
/// so any model name finds it.
const SOLE_MODEL: &str = "*";

/// The window of `model` in `table`, else `fallback`.
pub(crate) fn known(table: &[(&str, u64)], model: &str, fallback: u64) -> u64 {
    (table.iter())
        .find(|(name, _)| *name == model)
        .map_or(fallback, |(_, window)| *window)
}

/// How a Chat Completions target learns its context window.
#[derive(Debug, Clone, Copy)]
pub(crate) enum ContextRule {
    /// From a table of models, [`DEFAULT_CONTEXT_WINDOW`] for the others.
    Known(&'static [(&'static str, u64)]),
    /// `GET {base}/models`, each model's `max_model_len`.
    Models,
    /// `GET {base}/props`, then the other `/props` path, for `n_ctx`.
    Props,
}

/// The windows a server reported, by model id.
pub(crate) type Probed = HashMap<String, u64>;

/// The window of `model` among `probed`: its own, the sole model's, else
/// the default.
pub(crate) fn probed_window(probed: Option<&Probed>, model: &str) -> u64 {
    let probed = probed.map(|probed| probed.get(model).or_else(|| probed.get(SOLE_MODEL)));
    probed.flatten().copied().unwrap_or(DEFAULT_CONTEXT_WINDOW)
}

/// Ask the server at `base_url` for its windows by `rule`; empty when the
/// rule has no probe, the probe fails, or it runs out of time.
pub(crate) async fn probe(
    client: &reqwest::Client,
    rule: ContextRule,
    base_url: &str,
    key: Option<&ApiKey>,
) -> Probed {
    let asked = async {
        match rule {
            ContextRule::Known(_) => Probed::new(),
            ContextRule::Models => {
                let url = format!("{}/models", base_url.trim_end_matches('/'));
                let listed = get_json(client, &url, key).await;
                listed.map(|value| models(&value)).unwrap_or_default()
            }
            ContextRule::Props => {
                for url in props_urls(base_url) {
                    let found = get_json(client, &url, key).await.and_then(|v| n_ctx(&v));
                    if let Some(window) = found {
                        return Probed::from([(SOLE_MODEL.to_owned(), window)]);
                    }
                }
                Probed::new()
            }
        }
    };
    tokio::time::timeout(PROBE_TIMEOUT, asked)
        .await
        .unwrap_or_default()
}

/// The JSON body of a 2xx `GET url`, else `None`.
async fn get_json(client: &reqwest::Client, url: &str, key: Option<&ApiKey>) -> Option<JsonValue> {
    let mut request = client.get(url).header(ACCEPT, "application/json");
    if let Some(key) = key {
        request = request.bearer_auth(key.expose());
    }
    let response = request.send().await.ok()?;
    if !response.status().is_success() {
        return None;
    }
    response.json().await.ok()
}

/// Each listed model's positive `max_model_len`; a list of one model also
/// files it under [`SOLE_MODEL`].
fn models(value: &JsonValue) -> Probed {
    let rows = value.get("data").and_then(JsonValue::as_array);
    let rows = rows.map(Vec::as_slice).unwrap_or_default();
    let mut probed = Probed::new();
    for row in rows {
        let id = row.get("id").and_then(JsonValue::as_str);
        let window = row.get("max_model_len").and_then(JsonValue::as_u64);
        if let (Some(id), Some(window)) = (id.filter(|id| !id.is_empty()), window)
            && window > 0
        {
            probed.insert(id.to_owned(), window);
            if rows.len() == 1 {
                probed.insert(SOLE_MODEL.to_owned(), window);
            }
        }
    }
    probed
}

/// A positive `n_ctx`, from `default_generation_settings` first.
fn n_ctx(value: &JsonValue) -> Option<u64> {
    let settings = value.get("default_generation_settings");
    let nested = settings
        .and_then(|s| s.get("n_ctx"))
        .and_then(JsonValue::as_u64);
    let top = value.get("n_ctx").and_then(JsonValue::as_u64);
    nested.filter(|&n| n > 0).or(top.filter(|&n| n > 0))
}

/// `{base}/props`, then the root's `/props` when the base ends in `/v1`,
/// else `{base}/v1/props`.
pub(crate) fn props_urls(base_url: &str) -> Vec<String> {
    let base = base_url.trim_end_matches('/');
    let other = match base.strip_suffix("/v1") {
        Some(root) => format!("{}/props", root.trim_end_matches('/')),
        None => format!("{base}/v1/props"),
    };
    vec![format!("{base}/props"), other]
}

#[cfg(test)]
mod tests;
