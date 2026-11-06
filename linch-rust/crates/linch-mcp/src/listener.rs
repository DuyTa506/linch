//! The tool-change subscription of one connection (protocol 2026-07-28).

use std::sync::Arc;
use std::time::Duration;

use rmcp::model::{ServerNotification, SubscriptionFilter};
use rmcp::service::{Peer, RoleClient, ServiceError, Subscription, SubscriptionEnd};
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use tokio_util::sync::CancellationToken;

/// Longest wait for the subscription's cancellation to go out on close.
const CANCEL_GRACE: Duration = Duration::from_secs(1);

/// Wait before opening a new subscription when the server ended the last
/// one normally, so a server that ends each at once is not asked in a loop.
const RENEW_DELAY: Duration = Duration::from_secs(1);

/// Forwards a `subscriptions/listen` stream's tool changes to the relist
/// loop.
pub(crate) struct Listener {
    task: JoinHandle<()>,
    closing: CancellationToken,
}

impl Listener {
    /// Subscribe if the server needs it. From protocol 2026-07-28 on,
    /// `notifications/tools/list_changed` only arrives on a stream that asked
    /// for it; earlier versions send it unprompted, to
    /// [`Handler`](crate::Handler). `lost` is cancelled if the stream drops.
    pub(crate) async fn start(
        peer: &Peer<RoleClient>,
        list_changed: &Arc<Notify>,
        lost: &CancellationToken,
    ) -> Result<Option<Self>, ServiceError> {
        if !needs_subscription(peer) {
            return Ok(None);
        }
        let subscription = peer.listen(filter()).await?;
        let closing = CancellationToken::new();
        let task = tokio::spawn(forward(
            peer.clone(),
            subscription,
            Arc::clone(list_changed),
            lost.clone(),
            closing.clone(),
        ));
        Ok(Some(Self { task, closing }))
    }

    /// Cancel the subscription before the transport closes. A server waits
    /// for open requests when its input ends, and this one never finishes
    /// on its own.
    pub(crate) async fn stop(mut self) {
        self.closing.cancel();
        if tokio::time::timeout(CANCEL_GRACE, &mut self.task)
            .await
            .is_err()
        {
            self.task.abort();
        }
    }
}

fn needs_subscription(peer: &Peer<RoleClient>) -> bool {
    peer.peer_info().is_some_and(|info| {
        !info.protocol_version.has_initialize()
            && info
                .capabilities
                .tools
                .as_ref()
                .and_then(|tools| tools.list_changed)
                == Some(true)
    })
}

fn filter() -> SubscriptionFilter {
    SubscriptionFilter::builder().tools_list_changed().build()
}

/// Wake the relist loop on each tool change. A stream that ends without
/// the server's final result means the connection dropped; one the server
/// ends normally is opened again after [`RENEW_DELAY`].
async fn forward(
    peer: Peer<RoleClient>,
    mut subscription: Subscription,
    list_changed: Arc<Notify>,
    lost: CancellationToken,
    closing: CancellationToken,
) {
    loop {
        let next = tokio::select! {
            biased;
            () = closing.cancelled() => {
                let reason = Some("client disconnecting".to_owned());
                let _ = subscription.cancel_with_reason(reason).await;
                return;
            }
            next = subscription.next() => next,
        };
        match next {
            Ok(Some(ServerNotification::ToolListChangedNotification(_))) => {
                list_changed.notify_one();
            }
            Ok(Some(_)) => {}
            Ok(None) if matches!(subscription.end(), Some(SubscriptionEnd::Abrupt)) => {
                lost.cancel();
                return;
            }
            Ok(None) => {
                let renewed = tokio::select! {
                    biased;
                    () = closing.cancelled() => return,
                    renewed = async {
                        tokio::time::sleep(RENEW_DELAY).await;
                        peer.listen(filter()).await
                    } => renewed,
                };
                let Ok(renewed) = renewed else {
                    lost.cancel();
                    return;
                };
                subscription = renewed;
                // A change between the two streams went unseen.
                list_changed.notify_one();
            }
            Err(_) => {
                lost.cancel();
                return;
            }
        }
    }
}
