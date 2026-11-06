//! The runner's side of the session catalog (see [`crate::catalog`]): the
//! request built on the frozen prefix, the notice of a change, and what
//! tool search does not offer (see [`crate::tool_search`]).

use std::sync::Arc;

use super::turn::Runner;
use crate::catalog;
use crate::message::{ContentBlock, Message};
use crate::provider::ProviderRequest;
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
    /// (never, once the prompt is in), the live ones.
    pub(super) fn request(&self, messages: Vec<Message>) -> ProviderRequest {
        let agent = &self.agent;
        let Some(catalog) = &self.catalog else {
            let system = agent.system(None);
            return agent.request(system, self.snapshot.definitions(), messages);
        };
        let snapshot = &self.snapshot;
        let keep = |name: &str| snapshot.resolve(name).is_some();
        let keep: Option<&dyn Fn(&str) -> bool> = self.narrowed.then_some(&keep);
        let tools = catalog.tools(keep);
        let mut request = agent.request(catalog.system().to_vec(), tools, messages);
        request.appended_tools = catalog.appended(keep);
        request
    }
}
