//! [`HttpClients`]: HTTP clients shared by the providers of one holder.
//!
//! A `reqwest::Client` owns a connection pool and a TLS configuration, about
//! 90 KB, and a provider built with its own shares neither with another.
//! A holder (one per tenant in a server) hands the same client to every
//! provider whose settings match, and drops it once no provider holds it.

use std::collections::HashMap;
use std::fmt;
use std::ops::Deref;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, Weak};
use std::time::Duration;

use reqwest::header::HeaderMap;

/// An HTTP client that may be shared with other providers. It dereferences
/// to the `reqwest::Client`; a clone is another handle on the same one.
#[derive(Debug, Clone)]
pub(crate) struct SharedClient(Arc<reqwest::Client>);

impl SharedClient {
    pub(crate) fn new(client: reqwest::Client) -> Self {
        Self(Arc::new(client))
    }
}

impl Deref for SharedClient {
    type Target = reqwest::Client;

    fn deref(&self) -> &reqwest::Client {
        &self.0
    }
}

/// What makes two providers' clients interchangeable: the timeouts and the
/// default headers baked into the client. A key is sent per request and an
/// endpoint is part of the URL, so neither is in it. The header values are
/// compared as they are and never printed.
#[derive(PartialEq, Eq, Hash)]
struct ClientKey {
    connect: Duration,
    read: Duration,
    headers: Vec<(String, Vec<u8>)>,
}

impl ClientKey {
    fn new(connect: Duration, read: Duration, headers: &HeaderMap) -> Self {
        let mut headers: Vec<(String, Vec<u8>)> = (headers.iter())
            .map(|(name, value)| (name.as_str().to_owned(), value.as_bytes().to_vec()))
            .collect();
        headers.sort();
        Self {
            connect,
            read,
            headers,
        }
    }
}

/// The HTTP clients of one holder, by settings.
///
/// Cloning gives another handle on the same set. The holder keeps only a
/// weak reference to each client, so one goes when the last provider that
/// uses it does. Never share one `HttpClients` across tenants: a tenant's
/// default headers may carry its credentials.
#[derive(Clone, Default)]
pub struct HttpClients {
    clients: Arc<Mutex<HashMap<ClientKey, Weak<reqwest::Client>>>>,
}

impl HttpClients {
    /// No clients yet.
    pub fn new() -> Self {
        Self::default()
    }

    /// How many clients some provider holds now.
    pub fn live(&self) -> usize {
        (lock(&self.clients).values())
            .filter(|client| client.strong_count() > 0)
            .count()
    }

    /// The client for these settings: the one a provider holds, or
    /// `build`'s, which then joins the set.
    pub(crate) fn get(
        &self,
        connect: Duration,
        read: Duration,
        headers: &HeaderMap,
        build: impl FnOnce() -> reqwest::Result<reqwest::Client>,
    ) -> reqwest::Result<SharedClient> {
        let key = ClientKey::new(connect, read, headers);
        let mut clients = lock(&self.clients);
        clients.retain(|_, client| client.strong_count() > 0);
        if let Some(client) = clients.get(&key).and_then(Weak::upgrade) {
            return Ok(SharedClient(client));
        }
        let client = Arc::new(build()?);
        clients.insert(key, Arc::downgrade(&client));
        Ok(SharedClient(client))
    }
}

impl PartialEq for HttpClients {
    fn eq(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.clients, &other.clients)
    }
}

impl fmt::Debug for HttpClients {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("HttpClients")
            .field("live", &self.live())
            .finish()
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    // No code panics while holding this lock.
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

#[cfg(test)]
mod tests {
    use std::cell::Cell;

    use reqwest::header::HeaderValue;

    use super::*;

    const CONNECT: Duration = Duration::from_secs(30);
    const READ: Duration = Duration::from_secs(300);

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut map = HeaderMap::new();
        for (name, value) in pairs {
            map.insert(*name, HeaderValue::from_static(value));
        }
        map
    }

    /// Gets a client and counts the builds.
    fn get(
        clients: &HttpClients,
        read: Duration,
        map: &HeaderMap,
        builds: &Cell<u32>,
    ) -> SharedClient {
        let build = || {
            builds.set(builds.get() + 1);
            Ok(reqwest::Client::new())
        };
        clients.get(CONNECT, read, map, build).unwrap()
    }

    #[test]
    fn fifty_providers_with_one_key_build_one_client() {
        let (clients, builds) = (HttpClients::new(), Cell::new(0));
        let held: Vec<_> = (0..50)
            .map(|_| get(&clients, READ, &headers(&[("x-team", "a")]), &builds))
            .collect();
        assert_eq!((builds.get(), clients.live()), (1, 1));
        drop(held);
        assert_eq!(clients.live(), 0, "no provider holds it");
        let _again = get(&clients, READ, &HeaderMap::new(), &builds);
        assert_eq!(builds.get(), 2, "a client no one holds is built again");
    }

    #[test]
    fn other_settings_are_other_clients_and_header_order_is_not_a_setting() {
        let (clients, builds) = (HttpClients::new(), Cell::new(0));
        let ab = headers(&[("x-a", "1"), ("x-b", "2")]);
        let ba = headers(&[("x-b", "2"), ("x-a", "1")]);
        let _first = get(&clients, READ, &ab, &builds);
        let _same = get(&clients, READ, &ba, &builds);
        assert_eq!(builds.get(), 1);
        let _read = get(&clients, Duration::from_secs(600), &ab, &builds);
        let _value = get(
            &clients,
            READ,
            &headers(&[("x-a", "1"), ("x-b", "3")]),
            &builds,
        );
        let _name = get(&clients, READ, &headers(&[("x-a", "1")]), &builds);
        assert_eq!((builds.get(), clients.live()), (4, 4));
    }

    #[test]
    fn two_holders_share_nothing() {
        let (a, b, builds) = (HttpClients::new(), HttpClients::new(), Cell::new(0));
        let _one = get(&a, READ, &HeaderMap::new(), &builds);
        let _two = get(&b, READ, &HeaderMap::new(), &builds);
        assert_eq!(builds.get(), 2);
        assert!(a != b && a == a.clone());
    }

    #[test]
    fn a_failed_build_is_not_remembered() {
        let clients = HttpClients::new();
        let failed = clients.get(CONNECT, READ, &HeaderMap::new(), || {
            let proxy = reqwest::Proxy::all("")?;
            reqwest::Client::builder().proxy(proxy).build()
        });
        assert!(failed.is_err());
        assert_eq!(clients.live(), 0);
    }
}
