use super::*;
use async_trait::async_trait;
use futures::stream;

#[tokio::test]
async fn cancelling_mid_stream_settles_a_cancelled_receipt() {
    let provider = ScriptedProvider::new([ScriptedTurn::builder("m")
        .text("a")
        .text("b")
        .usage(usage(1, 1))
        .build()]);
    let cancel = CancellationToken::new();
    let mut deltas = Vec::new();
    let token = cancel.clone();
    let mut sink = |delta: PartialAssistantEvent| {
        deltas.push(delta);
        token.cancel();
    };
    let outcome = call()
        .drive(&provider, request(), &cancel, Some(&mut sink))
        .await;
    assert_eq!(deltas.len(), 1);
    assert_eq!(outcome.receipt.disposition, CallDisposition::Cancelled);
    assert_eq!(outcome.receipt.usage, None);
    assert_eq!(outcome.receipt.actual_model.as_deref(), Some("m"));
    let failure = outcome.result.unwrap_err();
    assert!(matches!(failure.error, ProviderError::Cancelled));
    assert_eq!(failure.event.error["kind"], "cancelled");
}

#[tokio::test]
async fn a_call_cancelled_before_dispatch_never_reaches_the_provider() {
    let provider = ScriptedProvider::new([ScriptedTurn::builder("m").text("a").build()]);
    let cancel = CancellationToken::new();
    cancel.cancel();
    let outcome = call().drive(&provider, request(), &cancel, None).await;
    assert_eq!(outcome.receipt.disposition, CallDisposition::Cancelled);
    assert_eq!(outcome.receipt.actual_model, None);
    assert!(provider.requests().is_empty());
    assert_eq!(provider.remaining(), 1);
}

/// A provider that ignores its token.
#[derive(Clone, Copy, Debug)]
enum Stubborn {
    /// Stalls forever after `message_start`.
    Stalls,
    /// Waits for the abort and reports it as a transport error.
    ReportsAbort,
    /// Streams a whole response without ever pausing.
    KeepsStreaming,
}

#[async_trait]
impl Provider for Stubborn {
    fn id(&self) -> &str {
        "stubborn"
    }

    async fn stream(
        &self,
        _request: ProviderRequest,
        cancel: CancellationToken,
    ) -> Result<ProviderStream, ProviderError> {
        match self {
            Self::Stalls => Ok(stream::iter([Ok(start())]).chain(stream::pending()).boxed()),
            Self::ReportsAbort => {
                cancel.cancelled().await;
                Err(ProviderError::Transport {
                    message: "aborted".into(),
                })
            }
            Self::KeepsStreaming => {
                let events = [
                    start(),
                    text("a"),
                    text("b"),
                    end(StopReason::EndTurn, usage(1, 1)),
                ];
                Ok(stream::iter(events.map(Ok)).boxed())
            }
        }
    }
}

#[tokio::test]
async fn cancel_ends_the_call_even_when_the_provider_ignores_it() {
    for provider in [
        Stubborn::Stalls,
        Stubborn::ReportsAbort,
        Stubborn::KeepsStreaming,
    ] {
        let cancel = CancellationToken::new();
        let token = cancel.clone();
        tokio::spawn(async move {
            tokio::task::yield_now().await;
            token.cancel();
        });
        // An always-ready stream never yields to the spawned task, so the
        // sink cancels instead.
        let token = cancel.clone();
        let mut deltas = 0;
        let mut sink = |_: PartialAssistantEvent| {
            deltas += 1;
            token.cancel();
        };
        let outcome = call()
            .drive(&provider, request(), &cancel, Some(&mut sink))
            .await;
        assert_eq!(
            outcome.receipt.disposition,
            CallDisposition::Cancelled,
            "{provider:?}"
        );
        assert_eq!(outcome.receipt.usage, None, "{provider:?}");
        assert!(matches!(
            outcome.result.unwrap_err().error,
            ProviderError::Cancelled
        ));
        assert!(deltas <= 1, "{provider:?}");
    }
}
