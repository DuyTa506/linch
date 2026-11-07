//! Mailbox conformance suite shared by every backend.
//!
//! Every [`Mailbox`] must pass [`mailbox_suite`]. A failed rule panics with
//! a message naming it, so call the suite from a test. Each check uses
//! recipients of its own under `conformance/`; run the suite on a mailbox
//! that holds nothing there.
//!
//! The delivery cycle the suite pins down:
//! - `send` queues a message in its recipient's inbox, fields intact;
//! - `drain` leases every message that waits and is not leased, oldest
//!   first, so concurrent drains never return one message twice;
//! - `ack` removes leased messages for good, `release` makes them wait
//!   again in their place; both ignore messages they do not hold.

use futures::future::join_all;

use super::{Mailbox, MailboxMessage};

/// Check every [`Mailbox`] rule. Panics on the first rule that fails.
pub async fn mailbox_suite(mailbox: &dyn Mailbox) {
    unknown_inboxes_are_empty(mailbox).await;
    a_message_keeps_its_fields_and_inbox(mailbox).await;
    drains_lease_until_ack(mailbox).await;
    a_release_puts_messages_back_in_order(mailbox).await;
    a_release_returns_only_what_it_names(mailbox).await;
    concurrent_sends_keep_every_message(mailbox).await;
    concurrent_drains_deliver_once(mailbox).await;
}

fn address(check: &str, who: &str) -> String {
    format!("conformance/{check}/{who}")
}

/// `count` plain messages to `recipient`, sent in order.
async fn send_numbered(
    mailbox: &dyn Mailbox,
    recipient: &str,
    count: usize,
) -> Vec<MailboxMessage> {
    let mut sent = Vec::new();
    for n in 0..count {
        let message = MailboxMessage::new("lead", recipient, n.to_string());
        mailbox.send(message.clone()).await.expect("send");
        sent.push(message);
    }
    sent
}

async fn drain(mailbox: &dyn Mailbox, recipient: &str) -> Vec<MailboxMessage> {
    mailbox.drain(recipient).await.expect("drain")
}

async fn unknown_inboxes_are_empty(mailbox: &dyn Mailbox) {
    let missing = address("unknown", "nobody");
    assert!(
        drain(mailbox, &missing).await.is_empty(),
        "an unknown inbox is empty"
    );
    mailbox.ack(&[]).await.expect("ack nothing");
    mailbox.release(&[]).await.expect("release nothing");
}

async fn a_message_keeps_its_fields_and_inbox(mailbox: &dyn Mailbox) {
    let (bob, carol) = (address("fields", "bob"), address("fields", "carol"));
    let mut message = MailboxMessage::new("alice", bob.as_str(), "for <bob> & co");
    message.kind = "request".into();
    message.request_id = Some("request-1".into());
    message.in_reply_to = Some("prior-1".into());
    mailbox.send(message.clone()).await.expect("send");
    assert!(
        drain(mailbox, &carol).await.is_empty(),
        "a drain takes only its inbox"
    );
    let drained = drain(mailbox, &bob).await;
    assert_eq!(drained, [message], "a drained message keeps every field");
    mailbox.ack(&drained).await.expect("ack");
}

async fn drains_lease_until_ack(mailbox: &dyn Mailbox) {
    let bob = address("lease", "bob");
    let sent = send_numbered(mailbox, &bob, 5).await;
    let drained = drain(mailbox, &bob).await;
    assert_eq!(drained, sent, "a drain returns the inbox oldest first");
    assert!(
        drain(mailbox, &bob).await.is_empty(),
        "a leased message is not drained again"
    );
    mailbox.ack(&drained).await.expect("ack");
    mailbox.release(&drained).await.expect("release after ack");
    assert!(
        drain(mailbox, &bob).await.is_empty(),
        "an acked message is gone for good"
    );
}

async fn a_release_puts_messages_back_in_order(mailbox: &dyn Mailbox) {
    let bob = address("release", "bob");
    let first = send_numbered(mailbox, &bob, 3).await;
    let drained = drain(mailbox, &bob).await;
    let later = MailboxMessage::new("lead", bob.as_str(), "later");
    mailbox.send(later.clone()).await.expect("send");
    mailbox.release(&drained).await.expect("release");
    let again = drain(mailbox, &bob).await;
    let mut expected = first;
    expected.push(later);
    assert_eq!(
        again, expected,
        "released messages wait again before later ones"
    );
    mailbox.ack(&again).await.expect("ack");
}

async fn a_release_returns_only_what_it_names(mailbox: &dyn Mailbox) {
    let bob = address("partial", "bob");
    let sent = send_numbered(mailbox, &bob, 2).await;
    let drained = drain(mailbox, &bob).await;
    mailbox.release(&drained[..1]).await.expect("release one");
    assert_eq!(
        drain(mailbox, &bob).await,
        sent[..1],
        "only the released message waits again"
    );
    mailbox.ack(&sent).await.expect("ack");
    assert!(
        drain(mailbox, &bob).await.is_empty(),
        "acked messages are gone"
    );
}

async fn concurrent_sends_keep_every_message(mailbox: &dyn Mailbox) {
    let bob = address("sends", "bob");
    let sent: Vec<_> = (0..25)
        .map(|n| MailboxMessage::new("alice", bob.as_str(), n.to_string()))
        .collect();
    let sends = sent.iter().map(|message| mailbox.send(message.clone()));
    for result in join_all(sends).await {
        result.expect("send");
    }
    let mut drained = drain(mailbox, &bob).await;
    drained.sort_by(|a, b| a.id.cmp(&b.id));
    let mut expected = sent;
    expected.sort_by(|a, b| a.id.cmp(&b.id));
    assert_eq!(
        drained, expected,
        "concurrent sends must not drop a message"
    );
    mailbox.ack(&drained).await.expect("ack");
}

async fn concurrent_drains_deliver_once(mailbox: &dyn Mailbox) {
    let bob = address("drains", "bob");
    let sent = send_numbered(mailbox, &bob, 10).await;
    let drains = (0..4).map(|_| mailbox.drain(&bob));
    let mut drained: Vec<MailboxMessage> = Vec::new();
    for result in join_all(drains).await {
        drained.extend(result.expect("drain"));
    }
    let mut ids: Vec<&str> = drained.iter().map(|m| m.id.as_str()).collect();
    ids.sort_unstable();
    let mut expected: Vec<&str> = sent.iter().map(|m| m.id.as_str()).collect();
    expected.sort_unstable();
    assert_eq!(
        ids, expected,
        "concurrent drains deliver each message exactly once"
    );
    mailbox.ack(&drained).await.expect("ack");
}
