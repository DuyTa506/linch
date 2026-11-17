//! Noticing a client that vanished without closing its connection: a
//! power cut, a dropped route, a killed VM. Without it the connection keeps
//! its slot among the tenant's `max_connections`, its workflow hold and its
//! calls in flight until the server restarts.
//!
//! - **TCP keepalive** on every accepted TCP socket, and a TCP user timeout
//!   as long as the keepalive takes to give up, so a peer that answers
//!   neither probes nor data is dropped by the kernel too when the server
//!   has data waiting to send (probes do not run then).
//! - **A WebSocket ping** every `ping_interval`; a connection that sent no
//!   frame at all for `pong_timeout` is ended. Keepalive probes are
//!   answered by the peer's kernel, so a client that is alive but hung
//!   passes them; a ping is answered by the client's WebSocket code.
//!
//! A Unix socket has neither: the kernel closes it when the peer's process
//! ends, whatever the cause.

use std::io;
use std::time::Duration;

use serde::Deserialize;
use socket2::{SockRef, TcpKeepalive};
use tokio::net::TcpStream;

/// The `liveness` section: how the server notices a vanished client on
/// its HTTP listener. Each field is at least 1 when given.
#[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct LivenessConfig {
    /// Idle time before the first TCP keepalive probe, in milliseconds;
    /// 30000 when absent.
    pub keepalive_idle_ms: Option<u64>,
    /// Time between two probes, in milliseconds; 10000 when absent.
    pub keepalive_interval_ms: Option<u64>,
    /// Probes without an answer before the kernel drops the connection;
    /// 3 when absent.
    pub keepalive_probes: Option<u32>,
    /// Time between WebSocket pings, in milliseconds; 30000 when absent.
    pub ping_interval_ms: Option<u64>,
    /// How long a WebSocket may send nothing (no frame, no pong) before
    /// the server ends it, in milliseconds; 60000 when absent. A client
    /// that answers pings sends something at least every `ping_interval_ms`.
    pub pong_timeout_ms: Option<u64>,
}

impl LivenessConfig {
    /// The name of the first field that is 0.
    pub(crate) fn zero_field(&self) -> Option<&'static str> {
        let zero = |value: Option<u64>| value == Some(0);
        [
            ("keepalive_idle_ms", zero(self.keepalive_idle_ms)),
            ("keepalive_interval_ms", zero(self.keepalive_interval_ms)),
            ("keepalive_probes", self.keepalive_probes == Some(0)),
            ("ping_interval_ms", zero(self.ping_interval_ms)),
            ("pong_timeout_ms", zero(self.pong_timeout_ms)),
        ]
        .into_iter()
        .find_map(|(name, is_zero)| is_zero.then_some(name))
    }

    pub(crate) fn resolve(&self) -> Liveness {
        let millis =
            |value: Option<u64>, default: u64| Duration::from_millis(value.unwrap_or(default));
        Liveness {
            keepalive_idle: millis(self.keepalive_idle_ms, 30_000),
            keepalive_interval: millis(self.keepalive_interval_ms, 10_000),
            keepalive_probes: self.keepalive_probes.unwrap_or(3),
            ping_interval: millis(self.ping_interval_ms, 30_000),
            pong_timeout: millis(self.pong_timeout_ms, 60_000),
        }
    }
}

/// [`LivenessConfig`] with its defaults filled in.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Liveness {
    pub(crate) keepalive_idle: Duration,
    pub(crate) keepalive_interval: Duration,
    pub(crate) keepalive_probes: u32,
    pub(crate) ping_interval: Duration,
    pub(crate) pong_timeout: Duration,
}

impl Default for Liveness {
    fn default() -> Self {
        LivenessConfig::default().resolve()
    }
}

impl Liveness {
    /// Turn TCP keepalive on for `stream`, and set its user timeout to the
    /// time the keepalive takes to give up (idle, then `probes` intervals).
    pub(crate) fn tune(&self, stream: &TcpStream) -> io::Result<()> {
        let socket = SockRef::from(stream);
        let keepalive = TcpKeepalive::new()
            .with_time(self.keepalive_idle)
            .with_interval(self.keepalive_interval)
            .with_retries(self.keepalive_probes);
        socket.set_tcp_keepalive(&keepalive)?;
        #[cfg(target_os = "linux")]
        {
            let giving_up = self.keepalive_idle + self.keepalive_interval * self.keepalive_probes;
            socket.set_tcp_user_timeout(Some(giving_up))?;
        }
        Ok(())
    }

    /// [`Self::tune`], reporting a failure on stderr: the connection
    /// still serves, without the check.
    pub(crate) fn tune_or_report(&self, stream: &TcpStream) {
        if let Err(error) = self.tune(stream) {
            eprintln!("linch-server: cannot set TCP keepalive on a connection: {error}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::TcpListener;

    #[tokio::test]
    async fn an_accepted_socket_has_keepalive_and_a_user_timeout() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let _client = TcpStream::connect(address).await.unwrap();
        let (accepted, _) = listener.accept().await.unwrap();
        let socket = SockRef::from(&accepted);
        assert!(!socket.keepalive().unwrap());

        Liveness::default().tune(&accepted).unwrap();
        assert!(socket.keepalive().unwrap());
        assert_eq!(
            socket.tcp_keepalive_time().unwrap(),
            Duration::from_secs(30)
        );
        assert_eq!(
            socket.tcp_keepalive_interval().unwrap(),
            Duration::from_secs(10)
        );
        assert_eq!(socket.tcp_keepalive_retries().unwrap(), 3);
        #[cfg(target_os = "linux")]
        assert_eq!(
            socket.tcp_user_timeout().unwrap(),
            Some(Duration::from_secs(60))
        );
    }

    #[test]
    fn a_zero_field_is_named() {
        let config = LivenessConfig {
            ping_interval_ms: Some(0),
            ..LivenessConfig::default()
        };
        assert_eq!(config.zero_field(), Some("ping_interval_ms"));
        assert_eq!(LivenessConfig::default().zero_field(), None);
    }
}
