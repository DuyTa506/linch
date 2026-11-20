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

use linch_core::JsonValue;
use reqwest::header::ACCEPT;

use crate::ApiKey;

/// How long a probe may take in all (Python's `context_window_timeout`).
pub(crate) const PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// Most bytes of a probe's answer read: a window is a few hundred bytes, a
/// server's `/props` a few KiB. A longer answer is dropped unread.
const MAX_PROBE_BODY: usize = 64 * 1024;

/// The key under which a server that serves one model files its window,
/// so any model name finds it.
const SOLE_MODEL: &str = "*";

/// The window of `model` in `table`, else `fallback`.
pub(crate) fn known(table: &[(&str, u64)], model: &str, fallback: u64) -> u64 {
    in_table(table, model).unwrap_or(fallback)
}

/// The window `table` names for `model`.
pub(crate) fn in_table(table: &[(&str, u64)], model: &str) -> Option<u64> {
    (table.iter())
        .find(|(name, _)| *name == model)
        .map(|(_, window)| *window)
}

/// How a Chat Completions target learns its context window.
#[derive(Debug, Clone, Copy)]
pub(crate) enum ContextRule {
    /// From a table of models, `DEFAULT_CONTEXT_WINDOW` for the others.
    Known(&'static [(&'static str, u64)]),
    /// `GET {base}/models`, each model's `max_model_len`.
    Models,
    /// `GET {base}/props`, then the other `/props` path, for `n_ctx`.
    Props,
}

/// The windows a server reported, by model id.
pub(crate) type Probed = HashMap<String, u64>;

/// The window the server reported for `model`: its own, or the sole
/// model's.
pub(crate) fn probed_lookup(probed: Option<&Probed>, model: &str) -> Option<u64> {
    let probed = probed?;
    (probed.get(model).or_else(|| probed.get(SOLE_MODEL))).copied()
}

/// The window `/props` reports, for a server that serves one model.
pub(crate) async fn props_window(
    client: &reqwest::Client,
    base_url: &str,
    key: Option<&ApiKey>,
) -> Option<u64> {
    let probed = probe(client, ContextRule::Props, base_url, key).await;
    probed.get(SOLE_MODEL).copied()
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
    let mut response = request.send().await.ok()?;
    if !response.status().is_success() {
        return None;
    }
    let mut body = Vec::new();
    while let Some(chunk) = response.chunk().await.ok()? {
        body.extend_from_slice(&chunk);
        if body.len() > MAX_PROBE_BODY {
            return None;
        }
    }
    serde_json::from_slice(&body).ok()
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

/// `{base}/props`, and `{base}/v1/props` too when the base does not end in
/// `/v1`. Never the parent of the base: a tenant's `base_url` is admitted by
/// its path, and the probe sends the key.
pub(crate) fn props_urls(base_url: &str) -> Vec<String> {
    let base = base_url.trim_end_matches('/');
    let mut urls = vec![format!("{base}/props")];
    if !base.ends_with("/v1") {
        urls.push(format!("{base}/v1/props"));
    }
    urls
}

#[cfg(test)]
mod tests;
