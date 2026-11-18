//! Prompt-cache isolation between tenants that share one provider key: a
//! tenant whose operator turned it on sends a salt derived from its name,
//! stable for the tenant and different for every other, in the request
//! field its provider has for one. The salt never reaches an event, an
//! error or a client; a provider with no such field is refused.

mod common;

use std::io::{Read, Write};
use std::net::TcpListener;
use std::sync::{Arc, Mutex};
use std::thread;

use common::*;
use linch_rpc::{Access, Allowlists, Tenant};
use serde_json::{Value, json};

const SECRET: &[u8] = b"operator-secret-0123456789abcdef";

/// A vLLM-like server that answers every request with a 400 that echoes the
/// request body, as a server may, and keeps each body. Returns its base
/// URL (with `/v1`) and the bodies it read.
fn echoing_server() -> (String, Arc<Mutex<Vec<Value>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/v1", listener.local_addr().unwrap());
    let bodies = Arc::new(Mutex::new(Vec::new()));
    let kept = Arc::clone(&bodies);
    thread::spawn(move || {
        for socket in listener.incoming() {
            let Ok(mut socket) = socket else { return };
            let Some(body) = read_body(&mut socket) else {
                continue;
            };
            let reply = json!({"error": {"message": format!("bad request: {body}")}}).to_string();
            let response = format!(
                "HTTP/1.1 400 Bad Request\r\ncontent-type: application/json\r\n\
                 content-length: {}\r\nconnection: close\r\n\r\n{reply}",
                reply.len()
            );
            if let Ok(parsed) = serde_json::from_str(&body) {
                kept.lock().unwrap().push(parsed);
            }
            let _ = socket.write_all(response.as_bytes());
        }
    });
    (url, bodies)
}

/// The body of one HTTP request: everything after the head, up to its
/// `content-length`.
fn read_body(socket: &mut std::net::TcpStream) -> Option<String> {
    let mut seen = Vec::new();
    let mut buffer = [0; 8192];
    loop {
        let read = socket.read(&mut buffer).ok()?;
        if read == 0 {
            return None;
        }
        seen.extend_from_slice(&buffer[..read]);
        let text = String::from_utf8_lossy(&seen).into_owned();
        let Some((head, body)) = text.split_once("\r\n\r\n") else {
            continue;
        };
        let wanted = head
            .to_ascii_lowercase()
            .lines()
            .find_map(|line| line.strip_prefix("content-length:")?.trim().parse().ok())
            .unwrap_or(0usize);
        if body.len() >= wanted {
            return Some(body.to_owned());
        }
    }
}

fn allow(url: &str) -> Allowlists {
    Allowlists::new()
        .provider_base_urls(&[url.to_owned()])
        .unwrap()
}

/// A tenant of the operator's secret, isolated or not.
fn tenant_access(name: &str, url: &str, isolated: bool) -> Access {
    let mut made = Tenant::new(name, allow(url)).unwrap();
    if isolated {
        made = made.with_cache_isolation(SECRET).unwrap();
    }
    Access::Tenant(Arc::new(made).connect().unwrap())
}

fn vllm_config(namespace: &str, url: &str) -> Value {
    json!({
        "namespace": namespace,
        "spec": {"model": "qwen", "max_turns": 2},
        "provider": {"id": "vllm", "base_url": url},
    })
}

/// Run one prompt on a fresh agent of `rpc` and return everything the
/// client saw of it, as one string.
async fn run_once(rpc: &mut Rpc, namespace: &str, url: &str) -> String {
    let agent = rpc.open(vllm_config(namespace, url)).await;
    let run_id = rpc.start(&agent, "s1", "hello").await;
    let ended = rpc.until_end(&run_id).await;
    let events: Vec<Value> = ended
        .envelopes
        .iter()
        .map(|envelope| envelope.event.to_value().unwrap())
        .collect();
    format!("{events:?} {:?}", ended.error)
}

fn salts(bodies: &Arc<Mutex<Vec<Value>>>) -> Vec<String> {
    let bodies = bodies.lock().unwrap();
    (bodies.iter())
        .filter_map(|body| body["cache_salt"].as_str().map(str::to_owned))
        .collect()
}

#[tokio::test]
async fn each_isolated_tenant_sends_its_own_stable_salt_and_never_shows_it() {
    let (url, bodies) = echoing_server();
    let mut acme = Rpc::remote(tenant_access("acme", &url, true));
    let mut beta = Rpc::remote(tenant_access("beta", &url, true));

    let mut taken = 0;
    let mut fresh = || {
        let all = salts(&bodies);
        let new = all[taken..].to_vec();
        taken = all.len();
        new
    };
    let seen_by_acme = run_once(&mut acme, "acme", &url).await;
    let acme_salts = fresh();
    let seen_by_beta = run_once(&mut beta, "beta", &url).await;
    let beta_salts = fresh();
    // The same tenant keeps its salt in another namespace and on a new run.
    let again = run_once(&mut acme, "acme/support", &url).await;
    let acme_again = fresh();

    assert!(!acme_salts.is_empty() && !beta_salts.is_empty());
    let acme_salt = acme_salts[0].clone();
    assert_eq!(acme_salt.len(), 64);
    assert!(acme_salt.bytes().all(|b| b.is_ascii_hexdigit()));
    assert!(!acme_salt.contains("acme"), "the name is not the salt");
    assert!(
        acme_salts
            .iter()
            .chain(&acme_again)
            .all(|s| *s == acme_salt)
    );
    assert!(beta_salts.iter().all(|salt| *salt != acme_salt));
    assert!(beta_salts.iter().all(|salt| *salt == beta_salts[0]));

    // The echoing 400 carried the salt back; nothing the client saw has it.
    let every: Vec<&String> = (acme_salts.iter())
        .chain(&beta_salts)
        .chain(&acme_again)
        .collect();
    for seen in [&seen_by_acme, &seen_by_beta, &again] {
        for salt in &every {
            assert!(!seen.contains(salt.as_str()), "the salt reached the client");
        }
    }
}

/// A second process of the same fleet, with the same secret, derives the
/// same salt: its admission writes the same value into an agent. Shown by
/// the wire: two separate `Tenant`s of one name send one salt.
#[tokio::test]
async fn two_processes_with_one_secret_send_the_same_salt() {
    let (url, bodies) = echoing_server();
    let mut first = Rpc::remote(tenant_access("acme", &url, true));
    run_once(&mut first, "acme", &url).await;
    let mut second = Rpc::remote(tenant_access("acme", &url, true));
    run_once(&mut second, "acme", &url).await;
    let sent = salts(&bodies);
    assert!(sent.len() >= 2);
    assert!(sent.iter().all(|salt| *salt == sent[0]), "{sent:?}");
}

#[tokio::test]
async fn a_tenant_without_isolation_sends_no_salt() {
    let (url, bodies) = echoing_server();
    let mut plain = Rpc::remote(tenant_access("acme", &url, false));
    run_once(&mut plain, "acme", &url).await;
    let bodies = bodies.lock().unwrap();
    assert!(!bodies.is_empty());
    for body in bodies.iter() {
        assert!(body.get("cache_salt").is_none(), "{body}");
        assert!(body.get("prompt_cache_key").is_none(), "{body}");
    }
}

#[tokio::test]
async fn a_tenant_cannot_choose_or_replace_its_salt() {
    let (url, bodies) = echoing_server();
    let allowed = allow(&url).provider_request(true);
    let made = Tenant::new("acme", allowed)
        .unwrap()
        .with_cache_isolation(SECRET)
        .unwrap();
    let mut rpc = Rpc::remote(Access::Tenant(Arc::new(made).connect().unwrap()));
    let mut config = vllm_config("acme", &url);
    config["provider"]["request"] = json!({"extra_body": {"cache_salt": "another-tenants-salt"}});
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "hello").await;
    rpc.until_end(&run_id).await;
    let sent = salts(&bodies);
    assert!(!sent.is_empty());
    assert!(sent.iter().all(|salt| salt != "another-tenants-salt"));
    // The config cannot carry a salt of its own either.
    let mut config = vllm_config("acme", &url);
    config["provider"]["request"] = json!({"cache_salt": "x"});
    assert!(rpc.call("agent.open", config).await.is_err());
}

#[tokio::test]
async fn an_isolated_tenant_on_sglang_sends_its_salt_in_cache_salt() {
    let (url, bodies) = echoing_server();
    let mut rpc = Rpc::remote(tenant_access("acme", &url, true));
    let mut config = vllm_config("acme", &url);
    config["provider"] = json!({"id": "sglang", "base_url": url});
    let agent = rpc.open(config).await;
    let run_id = rpc.start(&agent, "s1", "hello").await;
    rpc.until_end(&run_id).await;
    let sent = salts(&bodies);
    assert!(!sent.is_empty() && sent.iter().all(|salt| salt.len() == 64));
}

#[tokio::test]
async fn a_provider_with_no_field_for_it_is_refused_not_left_shared() {
    let (url, _) = echoing_server();
    let rpc = Rpc::remote(tenant_access("acme", &url, true));
    for provider in [
        json!({"id": "anthropic", "base_url": url}),
        json!({"id": "gemini", "base_url": url}),
        json!({"id": "deepseek", "base_url": url}),
        json!({"id": "llamacpp", "base_url": url}),
    ] {
        let mut config = vllm_config("acme", &url);
        config["provider"] = provider;
        assert_eq!(
            refused(&rpc, config).await.as_deref(),
            Some("cache_isolation")
        );
    }
    // The scripted provider makes no request, so there is nothing to isolate.
    let made = Tenant::new("acme", Allowlists::new().scripted_provider(true))
        .unwrap()
        .with_cache_isolation(SECRET)
        .unwrap();
    let scripted_rpc = Rpc::remote(Access::Tenant(Arc::new(made).connect().unwrap()));
    let mut config = scripted(vec![], None, "");
    config["namespace"] = json!("acme");
    scripted_rpc.open(config).await;
}

#[test]
fn a_secret_that_is_too_short_is_refused() {
    let tenant = Tenant::new("acme", Allowlists::new()).unwrap();
    let error = tenant.with_cache_isolation(b"short").unwrap_err();
    assert!(!error.to_string().contains("short"));
}
