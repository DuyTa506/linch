//! After a stop (or the end of input) a connection has one fixed grace to
//! finish its calls and write what is queued; a client that cannot take it
//! in that time is cut, however slowly it reads, and its tenant place is
//! freed. Time is paused: the sleeps below are virtual.

use std::io;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::Duration;

use futures::Sink;
use linch::PgPools;
use linch_rpc::{Access, Allowlists, Tenant, serve_lines, serve_messages};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader, DuplexStream};
use tokio::time::Instant;
use tokio_util::sync::CancellationToken;

/// The documented grace after a stop.
const GRACE: Duration = Duration::from_secs(15);

fn info(id: u64) -> String {
    format!("{{\"jsonrpc\":\"2.0\",\"id\":{id},\"method\":\"server.info\"}}\n")
}

/// A tenant connection (the tenant has one connection place, which the
/// connection holds) over a transport of `capacity` bytes, whose client
/// writes `lines` from a task of its own and then stays connected.
struct Client {
    tenant: Arc<Tenant>,
    stop: CancellationToken,
    serving: tokio::task::JoinHandle<io::Result<()>>,
    reader: tokio::io::ReadHalf<DuplexStream>,
    _writer: tokio::task::JoinHandle<()>,
    /// When the connection was made: the input of an `end_input` client ended then.
    began: Instant,
}

async fn connect(capacity: usize, lines: Vec<String>, end_input: bool) -> Client {
    let began = Instant::now();
    let tenant = Arc::new(Tenant::new("acme", Allowlists::new().max_connections(1)).unwrap());
    let place = Arc::clone(&tenant).connect().unwrap();
    let (ours, theirs) = tokio::io::duplex(capacity);
    let (server_reader, server_writer) = tokio::io::split(theirs);
    let stop = CancellationToken::new();
    let serving = tokio::spawn(serve_lines(
        server_reader,
        server_writer,
        Access::Tenant(place),
        stop.clone(),
    ));
    let (reader, mut writer) = tokio::io::split(ours);
    // The server stops reading once its queue is full, so the client's
    // writes come from a task of their own.
    let writer = tokio::spawn(async move {
        for line in lines {
            if writer.write_all(line.as_bytes()).await.is_err() {
                return;
            }
        }
        if end_input {
            let _ = writer.shutdown().await;
        }
        std::future::pending::<()>().await;
    });
    tokio::time::sleep(Duration::from_secs(1)).await;
    Client {
        tenant,
        stop,
        serving,
        reader,
        _writer: writer,
        began,
    }
}

impl Client {
    fn place_is_free(&self) -> bool {
        Arc::clone(&self.tenant).connect().is_ok()
    }

    /// Stop, and wait for the connection to end: how long it took, and how
    /// it ended.
    async fn stop_and_wait(&mut self) -> (Duration, io::Result<()>) {
        let started = Instant::now();
        self.stop.cancel();
        let serving = &mut self.serving;
        let ended = tokio::time::timeout(Duration::from_secs(120), serving).await;
        let ended = ended.expect("the connection ends").unwrap();
        (started.elapsed(), ended)
    }
}

/// The connection ended on the grace, not before it and not long after.
fn assert_cut_at_the_grace(took: Duration, ended: &io::Result<()>) {
    assert!(
        took >= GRACE && took <= GRACE + Duration::from_secs(1),
        "ended {took:?} after the stop, not at the grace {GRACE:?}"
    );
    let kind = ended.as_ref().err().map(io::Error::kind);
    assert_eq!(kind, Some(io::ErrorKind::TimedOut), "{ended:?}");
}

#[tokio::test(start_paused = true)]
async fn stop_ends_a_connection_whose_client_does_not_read() {
    let mut client = connect(256, (0..32).map(info).collect(), false).await;
    let (took, ended) = client.stop_and_wait().await;
    assert_cut_at_the_grace(took, &ended);
    assert!(client.place_is_free(), "its tenant place is held");
}

#[tokio::test(start_paused = true)]
async fn the_end_of_input_ends_a_connection_whose_client_does_not_read() {
    let mut client = connect(256, (0..32).map(info).collect(), true).await;
    // The client ended its input at once and keeps its reading side open.
    let ended = tokio::time::timeout(Duration::from_secs(120), &mut client.serving).await;
    let ended = ended.expect("the connection ends").unwrap();
    assert_cut_at_the_grace(client.began.elapsed(), &ended);
    assert!(client.place_is_free(), "its tenant place is held");
}

/// More answers are waiting than the queue holds (1024): calls are blocked
/// handing their answer to it, and the read loop with them.
#[tokio::test(start_paused = true)]
async fn stop_ends_a_connection_whose_client_left_more_answers_unread_than_the_queue_holds() {
    let mut client = connect(256, (0..3000).map(info).collect(), false).await;
    let (took, ended) = client.stop_and_wait().await;
    assert_cut_at_the_grace(took, &ended);
    assert!(client.place_is_free(), "its tenant place is held");
}

/// The read loop itself is blocked handing a parse error to the full queue.
#[tokio::test(start_paused = true)]
async fn stop_ends_a_connection_whose_client_left_parse_errors_unread() {
    let mut client = connect(
        256,
        (0..3000).map(|_| "not json\n".to_owned()).collect(),
        false,
    )
    .await;
    let (took, ended) = client.stop_and_wait().await;
    assert_cut_at_the_grace(took, &ended);
    assert!(client.place_is_free(), "its tenant place is held");
}

/// The same with a line that is not UTF-8 and with lines over the limit.
#[tokio::test(start_paused = true)]
async fn stop_ends_a_connection_whose_client_left_refusals_unread() {
    let lines = (0..3000).map(|id| format!("{{\"id\":{id},\"x\":\"\u{0}\"}}\n"));
    let mut client = connect(256, lines.collect(), false).await;
    let (took, ended) = client.stop_and_wait().await;
    assert_cut_at_the_grace(took, &ended);
    assert!(client.place_is_free(), "its tenant place is held");
}

/// A client that reads every answer, promptly, still gets all of them after
/// the stop.
#[tokio::test(start_paused = true)]
async fn a_client_that_reads_gets_every_queued_answer_after_stop() {
    let client = connect(256, (0..200).map(info).collect(), false).await;
    client.stop.cancel();
    let mut lines = BufReader::new(client.reader).lines();
    let mut seen = 0;
    while let Ok(Some(_)) = lines.next_line().await {
        seen += 1;
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert_eq!(seen, 200, "answers read before the connection closed");
    let ended = client.serving.await.unwrap();
    assert!(ended.is_ok(), "{ended:?}");
    assert!(Arc::clone(&client.tenant).connect().is_ok());
}

/// A byte stream read 4 KiB every 250 ms (16 KiB a second, never silent):
/// it gets what fits in the grace, exactly 15 s of reads, and is cut.
#[tokio::test(start_paused = true)]
async fn a_byte_stream_reader_gets_what_fits_in_the_grace_and_is_cut_after_it() {
    let mut client = connect(64 * 1024, (0..2000).map(info).collect(), false).await;
    let started = Instant::now();
    client.stop.cancel();
    let mut buffer = vec![0_u8; 4096];
    let mut read = 0;
    let mut serving = client.serving;
    let ended = loop {
        tokio::select! {
            biased;
            ended = &mut serving => break ended.unwrap(),
            () = tokio::time::sleep(Duration::from_millis(250)) => {}
        }
        read += client.reader.read(&mut buffer).await.unwrap();
    };
    assert_cut_at_the_grace(started.elapsed(), &ended);
    // 60 reads of 4096 bytes in 15 s (one more may land on the deadline).
    assert!(
        (59 * 4096..=61 * 4096).contains(&read),
        "read {read} bytes in the grace"
    );
}

/// A message transport whose client takes `per_message` to read each
/// message, and never stops reading.
struct SlowSink {
    per_message: Duration,
    sending: Option<Pin<Box<tokio::time::Sleep>>>,
    pending: Option<String>,
    received: Arc<Mutex<Vec<String>>>,
}

impl SlowSink {
    fn drive(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        if let Some(sleep) = self.sending.as_mut() {
            if sleep.as_mut().poll(cx).is_pending() {
                return Poll::Pending;
            }
            self.sending = None;
            let message = self.pending.take().expect("a message in flight");
            self.received.lock().unwrap().push(message);
        }
        Poll::Ready(Ok(()))
    }
}

impl Sink<String> for SlowSink {
    type Error = io::Error;

    fn poll_ready(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.drive(cx)
    }

    fn start_send(mut self: Pin<&mut Self>, item: String) -> io::Result<()> {
        self.pending = Some(item);
        self.sending = Some(Box::pin(tokio::time::sleep(self.per_message)));
        Ok(())
    }

    fn poll_flush(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.drive(cx)
    }

    fn poll_close(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.drive(cx)
    }
}

/// Serve `count` `server.info` calls over a message transport whose client
/// takes `per_message` for each message; stop once they are all queued, and
/// return how long the connection took to end, how it ended and the messages
/// the client received.
async fn slow_messages(count: u64, per_message: Duration) -> (Duration, io::Result<()>, usize) {
    let received = Arc::new(Mutex::new(Vec::new()));
    let sink = SlowSink {
        per_message,
        sending: None,
        pending: None,
        received: Arc::clone(&received),
    };
    let (sender, incoming) = tokio::sync::mpsc::unbounded_channel::<io::Result<String>>();
    let incoming = Box::pin(futures::stream::unfold(incoming, |mut incoming| async {
        incoming.recv().await.map(|message| (message, incoming))
    }));
    let stop = CancellationToken::new();
    let access = Access::Local(Arc::new(PgPools::new()));
    let serving = tokio::spawn(serve_messages(incoming, sink, access, stop.clone()));
    for id in 0..count {
        let request: Value = json!({"jsonrpc": "2.0", "id": id, "method": "server.info"});
        sender.send(Ok(request.to_string())).unwrap();
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    let started = Instant::now();
    stop.cancel();
    let ended = tokio::time::timeout(Duration::from_secs(3600), serving).await;
    let ended = ended.expect("the connection ends").unwrap();
    let received = received.lock().unwrap().len();
    (started.elapsed(), ended, received)
}

/// 2 s a message: the grace holds 7 of them (the first was begun before the
/// stop), and the rest is dropped at the end of it.
#[tokio::test(start_paused = true)]
async fn a_message_client_gets_what_fits_in_the_grace_and_is_cut_after_it() {
    let (took, ended, received) = slow_messages(10, Duration::from_secs(2)).await;
    assert_cut_at_the_grace(took, &ended);
    assert!((7..=8).contains(&received), "received {received} messages");
}

/// One message that needs more than 5 s (a 9 MiB answer on a slow link):
/// two of them fit in the grace.
#[tokio::test(start_paused = true)]
async fn a_message_client_that_needs_six_seconds_per_message_gets_two_in_the_grace() {
    let (took, ended, received) = slow_messages(3, Duration::from_secs(6)).await;
    assert_cut_at_the_grace(took, &ended);
    assert_eq!(received, 2);
}

/// A client that takes everything promptly ends the connection at once.
#[tokio::test(start_paused = true)]
async fn a_message_client_that_reads_ends_the_connection_before_the_grace() {
    let (took, ended, received) = slow_messages(10, Duration::from_millis(10)).await;
    assert!(ended.is_ok(), "{ended:?}");
    assert_eq!(received, 10);
    assert!(took < Duration::from_secs(1), "{took:?}");
}
