//! A small MCP server for the tests and the stdio example.
//!
//! Tools: `echo` (read-only hint) returns its `text` argument, `fail`
//! returns `isError`, `image` returns text and an image, `pid` returns the
//! server's process id, and `slow` waits until the client cancels it.

use std::borrow::Cow;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, PoisonError};

use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, DiscoverRequestMethod,
    DiscoverResult, ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerConfig,
    SubscriptionFilter, Tool, ToolAnnotations,
};
use rmcp::service::{NotificationContext, RequestContext, RoleServer, SubscriptionContext};
use rmcp::{ErrorData, ServerHandler};
use serde_json::{Value, json};
use tokio::sync::{Notify, watch};

/// State shared by every connection to the test server.
#[derive(Clone)]
pub struct Fixture {
    tools: Arc<Mutex<Vec<Tool>>>,
    /// Bumped by [`Fixture::set_tools`]; each connection notifies on change.
    version: Arc<watch::Sender<u64>>,
    /// Notified when a `slow` call sees the client's cancellation.
    pub cancelled: Arc<Notify>,
    /// `subscriptions/listen` streams opened so far.
    pub subscriptions: Arc<AtomicUsize>,
    /// While set, `tools/list` never answers.
    pub hang_lists: Arc<AtomicBool>,
    /// Notified when a `tools/list` request starts hanging.
    pub hung: Arc<Notify>,
    /// Notify to end one open `subscriptions/listen` stream normally.
    pub end_subscription: Arc<Notify>,
    /// Whether to answer `server/discover` like a pre-2026-07-28 server.
    legacy: bool,
    /// Whether every `tools/list` page holds one new tool and a next cursor.
    endless: bool,
}

impl Fixture {
    pub fn new(tools: Vec<Tool>) -> Self {
        Self {
            tools: Arc::new(Mutex::new(tools)),
            version: Arc::new(watch::Sender::new(0)),
            cancelled: Arc::new(Notify::new()),
            subscriptions: Arc::new(AtomicUsize::new(0)),
            hang_lists: Arc::new(AtomicBool::new(false)),
            hung: Arc::new(Notify::new()),
            end_subscription: Arc::new(Notify::new()),
            legacy: false,
            endless: false,
        }
    }

    /// A server that only speaks the `initialize` lifecycle.
    pub fn legacy(mut self) -> Self {
        self.legacy = true;
        self
    }

    /// A server whose tool list never ends: page `n` holds tool `p<n>`.
    pub fn endless(mut self) -> Self {
        self.endless = true;
        self
    }

    /// Change the tool list and tell every connected client.
    pub fn set_tools(&self, tools: Vec<Tool>) {
        *self.tools.lock().unwrap_or_else(PoisonError::into_inner) = tools;
        self.version.send_modify(|version| *version += 1);
    }

    fn tools(&self) -> Vec<Tool> {
        self.tools
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }
}

/// A tool taking an optional string `text`.
pub fn tool(name: impl Into<Cow<'static, str>>, description: &'static str) -> Tool {
    let Value::Object(schema) = json!({
        "type": "object",
        "properties": {"text": {"type": "string"}},
    }) else {
        unreachable!()
    };
    Tool::new(name, description, Arc::new(schema))
}

/// The tools the server can run.
pub fn standard_tools() -> Vec<Tool> {
    vec![
        tool("echo", "Echo text.").with_annotations(ToolAnnotations::new().read_only(true)),
        tool("fail", "Always fails."),
        tool("image", "Return a chart."),
        tool("pid", "Return the server's process id."),
        tool("slow", "Wait until cancelled."),
    ]
}

#[derive(Clone)]
pub struct TestServer(pub Fixture);

impl ServerHandler for TestServer {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(
            ServerCapabilities::builder()
                .enable_tools()
                .enable_tool_list_changed()
                .build(),
        )
    }

    async fn discover(
        &self,
        _context: RequestContext<RoleServer>,
    ) -> Result<DiscoverResult, ErrorData> {
        if self.0.legacy {
            return Err(ErrorData::method_not_found::<DiscoverRequestMethod>());
        }
        Ok(DiscoverResult::from_server_info(
            self.supported_protocol_versions().into_owned(),
            self.get_info(),
        ))
    }

    async fn list_tools(
        &self,
        request: Option<PaginatedRequestParams>,
        context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        if self.0.hang_lists.load(Ordering::SeqCst) {
            self.0.hung.notify_one();
            context.ct.cancelled().await;
            return Err(ErrorData::internal_error("cancelled", None));
        }
        if self.0.endless {
            let page: u64 = request
                .and_then(|request| request.cursor)
                .map_or(0, |cursor| cursor.parse().unwrap());
            let mut result =
                ListToolsResult::with_all_items(vec![tool(format!("p{page}"), "Page.")]);
            result.next_cursor = Some((page + 1).to_string());
            return Ok(result);
        }
        Ok(ListToolsResult::with_all_items(self.0.tools()))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let text = request
            .arguments
            .as_ref()
            .and_then(|arguments| arguments.get("text"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        let result = match request.name.as_ref() {
            "echo" => CallToolResult::success(vec![ContentBlock::text(text)]),
            "fail" => CallToolResult::error(vec![ContentBlock::text("it broke")]),
            "image" => CallToolResult::success(vec![
                ContentBlock::text("chart:"),
                ContentBlock::image("aGk=", "image/png"),
            ]),
            "pid" => {
                CallToolResult::success(vec![ContentBlock::text(std::process::id().to_string())])
            }
            "slow" => {
                context.ct.cancelled().await;
                self.0.cancelled.notify_one();
                CallToolResult::success(vec![])
            }
            other => {
                return Err(ErrorData::invalid_params(
                    format!("unknown tool {other}"),
                    None,
                ));
            }
        };
        Ok(result.into())
    }

    fn accepted_subscription_filter(
        &self,
        requested: &SubscriptionFilter,
    ) -> Option<SubscriptionFilter> {
        Some(requested.supported_by(&self.get_info().capabilities))
    }

    /// Modern clients get list changes on their subscription stream.
    async fn listen(&self, context: SubscriptionContext) -> Result<(), ErrorData> {
        self.0.subscriptions.fetch_add(1, Ordering::SeqCst);
        let mut version = self.0.version.subscribe();
        loop {
            tokio::select! {
                () = context.cancelled() => return Ok(()),
                () = self.0.end_subscription.notified() => return Ok(()),
                changed = version.changed() => {
                    if changed.is_err() {
                        return Ok(());
                    }
                    let _ = context.sink().notify_tool_list_changed().await;
                }
            }
        }
    }

    /// Legacy clients get list changes unprompted.
    async fn on_initialized(&self, context: NotificationContext<RoleServer>) {
        let mut version = self.0.version.subscribe();
        let peer = context.peer.clone();
        tokio::spawn(async move {
            while version.changed().await.is_ok() {
                if peer.notify_tool_list_changed().await.is_err() {
                    break;
                }
            }
        });
    }
}
