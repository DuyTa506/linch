//! The runner's side of the session catalog (see [`crate::catalog`]): the
//! request built on the frozen prefix, the notice of a change, and what
//! tool search does not offer (see [`crate::tool_search`]).

use std::sync::Arc;

use super::turn::Runner;
use crate::catalog;
use crate::message::{ContentBlock, Message};
use crate::provider::{ProviderRequest, ToolChoice};
use crate::scheduler::BatchPlan;
use crate::tool_search::{Loads, NOT_OFFERED, TOOL_SEARCH_TOOL};

impl Runner {
    /// The notice to add to the user message this turn ends with, when
    /// the catalog changed since the model was last told; `None` when it
    /// did not, or before the session has a prefix. A version the run
    /// already checked is not diffed again.
    pub(super) fn catalog_change(&mut self) -> Option<ContentBlock> {
        self.tell(&Loads::default())
    }

    /// The notice for a tool batch's results `blocks`: as
    /// [`Self::catalog_change`], with what the batch's searches found.
    pub(super) fn catalog_loads(&mut self, blocks: &[ContentBlock]) -> Option<ContentBlock> {
        let response = self
            .log
            .last_visible()
            .filter(|_| self.agent.tool_search.is_some());
        let loads = response.map_or_else(Loads::default, |response| Loads::of(response, blocks));
        self.tell(&loads)
    }

    fn tell(&mut self, loads: &Loads) -> Option<ContentBlock> {
        let catalog = self.catalog.as_ref()?;
        let version = catalog::version(&self.agent);
        if self.seen == Some(version) && loads.is_empty() {
            return None;
        }
        self.seen = Some(version);
        let live = catalog::live(&self.agent, Some(catalog), loads);
        Some(ContentBlock::CatalogChange(catalog.change(&live)?))
    }

    /// Under tool search, take this turn's tools down to those the
    /// session was offered.
    pub(super) fn offered_only(&mut self) {
        let Some(catalog) = (self.catalog.as_ref()).filter(|_| self.agent.tool_search.is_some())
        else {
            return;
        };
        let offered = self.snapshot.filtered(|name| catalog.offers(name));
        self.snapshot = Arc::new(offered);
    }

    /// Refuse a call of a registered tool the session was not offered as
    /// deferred, rather than as not registered.
    pub(super) fn refuse_unoffered(&self, plan: &mut BatchPlan) {
        let Some(catalog) = (self.catalog.as_ref()).filter(|_| self.agent.tool_search.is_some())
        else {
            return;
        };
        let registered = self.agent.snapshot();
        plan.refuse_unresolved(|name| {
            if catalog.offers(name) || registered.resolve(name).is_none() {
                return None;
            }
            Some(match name {
                TOOL_SEARCH_TOOL => NOT_OFFERED.to_owned(),
                _ => {
                    format!("Tool '{name}' is deferred; call tool_search with select:{name} first")
                }
            })
        });
    }

    /// The request for `messages`: the session's frozen system blocks and
    /// tools, narrowed to this turn's tools when a skill set them, with
    /// the tools added since the prefix named. Before the prefix is logged
    /// (never, once the prompt is in), the live ones. It carries the
    /// run's output schema, if any, unless the provider cannot take it,
    /// and the final tool the run made of it last; and the run's tool
    /// choice (see [`Self::tool_choice`]).
    pub(super) fn request(&self, messages: Vec<Message>) -> ProviderRequest {
        let agent = &self.agent;
        let mut request = match &self.catalog {
            None => {
                let system = agent.system(None);
                agent.request(system, self.snapshot.definitions(), messages)
            }
            Some(catalog) => {
                let snapshot = &self.snapshot;
                let keep = |name: &str| snapshot.resolve(name).is_some();
                let keep: Option<&dyn Fn(&str) -> bool> = self.narrowed.then_some(&keep);
                let tools = catalog.tools(keep);
                let mut request = agent.request(catalog.system().to_vec(), tools, messages);
                request.appended_tools = catalog.appended(keep);
                request
            }
        };
        if agent.provider.native_output_schema() {
            request.output_schema = (self.output.as_ref()).map(|output| output.schema().clone());
        }
        // Last, so the cached prefix of tools before it keeps its bytes.
        let made = self.final_tool.as_ref().and_then(|tool| tool.made.clone());
        request.tools.extend(made);
        request.tool_choice = self.tool_choice(&request);
        request
    }

    /// Take the named tool choice off `request` when the turn does not offer
    /// that tool (a skill narrowed the turn, tool search deferred it, a hook
    /// rewrote the request): a provider answers such a request with a 400.
    /// The turn goes out as `auto`; returns the name dropped.
    pub(super) fn drop_unoffered_choice(request: &mut ProviderRequest) -> Option<String> {
        let Some(ToolChoice::Tool { name }) = &request.tool_choice else {
            return None;
        };
        if request.tools.iter().any(|tool| &tool.name == name) {
            return None;
        }
        let name = name.clone();
        request.tool_choice = Some(ToolChoice::Auto {});
        Some(name)
    }

    /// The tool choice of `request`: none on a turn that offers no tool
    /// (Python's rule); else the run's; else, when the run's own final
    /// tool is the only tool, that tool forced, as Python's compatible
    /// mode does, if the provider takes it with this thinking (or
    /// `required`, which forces it too); else none.
    fn tool_choice(&self, request: &ProviderRequest) -> Option<ToolChoice> {
        if request.tools.is_empty() {
            return None;
        }
        if let Some(choice) = &self.tool_choice {
            return Some(choice.clone());
        }
        let tool = self
            .final_tool
            .as_ref()
            .filter(|tool| tool.made.is_some())?;
        if request.tools.len() != 1 {
            return None;
        }
        let provider = &self.agent.provider;
        let thinking = request.thinking.as_ref();
        let named = ToolChoice::Tool {
            name: tool.name.clone(),
        };
        [named, ToolChoice::Required {}]
            .into_iter()
            .find(|choice| provider.tool_choice_refusal(choice, thinking).is_none())
    }
}
